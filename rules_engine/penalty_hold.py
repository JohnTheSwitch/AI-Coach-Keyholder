"""
rules_engine/penalty_hold.py

The Rules Engine's part in a Penalty Window (v0.39 Phase J2): it RECORDS STATE and nothing else. The Penalty Engine alone resolves an Incident
(Warning or Penalty Window); this module never reads an Incident, never evaluates one and has no rule. From the window's own events it keeps:

- the redemption hold (`penalty_holds`): while a window exists, token redemption is blocked for the whole account, whichever lock the
  Incident came from. Earning is not touched (philosophy 3.3). `PenaltyHold.is_active()` is what the Release Engine's gate reads;
- the barrier intents (`penalty_barrier_intents`): what SHOULD happen on Chaster. `chaster_execution/penalty_barrier.py` carries them out.

    penalty_window.started                      -> hold on, intent freeze
    penalty_window.frozen (approved exemption)  -> intent unfreeze: an approved exemption takes precedence and releases the lock (philosophy 3.6)
    penalty_window.resumed (after an exemption) -> intent freeze
    penalty_window.completed                    -> hold off, intent unfreeze (executed only if this window raised the barrier)

A pause for an Emergency Override queues nothing: that path stays entirely outside this mechanism. The handlers run inside consume_event()'s
transaction, read only their own event's payload and this module's tables, and are idempotent on the event id.
"""

from __future__ import annotations

from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction
from infrastructure.outbox import ClaimedDomainEvent
from infrastructure.time_format import iso as _iso

__all__ = ["PenaltyHold", "EXEMPTION_REASONS", "HANDLERS"]

# penalty_engine.models.FreezeReason values of an APPROVED exemption (this package does not import the Penalty Engine).
EXEMPTION_REASONS = frozenset({"temporary_wear_exemption", "partnered_intimacy_authorization"})


def _intent(tx: Transaction, event: ClaimedDomainEvent, action: str, reason: str) -> None:
    stamp = _iso(event.occurred_at)
    tx.execute(
        "INSERT OR IGNORE INTO penalty_barrier_intents (id, penalty_window_id, action, reason, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'pending', ?, ?)", (event.id, event.payload["penalty_window_id"], action, reason, stamp, stamp))


def on_window_started(tx: Transaction, event: ClaimedDomainEvent) -> None:
    tx.execute("INSERT OR IGNORE INTO penalty_holds (penalty_window_id, started_at) VALUES (?, ?)",
               (event.payload["penalty_window_id"], _iso(event.occurred_at)))
    _intent(tx, event, "freeze", "window_started")


def on_window_frozen(tx: Transaction, event: ClaimedDomainEvent) -> None:
    if event.payload.get("reason") in EXEMPTION_REASONS:
        _intent(tx, event, "unfreeze", "exemption_started")


def on_window_resumed(tx: Transaction, event: ClaimedDomainEvent) -> None:
    last = tx.fetch_one("SELECT reason FROM penalty_barrier_intents WHERE penalty_window_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                        (event.payload["penalty_window_id"],))
    if last is not None and last["reason"] == "exemption_started":        # never after an Emergency Override
        _intent(tx, event, "freeze", "exemption_ended")


def on_window_completed(tx: Transaction, event: ClaimedDomainEvent) -> None:
    tx.execute("UPDATE penalty_holds SET released_at = ? WHERE penalty_window_id = ? AND released_at IS NULL",
               (_iso(event.occurred_at), event.payload["penalty_window_id"]))
    _intent(tx, event, "unfreeze", "window_completed")


HANDLERS = {
    "penalty_window.started": on_window_started,
    "penalty_window.frozen": on_window_frozen,
    "penalty_window.resumed": on_window_resumed,
    "penalty_window.completed": on_window_completed,
}


class PenaltyHold:
    def __init__(self, core: CoreDatabase) -> None:
        self._core = core

    def is_active(self) -> bool:
        with self._core.transaction() as tx:
            return tx.fetch_one("SELECT 1 FROM penalty_holds WHERE released_at IS NULL LIMIT 1") is not None
