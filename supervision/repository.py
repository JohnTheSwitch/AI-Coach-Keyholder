"""
supervision/repository.py

ActiveSupervisionState (v0.39), derived from Trust recalculations.

- Enter: score < 0.40 with confidence >= 0.30.
- Exit: score >= 0.50 (confidence >= 0.30) for two CONSECUTIVE evaluations.
  A weak-confidence or sub-threshold evaluation resets the streak.
- Or exit by probation (owner, 2026-11-13): PROBATION_CLEAN_DAYS clean days in a row, each with at least one answered check-in
  (`daily_evaluation.completed`). A day with nothing due neither counts nor breaks the run; a paused (vacation) day neither.
- Hygiene and any other domain without daily evaluations: it ends after PROBATION_QUIET_DAYS without a confirmed incident there, with at least
  one planned opening relocked on time in that run (checked on each daily evaluation).
- Entering needs a FALLING recalculation (new score below the previous one): after a probation exit the score is still low, and a rising
  recalculation must not put the wearer straight back.
- Driven by the `trust_domain.recalculated` event. The handler runs inside the
  outbox consumer's transaction and reads nothing outside its own table; the event
  payload already carries score and confidence. Redelivery is absorbed by the
  consumer dedup (an event is applied at most once).
- Emits `supervision.entered` / `supervision.exited` (ids/domain only).
No model, Discord or command path reaches this module.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction
from infrastructure.outbox import ClaimedDomainEvent, DomainEvent, write_event
from infrastructure.time_format import iso as _iso
from infrastructure.decision_log import decision
from supervision.models import (
    ENTER_BELOW,
    EXIT_AT_OR_ABOVE,
    EXIT_CONSECUTIVE_EVALUATIONS,
    MIN_CONFIDENCE,
    PROBATION_CLEAN_DAYS,
    PROBATION_QUIET_DAYS,
)
from datetime import timedelta
from infrastructure.time_format import parse_iso as _parse_iso

__all__ = ["SupervisionService", "TRIGGER_EVENT_TYPE"]

TRIGGER_EVENT_TYPE = "trust_domain.recalculated"


class SupervisionService:
    def __init__(self, db_path: str | Path, *, core: CoreDatabase | None = None) -> None:
        self.db_path = Path(db_path)
        self._core = core if core is not None else CoreDatabase(self.db_path)

    # Outbox handler ------------------------------------------------------

    def on_trust_recalculated(self, tx: Transaction, event: ClaimedDomainEvent) -> None:
        p = event.payload
        previous = p.get("previous_score")
        self.evaluate_in_transaction(
            tx, domain_id=p["domain_id"], score=float(p["new_score"]), confidence=float(p["new_confidence"]),
            now=event.occurred_at, previous_score=float(previous) if isinstance(previous, (int, float)) else None,
        )

    def on_good_opening(self, tx: Transaction, event: ClaimedDomainEvent) -> None:
        """A planned hygiene opening relocked on time: proof for the quiet-days exit."""
        domain_id = event.payload.get("trust_domain")
        if domain_id:
            tx.execute("UPDATE supervision_states SET probation_proofs = COALESCE(probation_proofs, 0) + 1 WHERE domain_id = ? AND active = 1", (domain_id,))

    def on_incident_confirmation(self, tx: Transaction, event: ClaimedDomainEvent) -> None:
        """A confirmed incident in a supervised domain starts its quiet run over."""
        p = event.payload
        if p.get("new_confirmation") == "confirmed" and p.get("trust_domain"):
            tx.execute("UPDATE supervision_states SET probation_since = ?, probation_proofs = 0 WHERE domain_id = ? AND active = 1",
                       (_iso(event.occurred_at), p["trust_domain"]))

    def _quiet_exits(self, tx: Transaction, *, skip: str | None, now) -> None:
        rows = tx.fetch_all("SELECT * FROM supervision_states WHERE active = 1 AND domain_id != ?", (skip or "",))
        for row in rows:
            since = row["probation_since"] or row["entered_at"]
            if since is None or now - _parse_iso(since) < timedelta(days=PROBATION_QUIET_DAYS) or int(row["probation_proofs"] or 0) < 1:
                continue
            self._upsert(tx, row["domain_id"], active=0, entered_at=row["entered_at"], entry_score=row["entry_score"], streak=0, exited_at=_iso(now), now=now)
            tx.execute("UPDATE supervision_states SET probation_since = NULL, probation_proofs = 0 WHERE domain_id = ?", (row["domain_id"],))
            write_event(tx, DomainEvent(event_type="supervision.exited", source_module="supervision", payload={"domain_id": row["domain_id"]}, occurred_at=now))
            decision("supervision", f"Supervision ended by probation: {PROBATION_QUIET_DAYS} days without an incident, with openings relocked on time.",
                     domain_id=row["domain_id"])

    def on_daily_evaluation(self, tx: Transaction, event: ClaimedDomainEvent) -> None:
        """Probation (owner, 2026-11-13). Uses only the event payload and this module's own table."""
        p = event.payload
        domain_id = p.get("trust_domain")
        self._quiet_exits(tx, skip=domain_id, now=event.occurred_at)
        if not domain_id or p.get("paused"):
            return
        row = tx.fetch_one("SELECT * FROM supervision_states WHERE domain_id = ?", (domain_id,))
        if row is None or not row["active"]:
            return
        days = int(row["probation_days"] or 0) if "probation_days" in row.keys() else 0
        answered, due = int(p.get("check_ins_ok") or 0), int(p.get("check_ins_due") or 0)
        if p.get("clean") and answered >= 1:
            days += 1
        elif p.get("clean") and due == 0:
            pass                                    # nothing was asked that day: neither counts nor breaks the run
        else:
            days = 0
        now = event.occurred_at
        if days >= PROBATION_CLEAN_DAYS:
            self._upsert(tx, domain_id, active=0, entered_at=row["entered_at"], entry_score=row["entry_score"], streak=0, exited_at=_iso(now), now=now)
            tx.execute("UPDATE supervision_states SET probation_days = 0 WHERE domain_id = ?", (domain_id,))
            write_event(tx, DomainEvent(event_type="supervision.exited", source_module="supervision",
                                        payload={"domain_id": domain_id}, occurred_at=now))
            decision("supervision", f"Supervision ended by probation: {days} clean days in a row.", domain_id=domain_id)
            return
        tx.execute("UPDATE supervision_states SET probation_days = ?, last_evaluated_at = ? WHERE domain_id = ?", (days, _iso(now), domain_id))

    def evaluate_in_transaction(self, tx: Transaction, *, domain_id: str, score: float, confidence: float,
                                now: datetime, previous_score: float | None = None) -> str | None:
        """Returns 'entered', 'exited' or None."""
        if not (0.0 <= score <= 1.0) or not (0.0 <= confidence <= 1.0):
            raise ValueError("score and confidence must be within [0, 1]")
        row = tx.fetch_one("SELECT * FROM supervision_states WHERE domain_id = ?", (domain_id,))
        confident = confidence >= MIN_CONFIDENCE
        active = bool(row["active"]) if row is not None else False
        change = None

        if not active:
            falling = previous_score is None or score < previous_score
            if score < ENTER_BELOW and confident and falling:
                self._upsert(tx, domain_id, active=1, entered_at=_iso(now), entry_score=score, streak=0,
                             exited_at=row["exited_at"] if row else None, now=now)
                tx.execute("UPDATE supervision_states SET probation_days = 0, probation_since = ?, probation_proofs = 0 WHERE domain_id = ?", (_iso(now), domain_id))
                change = "entered"
            else:
                self._upsert(tx, domain_id, active=0, entered_at=row["entered_at"] if row else None,
                             entry_score=row["entry_score"] if row else None, streak=0,
                             exited_at=row["exited_at"] if row else None, now=now)
        else:
            streak = (int(row["recovery_streak"]) + 1) if (score >= EXIT_AT_OR_ABOVE and confident) else 0
            if streak >= EXIT_CONSECUTIVE_EVALUATIONS:
                self._upsert(tx, domain_id, active=0, entered_at=row["entered_at"], entry_score=row["entry_score"],
                             streak=0, exited_at=_iso(now), now=now)
                change = "exited"
            else:
                self._upsert(tx, domain_id, active=1, entered_at=row["entered_at"], entry_score=row["entry_score"],
                             streak=streak, exited_at=row["exited_at"], now=now)

        if change is not None:
            write_event(tx, DomainEvent(event_type=f"supervision.{change}", source_module="supervision",
                                        payload={"domain_id": domain_id}, occurred_at=now))
        return change

    # Reads ---------------------------------------------------------------

    def is_active(self, domain_id: str | None = None) -> bool:
        """True if the given domain, or (domain_id=None) any domain, is under supervision."""
        with self._core.transaction() as tx:
            if domain_id is None:
                return tx.fetch_one("SELECT 1 FROM supervision_states WHERE active = 1") is not None
            row = tx.fetch_one("SELECT active FROM supervision_states WHERE domain_id = ?", (domain_id,))
        return bool(row["active"]) if row is not None else False

    # Internals -----------------------------------------------------------

    @staticmethod
    def _upsert(tx: Transaction, domain_id, *, active, entered_at, entry_score, streak, exited_at, now) -> None:
        tx.execute(
            "INSERT INTO supervision_states (domain_id, active, entered_at, entry_score, recovery_streak, exited_at, "
            "last_evaluated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(domain_id) DO UPDATE SET active = excluded.active, entered_at = excluded.entered_at, "
            "entry_score = excluded.entry_score, recovery_streak = excluded.recovery_streak, "
            "exited_at = excluded.exited_at, last_evaluated_at = excluded.last_evaluated_at",
            (domain_id, active, entered_at, entry_score, streak, exited_at, _iso(now)),
        )
