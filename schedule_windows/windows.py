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
from dataclasses import dataclass
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

    @classmethod
    def build(
        cls, *, timezone_name: str, quiet_start: str, quiet_end: str, work_start: str, work_end: str,
        holiday_country: str | None = "CZ",
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
        return cls(tz=tz, quiet_start=qs, quiet_end=qe, work_start=ws, work_end=we,
                   holiday_country=(holiday_country or None))

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
    if t >= cfg.quiet_start or t < cfg.quiet_end:
        return WindowKind.QUIET
    if is_non_working_day(cfg, loc.date()):
        return WindowKind.DISCRETIONARY
    return WindowKind.WORK if t < cfg.work_end else WindowKind.DISCRETIONARY


def discretionary_interval(cfg: WindowConfig, d: date) -> tuple[datetime, datetime]:
    """The discretionary interval of local date `d`, as UTC instants [start, end)."""
    start = cfg.quiet_end if is_non_working_day(cfg, d) else cfg.work_end
    return cfg.to_utc(d, start), cfg.to_utc(d, cfg.quiet_start)
