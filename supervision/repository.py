"""
supervision/repository.py

ActiveSupervisionState (v0.39), derived from SHORT-TERM Trust (owner, 2026-11-13; trust_manager/short_term.py).

- Short-term Trust is the share of good evidence among all evidence of the domain in a rolling window: 7 days for the check-in domain
  (it gets evidence several times a day), 14 days for any other (hygiene gets a few pieces a week). The long-term score no longer starts or
  ends supervision; it still sets privileges and the check-in level.
- Enter: short-term < 0.40 with at least MIN_PIECES_TO_ENTER (5) pieces in the window, so one bad piece of thin evidence never starts it.
- Exit: short-term >= 0.60 (any number of pieces). With nothing bad left in the window and one good piece, that is 1.0: the run of bad
  evidence has aged out and something good came after it. No evidence at all: it stays (nothing shows it is better).
- The band 0.40 - 0.60 neither enters nor exits (hysteresis), so the state does not flip back and forth.
- Evaluated on every `trust_domain.recalculated` (that domain) and on every `daily_evaluation.completed` (every domain with a row here, plus
  the evaluation's own), so evidence ageing out of the window ends supervision even when nothing new happens.

This replaces the earlier rules (long-term score < 0.40 with confidence >= 0.30; exit at >= 0.50 twice, by a probation of 7 clean days, or by
14 quiet days for hygiene). The `probation_*` and `recovery_streak` columns stay (migrations are additive) and are no longer used.

The handler runs inside the outbox consumer's transaction; it writes only its own table and reads Trust evidence only through
`trust_manager.short_term.determine_short_term_trust_in_transaction`. Redelivery is absorbed by the consumer dedup. Emits
`supervision.entered` / `supervision.exited` (domain only, no score). No model, Discord or command path reaches this module.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Mapping

from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction
from infrastructure.decision_log import decision
from infrastructure.outbox import ClaimedDomainEvent, DomainEvent, write_event
from infrastructure.time_format import iso as _iso
from supervision.models import ENTER_BELOW, EXIT_AT_OR_ABOVE, MIN_PIECES_TO_ENTER, OTHER_DOMAIN_WINDOW, decide
from trust_manager.short_term import ShortTermTrust, determine_short_term_trust_in_transaction

__all__ = ["SupervisionService", "TRIGGER_EVENT_TYPE", "DAILY_EVENT_TYPE"]

TRIGGER_EVENT_TYPE = "trust_domain.recalculated"
DAILY_EVENT_TYPE = "daily_evaluation.completed"


class SupervisionService:
    def __init__(self, db_path: str | Path, *, core: CoreDatabase | None = None, windows: Mapping[str, timedelta] | None = None) -> None:
        """`windows`: the short-term window per domain (the composition gives the check-in domain CHECK_IN_DOMAIN_WINDOW); any other domain
        uses OTHER_DOMAIN_WINDOW."""
        self.db_path = Path(db_path)
        self._core = core if core is not None else CoreDatabase(self.db_path)
        self._windows = dict(windows or {})

    def window_for(self, domain_id: str) -> timedelta:
        return self._windows.get(domain_id, OTHER_DOMAIN_WINDOW)

    # Outbox handlers -----------------------------------------------------

    def on_trust_recalculated(self, tx: Transaction, event: ClaimedDomainEvent) -> None:
        self.evaluate_in_transaction(tx, domain_id=event.payload["domain_id"], now=event.occurred_at)

    def on_daily_evaluation(self, tx: Transaction, event: ClaimedDomainEvent) -> None:
        """Evidence ages out of the window without any new event; the daily evaluation is the clock that notices."""
        domains = {r["domain_id"] for r in tx.fetch_all("SELECT domain_id FROM supervision_states")}
        if event.payload.get("trust_domain"):
            domains.add(event.payload["trust_domain"])
        for domain_id in sorted(domains):
            if tx.fetch_one("SELECT 1 FROM trust_domains WHERE domain_id = ? AND is_active = 1", (domain_id,)):
                self.evaluate_in_transaction(tx, domain_id=domain_id, now=event.occurred_at)
            else:
                self._end_untracked(tx, domain_id, event.occurred_at)

    def _end_untracked(self, tx: Transaction, domain_id: str, now: datetime) -> None:
        """A domain that is no longer tracked (deactivated by consent) gets no new evidence: its supervision would otherwise never end and would hold
        every release. It ends."""
        row = tx.fetch_one("SELECT * FROM supervision_states WHERE domain_id = ? AND active = 1", (domain_id,))
        if row is None:
            return
        self._upsert(tx, domain_id, active=0, entered_at=row["entered_at"], entry_score=row["entry_score"], streak=0, exited_at=_iso(now), now=now)
        write_event(tx, DomainEvent(event_type="supervision.exited", source_module="supervision", payload={"domain_id": domain_id}, occurred_at=now))
        decision("supervision", "Supervision ended: the domain is no longer tracked.", domain_id=domain_id)

    def evaluate_in_transaction(self, tx: Transaction, *, domain_id: str, now: datetime) -> str | None:
        """Returns 'entered', 'exited' or None."""
        short = determine_short_term_trust_in_transaction(tx, domain_id, now=now, window=self.window_for(domain_id))
        row = tx.fetch_one("SELECT * FROM supervision_states WHERE domain_id = ?", (domain_id,))
        active = bool(row["active"]) if row is not None else False
        change = decide(active, short.score, short.pieces)
        entered_at = row["entered_at"] if row else None
        entry_score = row["entry_score"] if row else None
        exited_at = row["exited_at"] if row else None
        if change == "entered":
            active, entered_at, entry_score = True, _iso(now), short.score
        elif change == "exited":
            active, exited_at = False, _iso(now)
        self._upsert(tx, domain_id, active=int(active), entered_at=entered_at, entry_score=entry_score, streak=0, exited_at=exited_at, now=now)
        if change is not None:
            write_event(tx, DomainEvent(event_type=f"supervision.{change}", source_module="supervision",
                                        payload={"domain_id": domain_id}, occurred_at=now))
            decision("supervision", _explain(change, short), domain_id=domain_id)
        return change

    # Reads ---------------------------------------------------------------

    def is_active(self, domain_id: str | None = None) -> bool:
        """True if the given domain, or (domain_id=None) any domain, is under supervision."""
        with self._core.transaction() as tx:
            if domain_id is None:
                return tx.fetch_one("SELECT 1 FROM supervision_states WHERE active = 1") is not None
            row = tx.fetch_one("SELECT active FROM supervision_states WHERE domain_id = ?", (domain_id,))
        return bool(row["active"]) if row is not None else False

    def get_short_term(self, domain_id: str, now: datetime) -> ShortTermTrust:
        """Owner and CLI read (never shown to the wearer)."""
        with self._core.transaction() as tx:
            return determine_short_term_trust_in_transaction(tx, domain_id, now=now, window=self.window_for(domain_id))

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


def _explain(change: str, short: ShortTermTrust) -> str:
    detail = f"short-term {short.score:.2f} from {short.pieces} pieces over {short.window_days:g} days"
    if change == "entered":
        return f"Supervision started: the recent record is weak ({detail}; below {ENTER_BELOW:.2f}, at least {MIN_PIECES_TO_ENTER} pieces)."
    return f"Supervision ended: the recent record recovered ({detail}; at least {EXIT_AT_OR_ABOVE:.2f})."
