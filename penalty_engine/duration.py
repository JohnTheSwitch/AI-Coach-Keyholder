"""
penalty_engine/duration.py

Dynamic Penalty Window duration (v0.39, section 11.8). PURE: no I/O, no clock, not wired into the Penalty Engine yet.

    PW_Days = MaxDays - Trust * (MaxDays - MinDays)        (exact hours, rounded to whole hours)

Headroom principle: a NEW window never starts above 50% of the mode limit (Standard 7 days of 14, Advanced 15 of 30); the other
half is reserved for extensions caused by later violations inside an active window. A first MINOR offense is a warning plus a
Trust drop only (0 hours). Goal failures never create a window (only confirmed violations do).
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

__all__ = ["Mode", "ViolationSeverity", "SEVERITY_RANGES_DAYS", "HARD_CAP_HOURS", "INITIAL_PW_MAX_CAP_HOURS",
           "RECOVERY_CREDIT_MAX_FRACTION", "initial_window_hours"]


class Mode(StrEnum):
    STANDARD = "standard"
    ADVANCED = "advanced"


class ViolationSeverity(StrEnum):
    MINOR = "minor"
    MODERATE = "moderate"
    SEVERE = "severe"


# (MinDays, MaxDays)
SEVERITY_RANGES_DAYS: dict[tuple[Mode, ViolationSeverity], tuple[int, int]] = {
    (Mode.STANDARD, ViolationSeverity.MINOR): (1, 3),
    (Mode.ADVANCED, ViolationSeverity.MINOR): (2, 5),
    (Mode.STANDARD, ViolationSeverity.MODERATE): (3, 5),
    (Mode.ADVANCED, ViolationSeverity.MODERATE): (5, 10),
    (Mode.STANDARD, ViolationSeverity.SEVERE): (5, 7),
    (Mode.ADVANCED, ViolationSeverity.SEVERE): (8, 15),
}
HARD_CAP_HOURS = {Mode.STANDARD: 14 * 24, Mode.ADVANCED: 30 * 24}
INITIAL_PW_MAX_CAP_HOURS = {m: cap // 2 for m, cap in HARD_CAP_HOURS.items()}      # 168 h / 360 h
RECOVERY_CREDIT_MAX_FRACTION = Decimal("0.5")                                         # credits may remove at most half


def initial_window_hours(severity: ViolationSeverity, mode: Mode, trust: float | Decimal, *, prior_offenses: int = 0) -> int:
    """Hours for a NEW window. `prior_offenses` counts earlier confirmed violations of the same rule group; a first MINOR
    offense (prior_offenses == 0) yields 0 hours (warning only). Higher Trust gives a shorter window."""
    if isinstance(prior_offenses, bool) or not isinstance(prior_offenses, int) or prior_offenses < 0:
        raise ValueError("prior_offenses must be a non-negative int")
    t = trust if isinstance(trust, Decimal) else Decimal(str(trust))
    if not Decimal(0) <= t <= Decimal(1):
        raise ValueError("trust must be within [0, 1]")
    if severity is ViolationSeverity.MINOR and prior_offenses == 0:
        return 0
    lo, hi = SEVERITY_RANGES_DAYS[(mode, severity)]
    days = Decimal(hi) - t * Decimal(hi - lo)
    hours = int((days * 24).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    return min(hours, INITIAL_PW_MAX_CAP_HOURS[mode])
