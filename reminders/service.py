"""
reminders/service.py

Standing reminders shown in full at most once a week each (owner, 2026-11-16). See README.md.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from infrastructure.database import Database as CoreDatabase, Transaction, apply_transition
from infrastructure.outbox import DomainEvent, write_event
from infrastructure.time_format import parse_iso

__all__ = ["WEEK", "due_in_transaction", "mark_in_transaction", "take"]

logger = logging.getLogger("ai_coach_keyholder.reminders")

WEEK = timedelta(days=7)


def _iso(at: datetime) -> str:
    return at.isoformat().replace("+00:00", "Z")


def due_in_transaction(tx: Transaction, user_id: str, key: str, now: datetime) -> bool:
    try:
        row = tx.fetch_one("SELECT shown_at FROM reminder_log WHERE user_id = ? AND key = ?", (user_id, key))
        return row is None or now - parse_iso(row["shown_at"]) >= WEEK
    except Exception:
        logger.exception("A reminder's last showing could not be read; it is shown.")
        return True


def mark_in_transaction(tx: Transaction, user_id: str, key: str, now: datetime) -> None:
    tx.execute("INSERT INTO reminder_log (user_id, key, shown_at) VALUES (?, ?, ?) "
               "ON CONFLICT(user_id, key) DO UPDATE SET shown_at = excluded.shown_at", (user_id, key, _iso(now)))
    write_event(tx, DomainEvent(event_type="reminders.shown", source_module="reminders", occurred_at=now, payload={"key": key}))


def take(core: CoreDatabase, user_id: str, key: str, now: datetime) -> bool:
    """True (and marked as shown) when the reminder is due; for callers without an open transaction."""
    def write(tx: Transaction, _s: object) -> bool:
        if not due_in_transaction(tx, user_id, key, now):
            return False
        mark_in_transaction(tx, user_id, key, now)
        return True

    try:
        return bool(apply_transition(core, write=write))
    except Exception:
        logger.exception("A reminder could not be marked; it is shown.")
        return True
