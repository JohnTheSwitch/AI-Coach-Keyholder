"""
supervision/escalation.py

Escalation of repeated check-in non-compliance (v0.39 Phase 3).

Owner rule: three CONSECUTIVE missed check-ins, or three missed within any rolling 48 hours, register one
low-severity Incident (rule group `CHECKIN_REPEATED_NONCOMPLIANCE`). It goes through the normal Trust
Manager incident path (registered, then confirmed from objective system records), from which the Penalty
Engine consumes it exactly like any other confirmed Incident; this module never starts a penalty itself.

Counting: only CONCLUDED check-ins matter (answered, answered_uncertain, insufficient, missed); skipped,
undeliverable and needs_review prompts are never a miss. Missed check-ins up to the last escalation are
"covered" and never counted again, so each escalation uses up its misses. Idempotent and crash-safe
through the `check_in_escalations` ledger (UNIQUE per triggering check-in). No message text is stored here.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.time_format import iso as _iso
from infrastructure.time_format import parse_iso as _parse_iso

logger = logging.getLogger("ai_coach_keyholder.supervision.escalation")

__all__ = ["CheckInEscalation", "RULE_GROUP_ID", "ROLLING_WINDOW", "CONSECUTIVE_MISSES", "WINDOW_MISSES"]

RULE_GROUP_ID = "CHECKIN_REPEATED_NONCOMPLIANCE"
CONSECUTIVE_MISSES = 3
WINDOW_MISSES = 3
ROLLING_WINDOW = timedelta(hours=48)
_CONCLUDED = ("answered", "answered_uncertain", "insufficient", "missed")


class CheckInEscalation:
    def __init__(
        self,
        db_path: str | Path,
        *,
        core: CoreDatabase | None = None,
        user_id: str,
        register_incident: Callable[[str, datetime], str],
        confirm_incident: Callable[[str, str, datetime], None],
    ) -> None:
        """`register_incident(description, now) -> incident_id` and `confirm_incident(incident_id, evidence, now)`
        are supplied by the composition root (they wrap the Trust Manager)."""
        self.db_path = Path(db_path)
        self._core = core if core is not None else CoreDatabase(self.db_path)
        self._user = user_id
        self._register = register_incident
        self._confirm = confirm_incident

    def run(self, now: datetime) -> None:
        self._finish_registered(now)
        found = self._detect(now)
        if found is None:
            return
        trigger_id, trigger_due, reason = found
        description = ("Repeated check-in non-compliance: "
                       + ("three consecutive check-ins were missed." if reason == "consecutive"
                          else "three check-ins were missed within 48 hours."))
        incident_id = self._register(description, now)

        def write(tx: Transaction, _s: object) -> bool:
            if tx.fetch_one("SELECT 1 FROM check_in_escalations WHERE trigger_check_in_id = ?", (trigger_id,)):
                return False
            tx.execute(
                "INSERT INTO check_in_escalations (id, user_id, trigger_check_in_id, trigger_due_at, reason, incident_id, "
                "state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'registered', ?, ?)",
                (str(uuid.uuid4()), self._user, trigger_id, trigger_due, reason, incident_id, _iso(now), _iso(now)),
            )
            return True

        if apply_transition(self._core, write=write):
            self._finish_registered(now)

    # ------------------------------------------------------------------ internals

    def _finish_registered(self, now: datetime) -> None:
        with self._core.transaction() as tx:
            rows = tx.fetch_all(
                "SELECT id, incident_id, reason FROM check_in_escalations WHERE user_id = ? AND state = 'registered'",
                (self._user,))
        for row in rows:
            try:
                self._confirm(row["incident_id"], f"Objective record of missed check-ins ({row['reason']}).", now)
            except Exception:
                logger.exception("Confirming the check-in escalation incident failed; will retry.")
                continue

            def write(tx: Transaction, _s: object, _id=row["id"]) -> None:
                tx.execute("UPDATE check_in_escalations SET state = 'confirmed', updated_at = ? "
                           "WHERE id = ? AND state = 'registered'", (_iso(now), _id))

            apply_transition(self._core, write=write)

    def _detect(self, now: datetime) -> tuple[str, str, str] | None:
        with self._core.transaction() as tx:
            covered = tx.fetch_one(
                "SELECT MAX(trigger_due_at) AS m FROM check_in_escalations WHERE user_id = ?", (self._user,))["m"]
            marks = ",".join("?" for _ in _CONCLUDED)
            rows = tx.fetch_all(
                f"SELECT id, status, due_at FROM check_ins WHERE user_id = ? AND status IN ({marks}) "
                "ORDER BY due_at DESC LIMIT 20", (self._user, *_CONCLUDED))
        cutoff = None if covered is None else _parse_iso(covered)
        concluded = [(r["id"], r["status"], _parse_iso(r["due_at"])) for r in rows]
        # Consecutive: the most recent concluded check-ins are all uncovered misses.
        head = concluded[:CONSECUTIVE_MISSES]
        if len(head) == CONSECUTIVE_MISSES and all(
            c[1] == "missed" and (cutoff is None or c[2] > cutoff) for c in head
        ):
            return head[0][0], _iso(head[0][2]), "consecutive"
        recent = [c for c in concluded
                  if c[1] == "missed" and c[2] >= now - ROLLING_WINDOW and (cutoff is None or c[2] > cutoff)]
        if len(recent) >= WINDOW_MISSES:
            return recent[0][0], _iso(recent[0][2]), "rolling_48h"
        return None
