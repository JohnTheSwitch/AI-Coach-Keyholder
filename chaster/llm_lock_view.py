"""
chaster/llm_lock_view.py

Turns a raw Chaster `LockForWearer` dict into the small, clean dict the
model sees, so it never interprets raw dates/seconds or builds
time units itself.

Field provenance (this project's rule: never present an unconfirmed field
as fact):
- CONFIRMED on a real response: `title`, `status`, `endDate`.
- NOT yet confirmed: `isFrozen`, `canBeUnlocked`, `keyholder`. They are
  read only if present with the expected type; otherwise the view value
  is None ("unknown") and the prompt tells the model not to claim
  anything about it. Nothing is guessed or derived from other fields.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from chaster.english_text import format_time_english
from chaster.llm_tools import sanitize_text
from chaster.presentation import lock_status_text, lock_title
from infrastructure.time_format import parse_iso

__all__ = ["build_lock_view", "build_lock_views"]


def _optional_bool(lock: dict, key: str) -> bool | None:
    value = lock.get(key)
    return value if isinstance(value, bool) else None


def _end_date(lock: dict) -> datetime | None:
    raw = lock.get("endDate")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return parse_iso(raw)
    except ValueError:
        return None


def build_lock_view(lock: dict, *, now: datetime) -> dict[str, Any]:
    """Pure; `now` is supplied by the caller.

    `state` is the single resolved verdict, with FIXED precedence:
    frozen > expired > no_end_time > running. When `isFrozen` is true the
    view reports `state="frozen"`, no remaining time, `time_expired`
    false and `can_be_unlocked` null, so "frozen" can never be mistaken
    for "expired".

    `time_remaining_formatted` is a ready-to-quote English string
    (`2 days, 8 hours and 37 minutes`); it is None when the
    lock has expired or has no known end time -- see `time_expired` and
    `has_end_time`."""
    end = _end_date(lock)
    time_expired = end is not None and (end - now).total_seconds() <= 0
    formatted: str | None
    if end is None or time_expired:
        formatted = None
    else:
        formatted = format_time_english(int((end - now).total_seconds()))

    is_frozen = _optional_bool(lock, "isFrozen")
    if is_frozen is True:
        # A frozen lock is FROZEN, whatever its end date says. A frozen
        # lock whose countdown reads 0 is not "expired" and must never be
        # described as unlockable -- so the conflicting signals are
        # removed here, not left for a small model to reconcile.
        state = "frozen"
        formatted = None
        time_expired = False
    elif time_expired:
        state = "expired"
    elif end is None:
        state = "no_end_time"
    else:
        state = "running"

    keyholder = lock.get("keyholder")
    if "keyholder" not in lock:
        lock_type = "unknown"
    else:
        lock_type = "keyholder_lock" if keyholder is not None else "self_lock"
    return {
        "lock_name": sanitize_text(lock_title(lock)),
        "lock_type": lock_type,
        "state": state,
        "status": sanitize_text(lock_status_text(lock)),
        "is_frozen": is_frozen,
        "time_remaining_formatted": formatted,
        "time_expired": time_expired,
        "has_end_time": end is not None,
        "can_be_unlocked": None if state == "frozen" else _optional_bool(lock, "canBeUnlocked"),
        "is_keyholder_lock": (keyholder is not None) if "keyholder" in lock else None,
    }


def build_lock_views(
    locks: list[dict] | tuple[dict, ...], *, now: datetime, limit: int | None = None,
) -> list[dict[str, Any]]:
    """One independent view per lock, in the order Chaster returned them,
    numbered from 1 (`lock_number`) with the total (`lock_count`).

    Isolation guarantee: every view is built from its OWN dict only --
    no value (frozen flag, end date, type, title) is ever carried from
    one lock to the next, and the input dicts are never mutated. A lock
    that is not a dict is skipped rather than shifting the others."""
    usable = [lock for lock in locks if isinstance(lock, dict)]
    shown = usable if limit is None else usable[:limit]
    return [
        {"lock_number": index, "lock_count": len(usable), **build_lock_view(lock, now=now)}
        for index, lock in enumerate(shown, start=1)
    ]
