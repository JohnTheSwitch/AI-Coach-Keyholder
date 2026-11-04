"""
chaster_execution/penalty_barrier.py

PenaltyBarrierExecutor (v0.39 Phase J2): carries out the barrier intents a Penalty Window leaves in `penalty_barrier_intents`
(rules_engine/penalty_hold.py). It is driven only by the periodic cycle and accepts no free text, Discord message, model output or ad-hoc
parameter: the one input is that queue, and the one target is the configured lock (PENALTY_FREEZE_LOCK_ID). Only freeze / unfreeze exist here;
it can never add or remove time.

Why a queue instead of `lock_decisions`: a decision expires after 15 minutes, but a penalty must survive a halt of any length, and the
ChasterExecutor reads one wearer observation, which cannot point at a lock the account holds as keyholder.

Guarantees:
- HALT: while the runtime is halted (an unreadable control counts as halted) or the feature is off, nothing is read from or sent to Chaster.
  Every intent stays `pending`, is logged once as the intended action and NEVER expires; it is carried out when execution resumes. The halt
  is checked before every run and again immediately before every call;
- TARGETING: the lock must report role `keyholder` in a fresh read. A lock the account only wears (or a misconfigured id) is never called:
  the intent is `skipped` and the penalty is enforced by the internal redemption hold alone;
- AT MOST ONCE: the `executing` claim is committed before the call. An ambiguous outcome (timeout, 5xx, a crash after the claim) ends as
  `needs_review` and is never retried; only a call that provably reached nothing (`not_sent`, an unauthorized token) stays `pending`;
- ONLY WHAT IT RAISED: `window_completed` unfreezes only if this window's own start froze the lock, and a freeze is skipped once its window
  has ended. Nothing here re-freezes a lock the 72-hour backstop released: it acts on window events only, never on an observed state.

No token, header, request body or provider text is stored or logged.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Callable

from chaster_execution.models import LockActionTransport, TransportOutcome
from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.outbox import DomainEvent, write_event
from infrastructure.time_format import iso as _iso

logger = logging.getLogger("ai_coach_keyholder.chaster_execution.penalty_barrier")

__all__ = ["PenaltyBarrierExecutor", "PENDING", "EXECUTING", "EXECUTED", "SKIPPED", "FAILED", "NEEDS_REVIEW"]

PENDING, EXECUTING, EXECUTED, SKIPPED, FAILED, NEEDS_REVIEW = "pending", "executing", "executed", "skipped", "failed", "needs_review"
_LOCK_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_RETRY = (TransportOutcome.NOT_SENT, TransportOutcome.UNAUTHORIZED)              # provably nothing happened on Chaster
_REFUSED = (TransportOutcome.FORBIDDEN, TransportOutcome.NOT_ELIGIBLE, TransportOutcome.NOT_FOUND)


class PenaltyBarrierExecutor:
    def __init__(self, db_path: str | Path, *, core: CoreDatabase | None = None, enabled: bool, control, transport: LockActionTransport | None,
                 read_lock: Callable[[str], dict] | None, lock_id: str | None) -> None:
        """`read_lock(lock_id)` returns the raw lock as the connected account sees it (GET /locks/{id}). `enabled` False is the dry run: the
        queue only logs what would happen."""
        self._core = core if core is not None else CoreDatabase(Path(db_path))
        self._lock_id = lock_id.strip() if lock_id and lock_id.strip() else None
        self._enabled = bool(enabled and self._lock_id and _LOCK_ID.match(self._lock_id) and transport is not None and read_lock is not None)
        self._control = control
        self._transport = transport
        self._read_lock = read_lock

    def _halted(self) -> bool:
        try:
            return bool(self._control.is_halted())
        except Exception:
            logger.exception("The execution control could not be read; treating it as halted.")
            return True

    def run(self, now: datetime) -> list[tuple[str, str]]:
        self._reconcile_orphans(now)
        with self._core.transaction() as tx:
            rows = [dict(r) for r in tx.fetch_all("SELECT * FROM penalty_barrier_intents WHERE status = 'pending' ORDER BY created_at, rowid")]
        if not rows:
            return []
        halted = self._halted()
        if halted or not self._enabled:
            self._log_held(rows, now, "the runtime is halted" if halted else "the penalty barrier is switched off")
            return []
        results = []
        for row in rows:
            status = self._process(row, now)
            results.append((row["id"], status))
            if status == PENDING:
                break                                  # keep the order: a later intent must not overtake an unfinished one
        return results

    # ------------------------------------------------------------------ internals

    def _log_held(self, rows: list[dict], now: datetime, why: str) -> None:
        for row in rows:
            if row["logged_at"] is not None:
                continue
            logger.warning("Penalty barrier: intended %s (%s) of window %s is QUEUED because %s; nothing was sent to Chaster.",
                           row["action"], row["reason"], row["penalty_window_id"], why)

            def write(tx: Transaction, _s: object, intent_id=row["id"]) -> None:
                tx.execute("UPDATE penalty_barrier_intents SET logged_at = ? WHERE id = ? AND logged_at IS NULL", (_iso(now), intent_id))

            apply_transition(self._core, write=write)

    def _reconcile_orphans(self, now: datetime) -> None:
        with self._core.transaction() as tx:
            orphans = [r["id"] for r in tx.fetch_all("SELECT id FROM penalty_barrier_intents WHERE status = 'executing'")]
        for intent_id in orphans:
            self._move(intent_id, EXECUTING, NEEDS_REVIEW, now, "interrupted during the call: check the lock on Chaster")

    def _process(self, row: dict, now: datetime) -> str:
        intent_id, action, reason = row["id"], row["action"], row["reason"]
        with self._core.transaction() as tx:
            hold = tx.fetch_one("SELECT released_at, raised_freeze FROM penalty_holds WHERE penalty_window_id = ?", (row["penalty_window_id"],))
        if action == "freeze" and (hold is None or hold["released_at"] is not None):
            return self._skip(intent_id, now, "the Penalty Window has ended")
        if reason == "window_completed" and (hold is None or not hold["raised_freeze"]):
            return self._skip(intent_id, now, "this window did not raise the barrier")
        try:
            raw = self._read_lock(self._lock_id)
        except Exception as exc:
            logger.warning("Penalty barrier: reading the lock failed (%s); intent stays queued.", type(exc).__name__)
            return PENDING
        role, status, frozen = raw.get("role"), raw.get("status"), raw.get("isFrozen")
        if role != "keyholder":
            return self._skip(intent_id, now, "the account is not this lock's keyholder: enforced by the redemption hold only")
        if status != "locked":
            return self._skip(intent_id, now, "the lock is not locked")
        if not isinstance(frozen, bool):
            logger.warning("Penalty barrier: the lock's frozen state is unknown; intent stays queued.")
            return PENDING
        want_frozen = action == "freeze"
        if frozen is want_frozen:
            return self._skip(intent_id, now, f"the lock is already {'frozen' if frozen else 'not frozen'}")
        if not self._move(intent_id, PENDING, EXECUTING, now, "claimed", lock_id=self._lock_id):   # committed BEFORE the call
            return EXECUTING
        if self._halted():
            self._move(intent_id, EXECUTING, PENDING, now, "halted before the call")
            return PENDING
        try:
            sent = self._transport.set_frozen(lock_id=self._lock_id, frozen=want_frozen)
            outcome, http_status = sent.outcome, sent.http_status
        except Exception:
            logger.exception("The penalty barrier call raised; the outcome is ambiguous.")
            outcome, http_status = TransportOutcome.UNKNOWN, None
        if outcome is TransportOutcome.OK:
            self._move(intent_id, EXECUTING, EXECUTED, now, None, http_status=http_status, raised=reason == "window_started",
                       window_id=row["penalty_window_id"])
            logger.info("Penalty barrier: %s of lock %s executed (%s).", action, self._lock_id, reason)
            return EXECUTED
        if outcome in _RETRY:
            self._move(intent_id, EXECUTING, PENDING, now, outcome.value, http_status=http_status)
            logger.warning("Penalty barrier: %s was not applied (%s); intent stays queued.", action, outcome.value)
            return PENDING
        final = FAILED if outcome in _REFUSED else NEEDS_REVIEW
        self._move(intent_id, EXECUTING, final, now, outcome.value, http_status=http_status)
        logger.error("Penalty barrier: %s of lock %s ended as %s (%s).", action, self._lock_id, final, outcome.value)
        return final

    def _skip(self, intent_id: str, now: datetime, why: str) -> str:
        self._move(intent_id, PENDING, SKIPPED, now, why)
        logger.info("Penalty barrier: intent skipped: %s.", why)
        return SKIPPED

    def _move(self, intent_id: str, old: str, new: str, now: datetime, detail: str | None, *, http_status: int | None = None,
              lock_id: str | None = None, raised: bool = False, window_id: str | None = None) -> bool:
        def write(tx: Transaction, _s: object) -> bool:
            done = tx.execute(
                "UPDATE penalty_barrier_intents SET status = ?, detail = ?, http_status = COALESCE(?, http_status), "
                "lock_id = COALESCE(?, lock_id), updated_at = ? WHERE id = ? AND status = ?",
                (new, detail, http_status, lock_id, _iso(now), intent_id, old))
            changed = done.rowcount == 1
            if changed and raised:
                tx.execute("UPDATE penalty_holds SET raised_freeze = 1 WHERE penalty_window_id = ?", (window_id,))
            if changed and new in (EXECUTED, SKIPPED, FAILED, NEEDS_REVIEW):
                write_event(tx, DomainEvent(event_type="penalty_barrier.status_changed", source_module="chaster_execution",
                                            payload={"intent_id": intent_id, "status": new}, occurred_at=now))
            return changed

        return bool(apply_transition(self._core, write=write))
