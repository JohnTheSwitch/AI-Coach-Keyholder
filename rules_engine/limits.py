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
    "HOURS_UNIT",
    "MAX_SINGLE_ACTION_HOURS",
    "MAX_ADDED_HOURS_PER_WINDOW",
    "MAX_REMOVED_HOURS_PER_WINDOW",
    "MAX_ACTIONS_PER_WINDOW",
    "ROLLING_WINDOW",
    "ACTION_COOLDOWN",
    "LOCK_DATA_MAX_AGE",
    "apply_limits",
]

HOURS_UNIT = 1                                   # time changes are whole hours
MAX_SINGLE_ACTION_HOURS = 6
MAX_ADDED_HOURS_PER_WINDOW = 12
MAX_REMOVED_HOURS_PER_WINDOW = 6
MAX_ACTIONS_PER_WINDOW = 2
ROLLING_WINDOW = timedelta(hours=24)
ACTION_COOLDOWN = timedelta(hours=4)
LOCK_DATA_MAX_AGE = timedelta(minutes=15)


def apply_limits(
    action: ActionType, hours: int, recent: tuple[RecentAction, ...], now: datetime,
) -> tuple[int, str | None]:
    """
    Returns (allowed_hours, refusal_reason). A refusal means NO_ACTION.
    For ADD_TIME / REMOVE_TIME the amount is clamped down to what the
    rolling-window cap still allows (whole hours); if less than one hour
    remains, it is a refusal. FREEZE / UNFREEZE carry no hours but still
    count against the frequency and cooldown limits.
    """
    window_start = now - ROLLING_WINDOW
    in_window = [a for a in recent if window_start <= a.created_at <= now]

    if len(in_window) >= MAX_ACTIONS_PER_WINDOW:
        return 0, f"action limit reached ({MAX_ACTIONS_PER_WINDOW} per 24 hours)"

    if recent:
        last = max(a.created_at for a in recent)
        if now - last < ACTION_COOLDOWN:
            return 0, "cooldown active (minimum 4 hours between actions)"

    if action in (ActionType.FREEZE, ActionType.UNFREEZE):
        return 0, None

    if action == ActionType.ADD_TIME:
        used = sum(a.hours for a in in_window if a.action_type == ActionType.ADD_TIME)
        cap = MAX_ADDED_HOURS_PER_WINDOW
    else:
        used = sum(a.hours for a in in_window if a.action_type == ActionType.REMOVE_TIME)
        cap = MAX_REMOVED_HOURS_PER_WINDOW

    allowed = min(hours, MAX_SINGLE_ACTION_HOURS, cap - used)
    if allowed < HOURS_UNIT:
        return 0, "24-hour time cap reached"
    return allowed, None
