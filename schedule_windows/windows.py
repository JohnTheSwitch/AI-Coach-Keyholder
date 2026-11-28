"""
schedule_windows/windows.py

Pure local-time window classification (v0.39). No I/O, no clock: the instant is
always an argument. Precedence: QUIET > WORK > DISCRETIONARY.

- QUIET: QUIET_HOURS_START -> QUIET_HOURS_END every day (crosses midnight).
  No unlock, no proactive messages.
- WORK: on a working day (Mon-Fri that is not a public holiday), from the end of
  quiet hours until WORKDAY_END. No unlock. (The stretch between quiet end and
  WORKDAY_START is treated as WORK too: it is not an evening, so it is never
  discretionary -- the conservative reading.)
- DISCRETIONARY: workday evenings (WORKDAY_END -> QUIET start) and the whole
  non-quiet day on weekends and public holidays.

Time-zone rules use zoneinfo, so DST transitions are handled by the instant->local
conversion; boundaries are built per local date. If the holiday calendar cannot be
loaded, weekdays stay working days (stricter for unlocks); a wrong or missing holiday
can therefore only make the system more conservative, never less.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from enum import StrEnum
from functools import lru_cache
from zoneinfo import ZoneInfo

__all__ = ["WindowKind", "WindowConfig", "parse_hhmm", "classify", "discretionary_interval", "is_non_working_day"]

logger = logging.getLogger("ai_coach_keyholder.schedule_windows")


class WindowKind(StrEnum):
    QUIET = "quiet"
    WORK = "work"
    DISCRETIONARY = "discretionary"


def parse_hhmm(value: str, *, name: str) -> time:
    try:
        hh, mm = value.strip().split(":")
        t = time(int(hh), int(mm))
    except Exception as exc:
        raise ValueError(f"{name} must be HH:MM, got {value!r}") from exc
    return t


@lru_cache(maxsize=64)
def _holidays_for(country: str, year: int):
    import holidays
    return holidays.country_holidays(country, years=year)


@dataclass(frozen=True)
class WindowConfig:
    tz: ZoneInfo
    quiet_start: time
    quiet_end: time
    work_start: time
    work_end: time
    holiday_country: str | None = "CZ"
    # Base time of the adaptive release on weekends and public holidays (WORKDAY_END has no
    # meaning there). Owner decision: 10:00 local.
    weekend_release_start: time = time(10, 0)
    # Owner, 2026-11-25: a weekday may end at its own time (schedule_settings: `get(weekday) -> time | None`, shared and live). None or a
    # missing day: work_end.
    work_ends: object = field(default=None, compare=False, hash=False, repr=False)
    # Owner, 2026-11-28: a weekend morning stays quiet until his wake time (schedule_settings: `get(weekday) -> time | None`, Sat/Sun only).
    quiet_ends: object = field(default=None, compare=False, hash=False, repr=False)

    def end_of_quiet(self, d: date) -> time:
        """When quiet hours end on the morning of local date `d` (a weekend's own wake time, else QUIET_HOURS_END)."""
        if self.quiet_ends is not None and d.weekday() >= 5:
            try:
                own = self.quiet_ends.get(d.weekday())
            except Exception:
                own = None
            if own is not None and self.quiet_end < own <= time(12, 0) and own < self.quiet_start:
                return own
        return self.quiet_end

    def end_of_work(self, d: date) -> time:
        """The end of work time on local date `d` (its own per-day end, else WORKDAY_END)."""
        if self.work_ends is not None:
            try:
                own = self.work_ends.get(d.weekday())
            except Exception:
                own = None
            if own is not None and self.work_start < own < self.quiet_start:
                return own
        return self.work_end

    @classmethod
    def build(
        cls, *, timezone_name: str, quiet_start: str, quiet_end: str, work_start: str, work_end: str,
        holiday_country: str | None = "CZ", weekend_release_start: str = "10:00", work_ends: object = None, quiet_ends: object = None,
    ) -> "WindowConfig":
        """Fail-fast validation of the whole window layout."""
        try:
            tz = ZoneInfo(timezone_name)
        except Exception as exc:
            raise ValueError(f"APP_TIMEZONE {timezone_name!r} is not a known IANA time zone") from exc
        qs, qe = parse_hhmm(quiet_start, name="QUIET_HOURS_START"), parse_hhmm(quiet_end, name="QUIET_HOURS_END")
        ws, we = parse_hhmm(work_start, name="WORKDAY_START"), parse_hhmm(work_end, name="WORKDAY_END")
        if qs <= qe:
            raise ValueError("quiet hours must cross midnight (start later than end)")
        if not (qe <= ws < we < qs):
            raise ValueError("require QUIET_HOURS_END <= WORKDAY_START < WORKDAY_END < QUIET_HOURS_START")
        wr = parse_hhmm(weekend_release_start, name="WEEKEND_RELEASE_START")
        if not (qe <= wr < qs):
            raise ValueError("require QUIET_HOURS_END <= WEEKEND_RELEASE_START < QUIET_HOURS_START")
        return cls(tz=tz, quiet_start=qs, quiet_end=qe, work_start=ws, work_end=we,
                   holiday_country=(holiday_country or None), weekend_release_start=wr, work_ends=work_ends, quiet_ends=quiet_ends)

    def local(self, instant: datetime) -> datetime:
        if instant.tzinfo is None:
            raise ValueError("instant must be timezone-aware")
        return instant.astimezone(self.tz)

    def to_utc(self, d: date, t: time) -> datetime:
        return datetime.combine(d, t, tzinfo=self.tz).astimezone(timezone.utc)


def is_non_working_day(cfg: WindowConfig, d: date) -> bool:
    """Weekend or public holiday. A holiday-calendar failure never creates a free day."""
    if d.weekday() >= 5:
        return True
    if cfg.holiday_country:
        try:
            return d in _holidays_for(cfg.holiday_country, d.year)
        except Exception:
            logger.warning("Holiday calendar unavailable; treating %s as a working day.", d)
    return False


def classify(cfg: WindowConfig, instant: datetime) -> WindowKind:
    loc = cfg.local(instant)
    t = loc.timetz().replace(tzinfo=None)
    if t >= cfg.quiet_start or t < cfg.end_of_quiet(loc.date()):
        return WindowKind.QUIET
    if is_non_working_day(cfg, loc.date()):
        return WindowKind.DISCRETIONARY
    return WindowKind.WORK if t < cfg.end_of_work(loc.date()) else WindowKind.DISCRETIONARY


def discretionary_interval(cfg: WindowConfig, d: date) -> tuple[datetime, datetime]:
    """The discretionary interval of local date `d`, as UTC instants [start, end)."""
    start = cfg.end_of_quiet(d) if is_non_working_day(cfg, d) else cfg.end_of_work(d)
    return cfg.to_utc(d, start), cfg.to_utc(d, cfg.quiet_start)
