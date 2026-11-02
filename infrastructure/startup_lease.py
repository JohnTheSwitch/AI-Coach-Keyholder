"""
infrastructure/startup_lease.py

The restart-safe database lease guaranteeing at most one process
instance performs startup reconciliation at a time
(system_state_machine.md Section 7; LEASE-1). An atomic DB write
(`BEGIN IMMEDIATE`, via infrastructure.database.Database.transaction()),
never an in-memory mutex or a PID file — the same reasoning
implementation_conventions.md Section 10 already gives for every other
restart-safe lock in this system.

Independent from, and never a substitute for, the outbox's own claim
mechanism (infrastructure/outbox.py): this lease protects the
reconciliation *steps* from running twice concurrently; the outbox claim
protects the *ongoing publisher* from delivering the same event twice
(system_state_machine.md Section 8).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from infrastructure.database import Database, Transaction

__all__ = ["Lease", "StartupLeaseNotAcquired", "acquire_system_startup_lease", "release_system_startup_lease"]


# _iso/_parse_iso: thin local aliases for the shared implementation
# (infrastructure/time_format.py) -- kept as private names here so
# every existing call site in this module is unchanged; consolidated
# during the final architecture review pass (Phase 2.7) to remove five
# identical copies of this pair across the codebase.
from infrastructure.time_format import iso as _iso
from infrastructure.time_format import parse_iso as _parse_iso


class StartupLeaseNotAcquired(RuntimeError):
    """Raised when another process instance already holds the startup
    lease and it has not yet expired -- this process does not
    participate in startup reconciliation."""


@dataclass(frozen=True, kw_only=True)
class Lease:
    held_by: str
    acquired_at: datetime
    expires_at: datetime


def acquire_system_startup_lease(db: Database, process_id: str, now: datetime, duration: timedelta) -> Lease | None:
    """
    system_state_machine.md Section 7: an atomic UPDATE over the
    single-row system_startup_lease table, succeeding only if no lease
    is held or the held lease has expired. Returns None (not an
    exception) if another process currently holds a live lease --
    callers that need the exception (on_system_startup()) raise it
    themselves; this function's job is only the atomic attempt.

    No domain_event is written here -- lease acquisition is pure
    infrastructure bookkeeping about which process is currently
    performing startup, not a fact any domain module or future consumer
    would ever need to react to.
    """
    with db.transaction() as tx:
        row = tx.fetch_one("SELECT * FROM system_startup_lease WHERE id = 1")
        if row is not None and row["expires_at"] is not None and _parse_iso(row["expires_at"]) > now:
            return None  # another instance holds a live lease

        expires_at = now + duration
        tx.execute(
            """
            INSERT INTO system_startup_lease (id, held_by, acquired_at, expires_at)
            VALUES (1, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET held_by = ?, acquired_at = ?, expires_at = ?
            """,
            (process_id, _iso(now), _iso(expires_at), process_id, _iso(now), _iso(expires_at)),
        )
        return Lease(held_by=process_id, acquired_at=now, expires_at=expires_at)


def release_system_startup_lease(db: Database, lease: Lease) -> None:
    """
    Releases the lease early (normal completion of startup reconciliation
    — no need to wait out the full `duration`). Safe to call even if the
    lease has already expired or been taken over by another instance:
    only clears the row if it still matches `lease.held_by`, so a process
    that held the lease past its own expiry can never accidentally clear
    a DIFFERENT instance's now-current lease.

    `acquired_at` is kept: after a release it means "last acquired", which
    lets an offline tool see that a bot is running (Phase G3,
    behavioral_telemetry.analysis.bot_is_live). Acquisition only ever
    checks `expires_at`.
    """
    with db.transaction() as tx:
        tx.execute(
            "UPDATE system_startup_lease SET held_by = NULL, expires_at = NULL WHERE id = 1 AND held_by = ?",
            (lease.held_by,),
        )
