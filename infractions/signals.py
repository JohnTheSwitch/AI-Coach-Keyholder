"""
infractions/signals.py

The one path from a detector to an Incident, for every detector:

    detected -> `waiting` (the wearer can say it was a health or safety need) -> `open` (an UNCONFIRMED Incident and the keyholder's card) -> confirmed | dismissed

Nothing here confirms by itself (philosophy 2.12, 3.2): only the keyholder's button reaches `confirm_incident`. An unconfirmed Incident has no consumer, so
until that press there is no Trust effect, no Warning and no Penalty Window. Saying "health" clears a signal with no questions asked (3.9), and clears it for good
as long as the keyholder has not decided. A detection that came too soon for an excuse is therefore never an Incident at all, which is what keeps Mandatory
Health Access and the Emergency Override outside this path instead of merely undoing them later.
"""

from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Callable
from uuid import uuid4

from application.models import ActionSpec
from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.time_format import iso as _iso
from infrastructure.time_format import parse_iso
from infractions.excuses import Excuses
from notifications.queue import enqueue_in_transaction
from trust_manager.models import BreachDirectness, EvidenceConfidenceLevel, ImpactLevel, IncidentEvidence, IntentAssessment, RepetitionEvidence

logger = logging.getLogger("ai_coach_keyholder.infractions")

__all__ = ["SignalKind", "InfractionSignals", "Decision", "GRACE", "RULE_GROUPS", "EVIDENCE", "Signal"]

GRACE = timedelta(hours=12)                 # how long the wearer has to say "health" before anything is recorded against them

_MINOR_EVIDENCE = IncidentEvidence(
    actual_or_potential_impact=ImpactLevel.LOW, rule_breach_directness=BreachDirectness.PARTIAL,
    repetition=RepetitionEvidence(same_rule_confirmed_count=0, evaluation_window_days=30),
    evidence_confidence=EvidenceConfidenceLevel.MEDIUM, intentionality=IntentAssessment.UNCLEAR)
_SEVERE_EVIDENCE = IncidentEvidence(
    actual_or_potential_impact=ImpactLevel.HIGH, rule_breach_directness=BreachDirectness.DIRECT,
    repetition=RepetitionEvidence(same_rule_confirmed_count=0, evaluation_window_days=30),
    evidence_confidence=EvidenceConfidenceLevel.MEDIUM, intentionality=IntentAssessment.UNCLEAR)


class SignalKind(StrEnum):
    HYGIENE_DURATION = "hygiene_duration"
    HYGIENE_FREQUENCY = "hygiene_frequency"
    LOCK_CLAIM = "lock_claim"
    EXEMPTION_OVERRUN = "exemption_overrun"


RULE_GROUPS = {SignalKind.HYGIENE_DURATION: "HYGIENE_MISUSE", SignalKind.HYGIENE_FREQUENCY: "HYGIENE_MISUSE", SignalKind.LOCK_CLAIM: "EPISTEMIC_MISREPRESENTATION",
               SignalKind.EXEMPTION_OVERRUN: "HYGIENE_MISUSE"}
# Proportionate (3.8): a break that ran long is minor, so a first one ends in a Warning; a false lock report is serious.
EVIDENCE = {SignalKind.HYGIENE_DURATION: _MINOR_EVIDENCE, SignalKind.HYGIENE_FREQUENCY: _MINOR_EVIDENCE, SignalKind.LOCK_CLAIM: _SEVERE_EVIDENCE,
            SignalKind.EXEMPTION_OVERRUN: _MINOR_EVIDENCE}


class Decision(StrEnum):
    CONFIRMED = "confirmed"
    DISMISSED = "dismissed"
    NOT_OWNER = "not_owner"
    NOT_OPEN = "not_open"
    FAILED = "failed"


@dataclass(frozen=True, kw_only=True)
class Signal:
    id: str
    user_id: str
    kind: SignalKind
    subject_id: str
    trust_domain: str
    detail: str
    status: str
    review_after: datetime
    incident_id: str


def _signal(row) -> Signal:
    return Signal(id=row["id"], user_id=row["user_id"], kind=SignalKind(row["kind"]), subject_id=row["subject_id"], trust_domain=row["trust_domain"],
                  detail=row["detail"], status=row["status"], review_after=parse_iso(row["review_after"]), incident_id=row["incident_id"])


class InfractionSignals:
    def __init__(self, db_path: str | Path | None = None, *, core: CoreDatabase | None = None, owner_user_id: str | None, excuses: Excuses,
                 register_incident: Callable[[Signal, datetime], None], confirm_incident: Callable[[str, str, datetime], None]) -> None:
        """`register_incident(signal, now)` files the UNCONFIRMED Incident under `signal.incident_id`, which is chosen in advance so a retry cannot duplicate it.
        `confirm_incident(incident_id, evidence, now)` advances it to CONFIRMED (source keyholder_review)."""
        self._core = core if core is not None else CoreDatabase(Path(db_path))
        self._owner = owner_user_id.strip() if owner_user_id and owner_user_id.strip() else None
        self._excuses = excuses
        self._register = register_incident
        self._confirm = confirm_incident

    def raise_in_transaction(self, tx: Transaction, *, user_id: str, kind: SignalKind, subject_id: str, trust_domain: str, detail: str, wearer_text: str,
                             now: datetime) -> bool:
        """False when this subject already raised this kind. The wearer's card goes out with the signal, so the grace period is never spent in silence."""
        signal_id = str(uuid4())
        cursor = tx.execute(
            "INSERT OR IGNORE INTO infraction_signals (id, user_id, kind, subject_id, trust_domain, detail, status, review_after, incident_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'waiting', ?, ?, ?)",
            (signal_id, user_id, kind.value, subject_id, trust_domain, detail, _iso(now + GRACE), str(uuid4()), _iso(now)))
        if cursor.rowcount != 1:
            return False
        enqueue_in_transaction(tx, user_id=user_id, kind="infraction_wearer", dedupe_key=f"infraction:wearer:{signal_id}", text=wearer_text, now=now,
                               actions=(ActionSpec(action_id=f"infraction:health:{signal_id}", label="It was a health or safety need", style="success"),))
        return True

    def advance(self, now: datetime, *, window_of: Callable[[Signal], tuple[datetime, datetime]]) -> int:
        """Waiting signals whose grace ended: excused ones clear, the rest become an UNCONFIRMED Incident and the keyholder's card. `window_of` gives the time a
        signal covers, for the excuse check."""
        with self._core.transaction() as tx:
            rows = tx.fetch_all("SELECT * FROM infraction_signals WHERE status = 'waiting' AND review_after <= ? ORDER BY created_at, rowid", (_iso(now),))
        opened = 0
        for signal in map(_signal, rows):
            try:
                start, end = window_of(signal)
                # An overrun's own exemption is what was overrun: it cannot also excuse it.
                reason = self._excuses.reason(signal.user_id, start, end, ignore_exemption=signal.subject_id if signal.kind is SignalKind.EXEMPTION_OVERRUN else None)
                if reason is not None:
                    self._clear(signal.id, reason, now, statuses=("waiting",))
                    continue
                self._file(signal, now)
                opened += 1
            except Exception:
                logger.exception("Could not advance an infraction signal; it stays waiting.")
        return opened

    def clear_as_health(self, *, user_id: str, signal_id: str, now: datetime) -> bool:
        """The wearer's word is enough (3.9). The signal must be theirs and the keyholder must not have decided yet."""
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT * FROM infraction_signals WHERE id = ?", (signal_id,))
        if row is None or row["user_id"] != user_id:
            return False
        return self._clear(signal_id, "wearer_health", now, statuses=("waiting", "open"))

    def decide(self, *, actor: str, signal_id: str, confirm: bool, now: datetime) -> Decision:
        if self._owner is None or not isinstance(actor, str) or not hmac.compare_digest(actor.encode("utf-8"), self._owner.encode("utf-8")):
            return Decision.NOT_OWNER
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT * FROM infraction_signals WHERE id = ? AND status = 'open'", (signal_id,))
        if row is None:
            return Decision.NOT_OPEN
        signal = _signal(row)
        decision = Decision.CONFIRMED if confirm else Decision.DISMISSED
        if confirm:
            try:
                self._confirm(signal.incident_id, f"The keyholder reviewed a detector finding and confirmed it: {signal.detail}", now)
            except Exception:
                logger.exception("Confirming the incident of signal %s failed.", signal_id)
                return Decision.FAILED

        def write(tx: Transaction, _s: object) -> None:
            tx.execute("UPDATE infraction_signals SET status = ?, decided_at = ? WHERE id = ? AND status = 'open'", (decision.value, _iso(now), signal_id))

        apply_transition(self._core, write=write)
        return decision

    def rows(self) -> list[dict]:
        with self._core.transaction() as tx:
            return [dict(r) for r in tx.fetch_all("SELECT id, kind, status, created_at, decided_at, cleared_reason FROM infraction_signals ORDER BY created_at DESC, rowid DESC")]

    # ------------------------------------------------------------------ internals

    def _clear(self, signal_id: str, reason: str, now: datetime, *, statuses: tuple[str, ...]) -> bool:
        marks = ",".join("?" for _ in statuses)

        def write(tx: Transaction, _s: object) -> bool:
            cursor = tx.execute(f"UPDATE infraction_signals SET status = 'cleared', cleared_reason = ?, decided_at = ? WHERE id = ? AND status IN ({marks})",
                                (reason, _iso(now), signal_id, *statuses))
            return cursor.rowcount == 1

        return apply_transition(self._core, write=write)

    def _file(self, signal: Signal, now: datetime) -> None:
        with self._core.transaction() as tx:
            exists = tx.fetch_one("SELECT 1 FROM incidents WHERE id = ?", (signal.incident_id,))
        if exists is None:
            self._register(signal, now)                 # outside the write below: Trust Manager opens its own transaction

        def write(tx: Transaction, _s: object) -> None:
            cursor = tx.execute("UPDATE infraction_signals SET status = 'open', incident_registered_at = ? WHERE id = ? AND status = 'waiting'", (_iso(now), signal.id))
            if cursor.rowcount == 1 and self._owner is not None:
                enqueue_in_transaction(
                    tx, user_id=self._owner, kind="infraction_keyholder", dedupe_key=f"infraction:keyholder:{signal.id}", now=now,
                    text=f"For your review: {signal.detail} Nothing has happened yet: it stays unconfirmed until you decide.",
                    actions=(ActionSpec(action_id=f"infraction:confirm:{signal.id}", label="Confirm Incident", style="danger"),
                             ActionSpec(action_id=f"infraction:dismiss:{signal.id}", label="Dismiss", style="secondary")))

        apply_transition(self._core, write=write)
