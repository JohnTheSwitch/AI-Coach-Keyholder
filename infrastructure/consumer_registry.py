"""
infrastructure/consumer_registry.py

The shared, domain-agnostic consumer dispatch layer
(implementation_conventions.md Section 5's "consumer framework",
Phase 1.4's deferred item). Built directly on
infrastructure.outbox's claim/consume/publish primitives -- this module
adds only the missing piece: mapping an event_type string to the
handler(s) that should run, so a caller does not have to know, at every
call site, which consumer(s) care about which event.

Contains no knowledge of any specific event type or module (Trust
Manager, Penalty Engine, ...) -- the actual registrations (which
consumer subscribes to which event_type with which handler) are wired
by the composition layer (system/startup.py), not by this module.

Per-registration exception boundary (added while implementing
plugin_architecture_proposal.md's PluginRegistry, Step 2): a real gap
identified in that document's own Section 1 survey -- an unhandled
exception from one registration's handler used to propagate straight
through this loop, capable of aborting every other registration for
the same event, and everything after it in the same
`process_pending_events()` call. Fixed here for every registration,
first-party and plugin alike, not only plugins -- there was never a
reason for one consumer's bug to take down unrelated ones, and the
fix is a small, additive log-and-continue, not a behavior change any
existing consumer depends on. The exception is still allowed to
propagate out of `consume_event()`'s own transaction context manager
first (so a partial write still rolls back and `mark_processed()`
never runs for a failed handler) -- only caught here, one level up,
after that transaction has already safely closed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from infrastructure.database import Database, Transaction
from infrastructure.decision_log import log_event
from infrastructure.outbox import ClaimedDomainEvent, claim_pending_events, consume_event, mark_published

logger = logging.getLogger("ai_coach_keyholder.consumer_registry")

__all__ = ["ConsumerRegistry", "process_pending_events"]

# The handler receives the already-open Transaction (from consume_event's
# apply_transition-based dedup wrapper) and the claimed event -- it must
# never open its own transaction or call another module's public API
# method (which would open one), exactly as
# penalty_engine/repository.py's `_consume_confirmed_incident_in_transaction`
# demonstrates. This constraint is the whole reason this signature takes
# a Transaction, not a Database.
EventHandler = Callable[[Transaction, ClaimedDomainEvent], None]


@dataclass(frozen=True)
class _Registration:
    consumer_name: str
    handler: EventHandler


class ConsumerRegistry:
    """
    Maps an event_type string to the consumer(s) that react to it.
    Deliberately a plain in-memory mapping built fresh at process
    startup (system/startup.py) -- not itself persisted, since the
    registrations are a property of the running code, not of the data;
    only *whether a given (event_id, consumer_name) pair has been
    processed* (domain_event_consumers) is persisted, which is what
    `infrastructure.outbox` already owns.
    """

    def __init__(self) -> None:
        self._registrations: dict[str, list[_Registration]] = {}

    def register(self, event_type: str, consumer_name: str, handler: EventHandler) -> None:
        self._registrations.setdefault(event_type, []).append(_Registration(consumer_name, handler))

    def dispatch(self, db: Database, event: ClaimedDomainEvent, *, now: datetime) -> int:
        """
        Runs every registered handler for `event.event_type`, each
        through `consume_event()` (dedup + atomicity, one transaction
        per consumer per event -- never one shared transaction across
        multiple consumers, so one consumer's failure never blocks
        another's delivery of the same event). Returns the number of
        handlers that actually ran their effect (excludes redeliveries
        silently absorbed by dedup).
        """
        registrations = self._registrations.get(event.event_type, [])
        ran = 0
        for reg in registrations:
            try:
                did_run = consume_event(
                    db, event, consumer_name=reg.consumer_name,
                    handler=lambda tx, _reg=reg: _reg.handler(tx, event),
                    now=now,
                )
            except Exception:
                # The transaction consume_event() opened has already
                # rolled back by this point (Python's `with` runs
                # __exit__, and therefore the rollback, during unwind,
                # before this except clause ever runs) -- mark_processed()
                # never ran for this registration, so a legitimate retry
                # remains possible later. Caught here only to keep this
                # one registration's failure from aborting every other
                # registration for this event (see the module docstring).
                logger.exception(
                    "Consumer %r failed handling event_type=%r (event_id=%r) -- continuing with remaining registrations.",
                    reg.consumer_name, event.event_type, event.id,
                )
                continue
            if did_run:
                ran += 1
        return ran


def process_pending_events(
    db: Database,
    registry: ConsumerRegistry,
    *,
    claimant: str,
    now: datetime,
    batch_size: int = 50,
    lease_duration: timedelta = timedelta(minutes=5),
    max_cascade_rounds: int = 10,
) -> int:
    """
    The outbox publisher, tying together claim -> dispatch -> mark
    published (implementation_conventions.md Section 5).

    Loops, claiming and dispatching a fresh batch each round, until a
    round claims nothing new -- draining any CASCADE of events within
    this one call, not only the batch that existed at the moment this
    function was first invoked. This matters concretely: a handler
    commonly publishes a new event as a side effect of processing one
    (e.g. Penalty Engine's `_consume_confirmed_incident_in_transaction()`
    emitting `penalty_window.started` while reacting to
    `incident.confirmation_changed`) -- that new event is a fresh row,
    not part of the batch `claim_pending_events()` already fetched, so a
    single claim-dispatch-publish pass would leave it for a *later*
    call to pick up. Discovered while wiring Recovery Plan as a second,
    downstream consumer of Penalty Engine's own events
    (`system/README.md`): with no continuously-running publisher loop
    yet (still deferred, see `system/README.md`), a cascaded event would
    otherwise sit unprocessed until the next full `on_system_startup()`
    call -- an unacceptable delay for something `on_system_startup()` is
    specifically meant to fully settle.

    `max_cascade_rounds` is a safety bound against a hypothetical
    infinite cascade (e.g. a wiring bug where two handlers keep
    re-triggering each other) -- not expected to ever be reached in
    normal operation, where a cascade has a small, fixed depth (Trust
    Manager -> Penalty Engine -> Recovery Plan, three levels today).

    `published_at` means only "handed off to dispatch," per
    infrastructure.outbox's own contract; a handler with no registered
    consumer for its event_type is still marked published (there being
    no consumer for an event today is not a delivery failure -- see
    domain_events_catalog.md Finding 6's same reasoning).

    Returns the total number of individual handler runs across every
    round (for logging/diagnostics -- not itself load-bearing).
    """
    total_ran = 0
    for _round in range(max_cascade_rounds):
        claimed = claim_pending_events(db, claimant=claimant, batch_size=batch_size, now=now, lease_duration=lease_duration)
        if not claimed:
            break
        for event in claimed:
            total_ran += registry.dispatch(db, event, now=now)
        mark_published(db, [e.id for e in claimed], now)
        for e in claimed:                       # the decision log (owner, 2026-11-10): committed events only
            log_event(e.event_type, e.source_module, e.payload, e.occurred_at)
    return total_ran
