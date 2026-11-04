"""
supervision/escalation.py

Escalation of repeated check-in non-compliance (v0.39 Phase 3).

Owner rule (revised 2026-11-03, Phase J2): three CONSECUTIVE missed check-ins, or three missed within any rolling 48 hours, lower Trust and
issue a WARNING, but never start a Penalty Window by themselves. The Trust drop comes from the per-miss `check_in.missed` evidence (a growing
weight for consecutive misses). This module registers one low-severity Incident (rule group `CHECKIN_REPEATED_NONCOMPLIANCE`) that stays
UNCONFIRMED, records the Warning (event `check_in.warning_issued`, a DM to the wearer) and waits. Only the keyholder's decision
(`EscalationDecisions`, CLI) can confirm it; the Penalty Engine then resolves it like any other confirmed Incident. Nothing here confirms
an Incident automatically, and this module never starts a penalty itself.

Counting: only CONCLUDED check-ins matter (answered, answered_uncertain, insufficient, missed); skipped,
undeliverable and needs_review prompts are never a miss. Missed check-ins up to the last escalation are
"covered" and never counted again, so each escalation uses up its misses. Idempotent and crash-safe
through the `check_in_escalations` ledger (UNIQUE per triggering check-in). No message text is stored here.
"""

from __future__ import annotations

import hmac
import logging
import uuid
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Callable

from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.outbox import DomainEvent, write_event
from infrastructure.time_format import iso as _iso
from infrastructure.time_format import parse_iso as _parse_iso

logger = logging.getLogger("ai_coach_keyholder.supervision.escalation")

__all__ = ["CheckInEscalation", "EscalationDecisions", "EscalationOutcome", "RULE_GROUP_ID", "ROLLING_WINDOW", "CONSECUTIVE_MISSES",
           "WINDOW_MISSES", "WARNING_TEXT"]

RULE_GROUP_ID = "CHECKIN_REPEATED_NONCOMPLIANCE"
CONSECUTIVE_MISSES = 3
WINDOW_MISSES = 3
ROLLING_WINDOW = timedelta(hours=48)
_CONCLUDED = ("answered", "answered_uncertain", "insufficient", "missed")
WARNING_TEXT = ("Warning: three check-ins were missed. No Penalty Window has been started, and none starts unless your keyholder confirms "
                "this. If something is making check-ins hard right now, tell me and we can look at it.")


class CheckInEscalation:
    def __init__(
        self,
        db_path: str | Path,
        *,
        core: CoreDatabase | None = None,
        user_id: str,
        register_incident: Callable[[str, datetime], str],
        notify: Callable[[str], bool] | None = None,
    ) -> None:
        """`register_incident(description, now) -> incident_id` is supplied by the composition root (it wraps the Trust Manager).
        `notify(text) -> delivered` sends the Warning to the wearer; an undelivered Warning is retried on the next pass."""
        self.db_path = Path(db_path)
        self._core = core if core is not None else CoreDatabase(self.db_path)
        self._user = user_id
        self._register = register_incident
        self._notify = notify

    def run(self, now: datetime) -> None:
        self._deliver_warnings(now)
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
                "state, warned_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'registered', ?, ?, ?)",
                (str(uuid.uuid4()), self._user, trigger_id, trigger_due, reason, incident_id, _iso(now), _iso(now), _iso(now)),
            )
            write_event(tx, DomainEvent(event_type="check_in.warning_issued", source_module="supervision",
                                        payload={"incident_id": incident_id, "reason": reason}, occurred_at=now))
            return True

        if apply_transition(self._core, write=write):
            self._deliver_warnings(now)

    # ------------------------------------------------------------------ internals

    def _deliver_warnings(self, now: datetime) -> None:
        if self._notify is None:
            return
        with self._core.transaction() as tx:
            rows = tx.fetch_all("SELECT id FROM check_in_escalations WHERE user_id = ? AND warned_at IS NOT NULL "
                                "AND warning_notified_at IS NULL", (self._user,))
        for row in rows:
            try:
                delivered = self._notify(WARNING_TEXT)
            except Exception:
                logger.exception("Sending the check-in warning failed; will retry.")
                continue
            if not delivered:
                continue

            def write(tx: Transaction, _s: object, _id=row["id"]) -> None:
                tx.execute("UPDATE check_in_escalations SET warning_notified_at = ?, updated_at = ? WHERE id = ?",
                           (_iso(now), _iso(now), _id))

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


class EscalationOutcome(StrEnum):
    CONFIRMED = "confirmed"
    DISMISSED = "dismissed"
    OWNER_NOT_CONFIGURED = "owner_not_configured"
    NOT_OWNER = "not_owner"
    NOT_CONFIRMED = "not_confirmed"
    NOT_FOUND = "not_found"
    ALREADY_DECIDED = "already_decided"
    FAILED = "failed"


class EscalationDecisions:
    """The keyholder's manual decision on an escalation (CLI only). `confirm` advances the Incident to CONFIRMED (source keyholder_review), and
    the Penalty Engine resolves it as a Warning or a Penalty Window; `dismiss` leaves it UNCONFIRMED for good."""

    def __init__(self, db_path: str | Path, *, core: CoreDatabase | None = None, owner_user_id: str | None,
                 confirm_incident: Callable[[str, str, datetime], None]) -> None:
        self._core = core if core is not None else CoreDatabase(Path(db_path))
        self._owner = owner_user_id.strip() if owner_user_id and owner_user_id.strip() else None
        self._confirm = confirm_incident

    def authorize(self, actor: str) -> EscalationOutcome | None:
        if self._owner is None:
            return EscalationOutcome.OWNER_NOT_CONFIGURED
        if not isinstance(actor, str) or not hmac.compare_digest(actor.encode("utf-8"), self._owner.encode("utf-8")):
            return EscalationOutcome.NOT_OWNER
        return None

    def rows(self) -> list[dict]:
        with self._core.transaction() as tx:
            return [dict(r) for r in tx.fetch_all(
                "SELECT incident_id, reason, state, warned_at, keyholder_decision, created_at FROM check_in_escalations "
                "ORDER BY created_at DESC, rowid DESC")]

    def decide(self, *, actor: str, incident_id: str, confirm: bool, confirmed: bool, now: datetime) -> EscalationOutcome:
        refusal = self.authorize(actor)
        if refusal is not None:
            return refusal
        if confirmed is not True:
            return EscalationOutcome.NOT_CONFIRMED
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT id, reason, keyholder_decision FROM check_in_escalations WHERE incident_id = ?", (incident_id,))
        if row is None:
            return EscalationOutcome.NOT_FOUND
        if row["keyholder_decision"] is not None:
            return EscalationOutcome.ALREADY_DECIDED
        if confirm:
            try:
                self._confirm(incident_id, f"The keyholder reviewed the missed check-ins ({row['reason']}) and confirmed this Incident.", now)
            except Exception:
                logger.exception("Confirming the check-in escalation failed.")
                return EscalationOutcome.FAILED
        decision = EscalationOutcome.CONFIRMED if confirm else EscalationOutcome.DISMISSED

        def write(tx: Transaction, _s: object) -> None:
            tx.execute("UPDATE check_in_escalations SET keyholder_decision = ?, decided_at = ?, updated_at = ?, "
                       "state = CASE WHEN ? THEN 'confirmed' ELSE state END WHERE id = ? AND keyholder_decision IS NULL",
                       (decision.value, _iso(now), _iso(now), 1 if confirm else 0, row["id"]))

        apply_transition(self._core, write=write)
        return decision
