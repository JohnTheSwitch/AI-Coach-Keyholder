"""
supervision/repository.py

ActiveSupervisionState (v0.39), derived from Trust recalculations.

- Enter: score < 0.40 with confidence >= 0.30.
- Exit: score >= 0.50 (confidence >= 0.30) for two CONSECUTIVE evaluations.
  A weak-confidence or sub-threshold evaluation resets the streak.
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
from supervision.models import (
    ENTER_BELOW,
    EXIT_AT_OR_ABOVE,
    EXIT_CONSECUTIVE_EVALUATIONS,
    MIN_CONFIDENCE,
)

__all__ = ["SupervisionService", "TRIGGER_EVENT_TYPE"]

TRIGGER_EVENT_TYPE = "trust_domain.recalculated"


class SupervisionService:
    def __init__(self, db_path: str | Path, *, core: CoreDatabase | None = None) -> None:
        self.db_path = Path(db_path)
        self._core = core if core is not None else CoreDatabase(self.db_path)

    # Outbox handler ------------------------------------------------------

    def on_trust_recalculated(self, tx: Transaction, event: ClaimedDomainEvent) -> None:
        p = event.payload
        self.evaluate_in_transaction(
            tx, domain_id=p["domain_id"], score=float(p["new_score"]), confidence=float(p["new_confidence"]),
            now=event.occurred_at,
        )

    def evaluate_in_transaction(self, tx: Transaction, *, domain_id: str, score: float, confidence: float,
                                now: datetime) -> str | None:
        """Returns 'entered', 'exited' or None."""
        if not (0.0 <= score <= 1.0) or not (0.0 <= confidence <= 1.0):
            raise ValueError("score and confidence must be within [0, 1]")
        row = tx.fetch_one("SELECT * FROM supervision_states WHERE domain_id = ?", (domain_id,))
        confident = confidence >= MIN_CONFIDENCE
        active = bool(row["active"]) if row is not None else False
        change = None

        if not active:
            if score < ENTER_BELOW and confident:
                self._upsert(tx, domain_id, active=1, entered_at=_iso(now), entry_score=score, streak=0,
                             exited_at=row["exited_at"] if row else None, now=now)
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
