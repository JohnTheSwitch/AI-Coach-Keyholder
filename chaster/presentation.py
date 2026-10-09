"""
chaster/presentation.py

Pure display helpers for raw `LockForWearer` dicts, shared by the
`chaster status` command (application/service.py) and the read-only LLM
tool (chaster/llm_tools.py) so both describe a lock identically.

Display only: nothing here makes or gates a decision. `status` is shown
as Chaster's raw string; `endDate` is the one confirmed end-time field.
Time is always supplied by the caller (`now`), never read from the clock.
"""

from __future__ import annotations

from datetime import datetime

from infrastructure.time_format import parse_iso

__all__ = ["lock_title", "lock_status_text", "lock_time_remaining"]


def lock_title(lock: dict) -> str:
    title = lock.get("title")
    return title if isinstance(title, str) and title.strip() else "(untitled)"


def lock_status_text(lock: dict) -> str:
    """The real, raw status string exactly as Chaster reported it --
    never mapped to an enum. This project has confirmed `status`'s
    field name and type from a real response, but never that every
    value matches a documented set; displaying the raw string sidesteps
    that gap since no decision is made from it."""
    status = lock.get("status")
    return status if isinstance(status, str) and status.strip() else "unknown"


def lock_time_remaining(lock: dict, *, now: datetime) -> str:
    """`endDate` is the confirmed top-level field (NOT `endAt`, which
    has never been confirmed). Missing, non-string or unparseable
    values fall back to "Unlimited / Permanent" rather than raising --
    a formatting detail is not a reason to fail the whole reply."""
    raw_end_date = lock.get("endDate")
    if not isinstance(raw_end_date, str) or not raw_end_date.strip():
        return "Unlimited / Permanent"
    try:
        end_date = parse_iso(raw_end_date)
    except ValueError:
        return "Unlimited / Permanent"

    remaining = end_date - now
    if remaining.total_seconds() <= 0:
        return "Expired"

    total_minutes = int(remaining.total_seconds() // 60)
    days, rem_minutes = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(rem_minutes, 60)
    return f"{days}d {hours}h {minutes}m"
