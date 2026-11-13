"""
supervision/models.py

Pure definitions for proactive supervision (v0.39): the supervision hysteresis
thresholds and the four Trust levels with their check-in frequency and hygiene
interval. No I/O. All numbers are owner-supplied bindings.

Owner, 2026-11-13: Supervision Mode follows SHORT-TERM Trust (trust_manager/short_term.py), the share of good evidence in a recent window.
The Trust levels below (check-ins per day, hygiene interval) still follow the long-term score: the check-ins are what produces short-term
evidence, and tying their number to short-term Trust would switch them off after one good week, leave short-term Trust without evidence and
switch them back on (a weekly swing).
"""

from __future__ import annotations

from datetime import timedelta
from enum import IntEnum

__all__ = [
    "ENTER_BELOW", "EXIT_AT_OR_ABOVE", "MIN_PIECES_TO_ENTER", "CHECK_IN_DOMAIN_WINDOW", "OTHER_DOMAIN_WINDOW", "decide",
    "TrustLevel", "trust_level", "check_ins_per_day", "hygiene_interval_days",
]

ENTER_BELOW = 0.40                   # supervision starts when short-term Trust < this ...
MIN_PIECES_TO_ENTER = 5              # ... from at least this many pieces of evidence in the window (thin evidence has no authority)
EXIT_AT_OR_ABOVE = 0.60              # supervision ends when short-term Trust >= this (the 0.40 - 0.60 band changes nothing)
# The window: the check-in domain gets evidence several times a day, so one week shows the present; any other domain (hygiene) gets a few
# pieces a week and looks back two. These keep the timings agreed before: about a week of answered check-ins, two quiet weeks for hygiene.
CHECK_IN_DOMAIN_WINDOW = timedelta(days=7)
OTHER_DOMAIN_WINDOW = timedelta(days=14)


def decide(active: bool, short_term: float | None, pieces: int) -> str | None:
    """'entered', 'exited' or None. Unknown short-term Trust (no evidence in the window) changes nothing."""
    if short_term is None:
        return None
    if not active:
        return "entered" if short_term < ENTER_BELOW and pieces >= MIN_PIECES_TO_ENTER else None
    return "exited" if short_term >= EXIT_AT_OR_ABOVE else None


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
