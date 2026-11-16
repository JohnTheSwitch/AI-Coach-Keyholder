"""
system/graduation.py

Graduating from the beginner level (owner, 2026-11-16: "beginner, standard and advanced"). After GRADUATE_AFTER at the beginner level with
no confirmed severe incident in that time, the wearer is asked once (per stay at Beginner) whether to move up; moving up is their own
`mode request standard` (a 24-hour wait and a second confirmation, as every mode change). Nothing here changes the mode.

Composition layer: it only reads the operating mode and the confirmed incidents of ONE database and queues one notice.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.time_format import iso as _iso
from infrastructure.time_format import parse_iso as _parse_iso
from notifications.queue import enqueue_in_transaction
from penalty_engine.duration import ViolationSeverity
from penalty_engine.violations_catalog import get as violation

__all__ = ["GRADUATE_AFTER", "GRADUATION_TEXT", "graduation_tick"]

logger = logging.getLogger("ai_coach_keyholder.system.graduation")

GRADUATE_AFTER = timedelta(days=28)
GRADUATION_TEXT = ("Four weeks at the beginner level without a serious slip. You're ready for more: send `mode request standard` to move up "
                   "(a 24-hour wait, then `mode confirm`). Standard means tokens are harder to earn, the lines double and every feature can be on.")


def _severe(rule_group_id: str | None) -> bool:
    try:
        return violation(rule_group_id).severity is ViolationSeverity.SEVERE
    except KeyError:
        return False


def graduation_tick(core: CoreDatabase, user_id: str, now: datetime) -> bool:
    """True when the question was queued now. Never raises."""
    try:
        def write(tx: Transaction, _s: object) -> bool:
            row = tx.fetch_one("SELECT current_mode, mode_activated_at FROM operating_mode_state WHERE id = 1")
            if row is None or row["current_mode"] != "beginner":
                return False
            since = _parse_iso(row["mode_activated_at"])
            if now - since < GRADUATE_AFTER:
                return False
            incidents = tx.fetch_all("SELECT rule_group_id FROM incidents WHERE created_at >= ? AND confirmation = 'confirmed'",
                                     (_iso(now - GRADUATE_AFTER),))
            if any(_severe(r["rule_group_id"]) for r in incidents):
                return False
            return enqueue_in_transaction(tx, user_id=user_id, kind="graduation", dedupe_key=f"graduation:{row['mode_activated_at']}",
                                          text=GRADUATION_TEXT, now=now)

        return bool(apply_transition(core, write=write))
    except Exception:
        logger.exception("The graduation check failed; it is tried again next cycle.")
        return False
