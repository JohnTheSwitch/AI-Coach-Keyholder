"""
supervision/models.py

Pure definitions for proactive supervision (v0.39): the supervision hysteresis
thresholds and the four Trust levels with their check-in frequency and hygiene
interval. No I/O. All numbers are owner-supplied bindings.
"""

from __future__ import annotations

from enum import IntEnum

__all__ = [
    "ENTER_BELOW", "EXIT_AT_OR_ABOVE", "MIN_CONFIDENCE", "EXIT_CONSECUTIVE_EVALUATIONS",
    "TrustLevel", "trust_level", "check_ins_per_day", "hygiene_interval_days",
]

ENTER_BELOW = 0.40                   # supervision starts when score < this ...
MIN_CONFIDENCE = 0.30                # ... and confidence >= this (a weak estimate has no authority)
EXIT_AT_OR_ABOVE = 0.50              # supervision ends after score >= this ...
EXIT_CONSECUTIVE_EVALUATIONS = 2     # ... for this many consecutive evaluations


class TrustLevel(IntEnum):
    LEVEL_1 = 1      # 0.00 - 0.44  strict supervision
    LEVEL_2 = 2      # 0.45 - 0.69
    LEVEL_3 = 3      # 0.70 - 0.84
    LEVEL_4 = 4      # 0.85 - 1.00  full autonomy


def trust_level(score: float) -> TrustLevel:
    if not (0.0 <= score <= 1.0):
        raise ValueError("score must be within [0, 1]")
    if score >= 0.85:
        return TrustLevel.LEVEL_4
    if score >= 0.70:
        return TrustLevel.LEVEL_3
    if score >= 0.45:
        return TrustLevel.LEVEL_2
    return TrustLevel.LEVEL_1


_CHECK_INS = {TrustLevel.LEVEL_4: 0, TrustLevel.LEVEL_3: 1, TrustLevel.LEVEL_2: 2, TrustLevel.LEVEL_1: 3}
_HYGIENE_DAYS = {TrustLevel.LEVEL_4: 1, TrustLevel.LEVEL_3: 2, TrustLevel.LEVEL_2: 3, TrustLevel.LEVEL_1: 4}


def check_ins_per_day(level: TrustLevel) -> int:
    return _CHECK_INS[level]


def hygiene_interval_days(level: TrustLevel) -> int:
    """Calendar days (APP_TIMEZONE), not rolling hours: available when
    current_date >= last_hygiene_date + N."""
    return _HYGIENE_DAYS[level]
