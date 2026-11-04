"""
rules_engine/limits.py

Hard safety limits. These live in code, OUTSIDE the ruleset: no ruleset can
raise them (validation rejects any rule that could exceed the single-action
limit, and `apply_limits` enforces the rest at evaluation time).
Values are the owner-approved Phase 2 limits.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from rules_engine.models import ActionType, RecentAction

__all__ = [
    "ROLLING_WINDOW",
    "BARRIER_MAX_ACTIONS_PER_WINDOW",
    "BARRIER_COOLDOWN",
    "BARRIER_ACTIONS",
    "LOCK_DATA_MAX_AGE",
    "apply_limits",
]

ROLLING_WINDOW = timedelta(hours=24)
LOCK_DATA_MAX_AGE = timedelta(minutes=15)

# The barrier pool (freeze / unfreeze). There is no time-adjustment pool: the system never changes a lock's duration.
BARRIER_ACTIONS = (ActionType.FREEZE, ActionType.UNFREEZE)
BARRIER_MAX_ACTIONS_PER_WINDOW = 2
BARRIER_COOLDOWN = timedelta(hours=4)


def apply_limits(
    action: ActionType, hours: int, recent: tuple[RecentAction, ...], now: datetime,
) -> tuple[int, str | None]:
    """
    Returns (0, refusal_reason); a refusal means NO_ACTION. `hours` is always 0 and stays in the signature only because decisions keep their
    `hours` column. Raising the barrier only restrains: it is never blocked by the pool or the cooldown and does not use up the release
    budget (otherwise a release followed by its re-freeze would block the next day's release). The pool limits RELEASES (unfreezes).
    """
    if action is ActionType.FREEZE:
        return 0, None
    releases = tuple(a for a in recent if a.action_type is ActionType.UNFREEZE)
    window_start = now - ROLLING_WINDOW
    in_window = [a for a in releases if window_start <= a.created_at <= now]
    if len(in_window) >= BARRIER_MAX_ACTIONS_PER_WINDOW:
        return 0, f"action limit reached ({BARRIER_MAX_ACTIONS_PER_WINDOW} per 24 hours)"
    if releases and now - max(a.created_at for a in releases) < BARRIER_COOLDOWN:
        return 0, "cooldown active (minimum 4 hours between actions)"
    return 0, None
