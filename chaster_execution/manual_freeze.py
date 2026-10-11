"""
chaster_execution/manual_freeze.py

ManualFreezeService (v0.39 dual-role, Phase D): the guarded, OPERATOR-ONLY way to freeze or unfreeze one managed lock by hand, for live
verification (for example the pending test-lock check). It is reached only from the local CLI (`python -m cli freeze-lock`); no model,
conversation, Discord or Chaster path can call it (import-guarded in tests).

Every invocation writes exactly one audit row to `manual_lock_actions` (migration 034): refusals immediately, a real attempt FIRST as
`attempting` (committed before the call, like the executor's claim) and then updated with the outcome, so a crash can never hide a
physical action. An ambiguous result is `unknown` and is never retried here.

Checks, in this order (the first failure refuses and is audited; nothing is sent):
  1. owner        the given identity equals the configured OWNER_USER_ID (constant-time compare);
  2. confirmation the explicit --yes;
  3. lock id      a plain Chaster id;
  4. runtime halt the execution control is not halted (an unreadable control counts as halted);
  5. scope        a live keyholder search shows this lock among the locks the keyholder account manages, locked, with a known frozen state;
  6. state        the lock is not already in the requested state.
This path does not draw on the barrier limit pool and does not create Rules Engine decisions: it is a deliberate manual override with an audit trail.
"""

from __future__ import annotations

import hmac
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from chaster.lock_directory import LockDirectory
from chaster_execution.control import ExecutionControl
from chaster_execution.models import LockActionTransport, TransportOutcome
from infrastructure.clock import Clock
from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.time_format import iso as _iso

logger = logging.getLogger("ai_coach_keyholder.chaster_execution.manual_freeze")

__all__ = ["ManualFreezeService", "ManualFreezeOutcome", "ManualFreezeResult"]

_LOCK_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MAX_ACTOR = 64


class ManualFreezeOutcome(StrEnum):
    APPLIED = "applied"
    ATTEMPTING = "attempting"                  # only ever visible if the process died during the call
    # refusals (nothing was sent)
    OWNER_NOT_CONFIGURED = "owner_not_configured"
    NOT_OWNER = "not_owner"
    NOT_CONFIRMED = "not_confirmed"
    INVALID_LOCK_ID = "invalid_lock_id"
    HALTED = "halted"
    SCOPE_UNVERIFIABLE = "scope_unverifiable"
    NOT_MANAGED_LOCK = "not_managed_lock"
    STATE_UNKNOWN = "state_unknown"
    ALREADY_IN_STATE = "already_in_state"
    # attempts that did not apply
    NOT_SENT = "not_sent"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    NOT_ELIGIBLE = "not_eligible"
    NOT_FOUND = "not_found"
    UNKNOWN = "unknown"                        # ambiguous: the lock may or may not have changed; check on Chaster


_SENT_OUTCOMES = {
    TransportOutcome.OK: ManualFreezeOutcome.APPLIED,
    TransportOutcome.NOT_SENT: ManualFreezeOutcome.NOT_SENT,
    TransportOutcome.UNAUTHORIZED: ManualFreezeOutcome.UNAUTHORIZED,
    TransportOutcome.FORBIDDEN: ManualFreezeOutcome.FORBIDDEN,
    TransportOutcome.NOT_ELIGIBLE: ManualFreezeOutcome.NOT_ELIGIBLE,
    TransportOutcome.NOT_FOUND: ManualFreezeOutcome.NOT_FOUND,
    TransportOutcome.UNKNOWN: ManualFreezeOutcome.UNKNOWN,
}


@dataclass(frozen=True)
class ManualFreezeResult:
    outcome: ManualFreezeOutcome
    audit_id: str
    http_status: int | None = None

    @property
    def applied(self) -> bool:
        return self.outcome is ManualFreezeOutcome.APPLIED

    @property
    def refused(self) -> bool:
        return self.outcome in _REFUSALS


_REFUSALS = {
    ManualFreezeOutcome.OWNER_NOT_CONFIGURED, ManualFreezeOutcome.NOT_OWNER, ManualFreezeOutcome.NOT_CONFIRMED,
    ManualFreezeOutcome.INVALID_LOCK_ID, ManualFreezeOutcome.HALTED, ManualFreezeOutcome.SCOPE_UNVERIFIABLE,
    ManualFreezeOutcome.NOT_MANAGED_LOCK, ManualFreezeOutcome.STATE_UNKNOWN, ManualFreezeOutcome.ALREADY_IN_STATE,
}


class ManualFreezeService:
    def __init__(
        self, db_path: str | Path, *, core: CoreDatabase | None = None, control: ExecutionControl, directory: LockDirectory,
        transport: LockActionTransport, owner_user_id: str | None, keyholder_user_id: str | None, clock: Clock,
    ) -> None:
        self.db_path = Path(db_path)
        self._core = core if core is not None else CoreDatabase(self.db_path)
        self._control = control
        self._directory = directory
        self._transport = transport
        self._owner = owner_user_id.strip() if owner_user_id and owner_user_id.strip() else None
        self._keyholder = keyholder_user_id.strip() if keyholder_user_id and keyholder_user_id.strip() else None
        self._clock = clock

    def run(self, *, actor: str, lock_id: str, frozen: bool, confirmed: bool) -> ManualFreezeResult:
        now = self._clock.now()
        actor_text = (actor or "")[:_MAX_ACTOR]
        lock_text = (lock_id or "")[:_MAX_ACTOR]

        def refuse(outcome: ManualFreezeOutcome) -> ManualFreezeResult:
            return ManualFreezeResult(outcome=outcome, audit_id=self._audit(actor_text, lock_text, frozen, outcome, now))

        if self._owner is None:
            return refuse(ManualFreezeOutcome.OWNER_NOT_CONFIGURED)
        if not isinstance(actor, str) or not hmac.compare_digest(actor.encode("utf-8"), self._owner.encode("utf-8")):
            return refuse(ManualFreezeOutcome.NOT_OWNER)
        if confirmed is not True:
            return refuse(ManualFreezeOutcome.NOT_CONFIRMED)
        if not isinstance(lock_id, str) or not _LOCK_ID.match(lock_id) or not isinstance(frozen, bool):
            return refuse(ManualFreezeOutcome.INVALID_LOCK_ID)
        if self._control.is_halted():
            return refuse(ManualFreezeOutcome.HALTED)

        lock = self._managed_lock(lock_id, now)
        if lock is None:
            return refuse(ManualFreezeOutcome.SCOPE_UNVERIFIABLE if self._scope_unverifiable else ManualFreezeOutcome.NOT_MANAGED_LOCK)
        if lock.is_frozen is None:
            return refuse(ManualFreezeOutcome.STATE_UNKNOWN)
        if lock.is_frozen is frozen:
            return refuse(ManualFreezeOutcome.ALREADY_IN_STATE)

        audit_id = self._audit(actor_text, lock_text, frozen, ManualFreezeOutcome.ATTEMPTING, now)     # claim BEFORE the call
        try:
            result = self._transport.set_frozen(lock_id=lock_id, frozen=frozen)
            outcome = _SENT_OUTCOMES.get(result.outcome, ManualFreezeOutcome.UNKNOWN)
            http_status = result.http_status
        except Exception:
            logger.exception("The manual freeze call raised; the outcome is ambiguous.")
            outcome, http_status = ManualFreezeOutcome.UNKNOWN, None
        self._finish(audit_id, outcome, http_status)
        return ManualFreezeResult(outcome=outcome, audit_id=audit_id, http_status=http_status)

    # ------------------------------------------------------------------ internals

    _scope_unverifiable = False

    def _managed_lock(self, lock_id: str, now: datetime):
        """The lock, only if a live keyholder search lists it as managed and locked. Sets `_scope_unverifiable` when the
        search itself failed (so the refusal says why)."""
        self._scope_unverifiable = False
        try:
            listing = self._directory.list_locks(wearer_user_id=None, keyholder_user_id=self._keyholder, now=now)
        except Exception:
            logger.exception("The keyholder lock search raised.")
            self._scope_unverifiable = True
            return None
        if any(p.startswith("keyholder:") for p in listing.problems):
            self._scope_unverifiable = True
            return None
        for lock in listing.keyholder_locks:
            if lock.lock_id == lock_id and lock.status == "locked":
                return lock
        return None

    def _audit(self, actor: str, lock_id: str, frozen: bool, outcome: ManualFreezeOutcome, now: datetime) -> str:
        audit_id = str(uuid.uuid4())

        def write(tx: Transaction, _s: object) -> None:
            tx.execute(
                "INSERT INTO manual_lock_actions (id, created_at, actor, lock_id, requested_frozen, outcome, http_status, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL, ?)",
                (audit_id, _iso(now), actor, lock_id, int(frozen), outcome.value, _iso(now)),
            )

        apply_transition(self._core, write=write)
        return audit_id

    def _finish(self, audit_id: str, outcome: ManualFreezeOutcome, http_status: int | None) -> None:
        now = self._clock.now()

        def write(tx: Transaction, _s: object) -> None:
            tx.execute("UPDATE manual_lock_actions SET outcome = ?, http_status = ?, updated_at = ? WHERE id = ? AND outcome = 'attempting'",
                       (outcome.value, http_status, _iso(now), audit_id))

        apply_transition(self._core, write=write)
