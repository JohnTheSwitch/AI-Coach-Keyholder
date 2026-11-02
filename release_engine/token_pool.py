"""
release_engine/token_pool.py

The Token Progress Pool (v0.39, section 11.8). PURE arithmetic: no I/O, no clock, not wired yet.

Once per local day (midnight, APP_TIMEZONE) the day's progress is added to the pool:

    P_daily = (0.75 * completed / total + 0.25 * trust) * 0.286

so 100% goal completion at Trust 1.0 gives about 2.0 tokens per 7 days. Whenever the pool reaches 1.0, one token is
earned (`token_pool.earned`) and 1.0 is subtracted. Decimal arithmetic keeps the pool exact (no float drift).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal

__all__ = ["GOAL_WEIGHT", "TRUST_WEIGHT", "DAILY_SCALE", "TOKEN_SOURCE", "PoolDay", "daily_progress", "apply_day"]

GOAL_WEIGHT = Decimal("0.75")
TRUST_WEIGHT = Decimal("0.25")
DAILY_SCALE = Decimal("0.286")
TOKEN_SOURCE = "token_pool.earned"
_ONE = Decimal("1")
_QUANT = Decimal("0.000001")


@dataclass(frozen=True)
class PoolDay:
    progress: Decimal          # P_daily
    pool_after: Decimal        # pool left after tokens were paid out
    tokens_earned: int


def _dec(value: float | int | str | Decimal) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def daily_progress(completed: int, total: int, trust: float | Decimal) -> Decimal:
    """A day without assigned goals (`total == 0`, then `completed` must be 0) counts as a goal ratio of 1.0 (owner rule)."""
    if isinstance(completed, bool) or isinstance(total, bool) or not isinstance(completed, int) or not isinstance(total, int):
        raise TypeError("completed and total must be ints")
    if total < 0:
        raise ValueError("total must not be negative")
    if not 0 <= completed <= total:
        raise ValueError("completed must be within 0..total")
    t = _dec(trust)
    if not Decimal(0) <= t <= _ONE:
        raise ValueError("trust must be within [0, 1]")
    ratio = Decimal(1) if total == 0 else Decimal(completed) / Decimal(total)
    return ((GOAL_WEIGHT * ratio + TRUST_WEIGHT * t) * DAILY_SCALE).quantize(_QUANT, rounding=ROUND_HALF_EVEN)


def apply_day(pool: float | Decimal, completed: int, total: int, trust: float | Decimal,
              threshold: float | Decimal = _ONE) -> PoolDay:
    """One day's progress. A token is paid each time the pool reaches `threshold` (1.0 by default; Phase F4 Progressive Conditioning uses
    base(mode) * factor) and the threshold is subtracted. A stored pool above a lowered threshold simply pays out."""
    p = _dec(pool)
    t = _dec(threshold)
    if p < 0:
        raise ValueError("a stored pool is never negative")
    if t <= 0:
        raise ValueError("threshold must be positive")
    progress = daily_progress(completed, total, trust)
    p += progress
    earned = 0
    while p >= t:
        p -= t
        earned += 1
    return PoolDay(progress=progress, pool_after=p, tokens_earned=earned)
