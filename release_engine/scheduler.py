"""
release_engine/scheduler.py

Pure target-time selection for the adaptive discretionary release (v0.39).

target = base (local) + an offset of between MIN and MAX minutes, drawn
pseudorandomly but DETERMINISTICALLY from a stored secret seed and the local
date (HMAC-SHA256). Same seed + same date -> same offset, so a plan can be
audited/replayed and a restart never redraws it. A different date gives an
independent draw, which is what re-targeting uses.

The result is always validated against the window classifier: a target that is
not inside a DISCRETIONARY window is never returned. The configuration is
validated up front so that each base + MAX offset still ends before quiet hours.

Owner, 2026-11-17: when today's own moment has already passed (or is not free time), the release is not moved to another day: a new secret
moment is drawn from the free time left today (`_later_today`). Only when no free time is left today does it move to a later day.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import date, datetime, time, timedelta

from schedule_windows.windows import WindowConfig, WindowKind, classify, is_non_working_day

__all__ = ["validate_offsets", "draw_offset_minutes", "plan_target", "MIN_LEAD"]

MIN_LEAD = timedelta(minutes=1)     # a target must be at least this far in the future
_MAX_SEARCH_DAYS = 14
_STEP = timedelta(minutes=5)


def validate_offsets(cfg: WindowConfig, min_offset: int, max_offset: int) -> None:
    if isinstance(min_offset, bool) or isinstance(max_offset, bool) or not isinstance(min_offset, int) \
            or not isinstance(max_offset, int):
        raise ValueError("offsets must be integers (minutes)")
    if not (0 <= min_offset <= max_offset):
        raise ValueError("require 0 <= UNLOCK_WINDOW_MIN_OFFSET_MINUTES <= UNLOCK_WINDOW_MAX_OFFSET_MINUTES")
    quiet_minutes = cfg.quiet_start.hour * 60 + cfg.quiet_start.minute
    for name, base in ("WORKDAY_END", cfg.work_end), ("WEEKEND_RELEASE_START", cfg.weekend_release_start):
        if base.hour * 60 + base.minute + max_offset >= quiet_minutes:
            raise ValueError(f"{name} plus the maximum offset must end before quiet hours start")


def draw_offset_minutes(seed: bytes, local_date: date, min_offset: int, max_offset: int) -> int:
    if not isinstance(seed, (bytes, bytearray)) or len(seed) < 16:
        raise ValueError("seed must be at least 16 bytes")
    digest = hmac.new(bytes(seed), local_date.isoformat().encode("ascii"), hashlib.sha256).digest()
    span = max_offset - min_offset + 1
    return min_offset + int.from_bytes(digest[:8], "big") % span


def plan_target(
    now: datetime, seed: bytes, cfg: WindowConfig, min_offset: int, max_offset: int,
) -> datetime | None:
    """The first valid target strictly after `now + MIN_LEAD`, searching today and the
    following days. None only if nothing valid exists within the search horizon."""
    validate_offsets(cfg, min_offset, max_offset)
    today = cfg.local(now).date()
    for i in range(_MAX_SEARCH_DAYS):
        d = today + timedelta(days=i)
        base = cfg.weekend_release_start if is_non_working_day(cfg, d) else cfg.work_end
        target = cfg.to_utc(d, base) + timedelta(minutes=draw_offset_minutes(seed, d, min_offset, max_offset))
        if target <= now + MIN_LEAD or classify(cfg, target) is not WindowKind.DISCRETIONARY:
            if i == 0 and (again := _later_today(now, seed, cfg, d)) is not None:
                return again
            continue
        return target
    return None


def _later_today(now: datetime, seed: bytes, cfg: WindowConfig, d: date) -> datetime | None:
    """Owner, 2026-11-17: when today's own moment has passed (or is not free time), a new secret moment later TODAY, drawn the same way
    (HMAC of the seed and the date) from the free-time moments left today in 5-minute steps; None when no free time is left today."""
    start = cfg.to_utc(d, time(0, 0))
    end = cfg.to_utc(d + timedelta(days=1), time(0, 0))
    at = max(start, now + MIN_LEAD)
    at = at.replace(second=0, microsecond=0) + timedelta(minutes=(5 - at.minute % 5) % 5 or 5)
    moments = []
    while at < end:
        if classify(cfg, at) is WindowKind.DISCRETIONARY:
            moments.append(at)
        at += _STEP
    if not moments:
        return None
    digest = hmac.new(bytes(seed), (d.isoformat() + ":later-today").encode("ascii"), hashlib.sha256).digest()
    return moments[int.from_bytes(digest[:8], "big") % len(moments)]
