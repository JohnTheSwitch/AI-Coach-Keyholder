"""
trust_manager/clean_streak.py

Clean Day streak recovery (v0.39, section 11.8). PURE: no I/O, no clock, not wired yet.

A Clean Day has no violation or incident, every assigned daily goal met, and every proactive check-in completed on time and
sufficiently. Consecutive clean days (S) grow the daily Trust recovery: days 1-3 +0.008, days 4-7 +0.015, day 8 on +0.025.
Any violation, failed goal or missed check-in resets S to 0. Trust is capped at 1.00.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

__all__ = ["recovery_for_streak_day", "is_clean_day", "StreakResult", "advance"]

TRUST_CAP = Decimal("1.00")


def recovery_for_streak_day(day: int) -> Decimal:
    """Recovery earned on the `day`-th consecutive clean day (1-based)."""
    if isinstance(day, bool) or not isinstance(day, int) or day < 1:
        raise ValueError("day must be a positive int")
    if day <= 3:
        return Decimal("0.008")
    if day <= 7:
        return Decimal("0.015")
    return Decimal("0.025")


def is_clean_day(*, violations: int, goals_assigned: int, goals_met: int, check_ins_due: int, check_ins_ok: int) -> bool:
    """All three conditions must hold. A day with nothing assigned and nothing due is only clean if there was no
    violation (vacuous truth); whether such a day should earn recovery is an open owner question."""
    values = (violations, goals_assigned, goals_met, check_ins_due, check_ins_ok)
    if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in values):
        raise ValueError("counts must be non-negative ints")
    if goals_met > goals_assigned or check_ins_ok > check_ins_due:
        raise ValueError("met / ok cannot exceed assigned / due")
    return violations == 0 and goals_met == goals_assigned and check_ins_ok == check_ins_due


@dataclass(frozen=True)
class StreakResult:
    streak: int
    trust: Decimal
    delta: Decimal


def advance(streak: int, trust: float | Decimal, *, clean: bool) -> StreakResult:
    """One day's step. A dirty day resets the streak and changes no Trust here (penalties are separate evidence)."""
    if isinstance(streak, bool) or not isinstance(streak, int) or streak < 0:
        raise ValueError("streak must be a non-negative int")
    t = trust if isinstance(trust, Decimal) else Decimal(str(trust))
    if not Decimal(0) <= t <= TRUST_CAP:
        raise ValueError("trust must be within [0, 1]")
    if not clean:
        return StreakResult(streak=0, trust=t, delta=Decimal(0))
    day = streak + 1
    new = min(TRUST_CAP, t + recovery_for_streak_day(day))
    return StreakResult(streak=day, trust=new, delta=new - t)
