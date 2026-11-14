"""
recovery_tasks/service.py

Verified recovery tasks (owner, 2026-11-14; brat taming B3; migration 067). During an ACTIVE Penalty Window the bot offers one chore at a time
as a Chaster task: a photo of the result with Chaster's code, checked by the community like a spot check. Each passed task takes
CREDIT_HOURS off the window: it completes a Recovery Plan task (recovery_plan), whose `recovery_plan.task_completed` event the Penalty Engine
turns into a Recovery Credit (philosophy 3.4: capped at half the window, never taken back).

  offer    a card "Earn 2 hours off your Penalty Window: <chore>" with "Take it" / "Not now"; it expires after OFFER_VALID
  take     the Chaster task "Show: <chore>" is assigned (photo + code); TASK_DEADLINE to submit
  poll     verified -> passed (credit); rejected or nothing in time -> failed (no credit, nothing else: a recovery task is voluntary, so a
           failure is never an Incident); a vote that never ends -> cancelled (unknown stays unknown, no credit)
  not now  declined; the next offer comes after OFFER_PAUSE

Never: during a vacation, while the window is frozen, when the task would reach into quiet hours, while the runtime is halted, or while
Chaster's one task slot is taken (a spot check, or the wearer's own task). It changes no time, freezes nothing and unlocks nothing.
"""

from __future__ import annotations

import logging
import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from application.models import ActionSpec, OutgoingMessage
from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.decision_log import decision
from infrastructure.outbox import DomainEvent, write_event
from infrastructure.time_format import iso as _iso
from infrastructure.time_format import parse_iso as _parse_iso
from notifications.queue import enqueue_in_transaction

logger = logging.getLogger("ai_coach_keyholder.recovery_tasks")

__all__ = ["RecoveryTasks", "Ports", "CHORES", "CREDIT_HOURS"]

CREDIT_HOURS = 2.0                      # owner, 2026-11-14
TASK_DEADLINE = timedelta(hours=2)
OFFER_VALID = timedelta(hours=2)
OFFER_PAUSE = timedelta(hours=6)        # after "Not now", an expired offer or a failed task
VOTE_LIMIT = timedelta(hours=12)
CLOCK_SKEW = timedelta(minutes=2)
_OPEN = ("offered", "assigned", "submitted")
# Chores whose RESULT a photo can show (reviewers compare the photo with the text and the code). Plain, like the spot checks' texts.
CHORES = (
    "your bathroom sink, cleaned",
    "your desk, cleared and tidy",
    "your bed, made",
    "your laundry, folded",
    "your dishes, washed and put away",
    "your floor, vacuumed",
    "your kitchen counter, wiped clean",
    "your shoes, lined up",
    "your bin, emptied",
    "one wardrobe shelf, tidied",
)


@dataclass(frozen=True, kw_only=True)
class Ports:
    lock_id: Callable[[datetime], str | None]
    halted: Callable[[], bool]
    paused: Callable[[datetime], bool]
    quiet_between: Callable[[datetime, datetime], bool]
    active_window: Callable[[], str | None]                 # the ACTIVE (not frozen) Penalty Window's id
    plan_for: Callable[[str], str | None]                   # penalty window id -> its Recovery Plan id
    slot_taken: Callable[[], bool]                          # an open spot check holds Chaster's one task slot
    local_clock: Callable[[datetime], str]


class RecoveryTasks:
    def __init__(self, core: CoreDatabase, *, user_id: str, chaster, plans, ports: Ports,
                 rng: Callable[[list], str] = random.SystemRandom().choice) -> None:
        self._core = core
        self.user_id = user_id
        self._chaster = chaster
        self._plans = plans                     # recovery_plan.repository.RecoveryPlanManager
        self._ports = ports
        self._choice = rng

    # ------------------------------------------------------------------ offer

    def offer(self, now: datetime) -> bool:
        """Offers one chore when everything allows it. True when a card was queued."""
        self._expire(now)
        window_id = self._ports.active_window()
        if window_id is None or self._open() is not None or self._paused_after_last(now):
            return False
        if self._ports.paused(now) or self._ports.halted() or self._ports.quiet_between(now, now + OFFER_VALID + TASK_DEADLINE):
            return False
        plan_id = self._ports.plan_for(window_id)
        lock_id = self._ports.lock_id(now)
        if plan_id is None or lock_id is None or self._ports.slot_taken() or self._chaster.current_task(lock_id) is not None:
            return False
        recent = self._recent_chores()
        chore = self._choice([c for c in CHORES if c not in recent] or list(CHORES))
        task = self._plans.propose_task(plan_id, f"Show: {chore}", "A chore shown by photo with Chaster's code, checked by the community.",
                                        CREDIT_HOURS, now=now)
        row_id = str(uuid.uuid4())

        def write(tx: Transaction, _s: object) -> None:
            tx.execute("INSERT INTO recovery_chaster_tasks (id, user_id, penalty_window_id, recovery_task_id, chore, status, created_at, offer_expires_at) "
                       "VALUES (?, ?, ?, ?, ?, 'offered', ?, ?)", (row_id, self.user_id, window_id, task.id, chore, _iso(now), _iso(now + OFFER_VALID)))
            write_event(tx, DomainEvent(event_type="recovery_tasks.offered", source_module="recovery_tasks", occurred_at=now,
                                        payload={"recovery_chaster_task_id": row_id, "recovery_task_id": task.id, "penalty_window_id": window_id}))
            enqueue_in_transaction(tx, user_id=self.user_id, kind="recovery_task", dedupe_key=f"recovery:offer:{row_id}", now=now,
                                   text=(f"Earn {CREDIT_HOURS:g} hours off your Penalty Window. Show me {chore}: a photo with Chaster's code, checked by the "
                                         f"community, within {int(TASK_DEADLINE.total_seconds() // 3600)} hours of taking it. "
                                         f"The offer stands until {self._ports.local_clock(now + OFFER_VALID)}."),
                                   actions=(ActionSpec(action_id=f"recovery:take:{row_id}", label="Take it", style="success"),
                                            ActionSpec(action_id=f"recovery:skip:{row_id}", label="Not now")))

        apply_transition(self._core, write=write)
        decision("recovery_tasks", "A recovery task was offered.", recovery_chaster_task_id=row_id, chore=chore)
        return True

    # ------------------------------------------------------------------ the wearer's buttons

    def press(self, *, user_id: str, row_id: str, action: str, now: datetime) -> str:
        row = self._row(row_id)
        if row is None or row["user_id"] != user_id:
            return "That isn't yours to decide."
        if row["status"] != "offered" or now > _parse_iso(row["offer_expires_at"]):
            return "That offer is no longer open."
        if action == "skip":
            self._close(row, "declined", "declined", now, withdraw=True)
            return "Okay. I'll offer another one later."
        if action != "take":
            return "That isn't one of the options."
        if self._ports.active_window() != row["penalty_window_id"]:
            self._close(row, "cancelled", "window_over", now, withdraw=True)
            return "Your Penalty Window is over, so there's nothing to earn now."
        lock_id = self._ports.lock_id(now)
        if lock_id is None or self._ports.halted() or self._ports.slot_taken() or self._chaster.current_task(lock_id) is not None:
            return "I can't put a task on your lock right now (another task is open, or Chaster isn't reachable). Try again in a bit."
        task_id = self._chaster.assign(lock_id, f"Show: {row['chore']}")
        deadline = now + TASK_DEADLINE
        self._plans.accept_task(row["recovery_task_id"], now=now)

        def write(tx: Transaction, _s: object) -> None:
            tx.execute("UPDATE recovery_chaster_tasks SET status = 'assigned', lock_id = ?, task_id = ?, deadline = ? WHERE id = ? AND status = 'offered'",
                       (lock_id, task_id, _iso(deadline), row_id))
            write_event(tx, DomainEvent(event_type="recovery_tasks.assigned", source_module="recovery_tasks", occurred_at=now,
                                        payload={"recovery_chaster_task_id": row_id}))

        apply_transition(self._core, write=write)
        decision("recovery_tasks", "A recovery task was taken and assigned in Chaster.", recovery_chaster_task_id=row_id, deadline=_iso(deadline))
        return (f"It's in Chaster now: \"Show: {row['chore']}\". Send the photo with Chaster's code there by {self._ports.local_clock(deadline)}. "
                f"When the community verifies it, {CREDIT_HOURS:g} hours come off your Penalty Window.")

    # ------------------------------------------------------------------ poll

    def poll(self, now: datetime) -> int:
        """Reads the open tasks in Chaster. Returns how many were decided."""
        with self._core.transaction() as tx:
            rows = [dict(r) for r in tx.fetch_all("SELECT * FROM recovery_chaster_tasks WHERE user_id = ? AND status IN ('assigned', 'submitted')",
                                                  (self.user_id,))]
        decided = 0
        for row in rows:
            try:
                decided += self._poll_one(row, now)
            except Exception:
                logger.exception("Polling a recovery task failed; it is retried.")
        return decided

    def _poll_one(self, row: dict, now: datetime) -> int:
        deadline = _parse_iso(row["deadline"])
        entry = self._chaster.task_entry(row["lock_id"], row["task_id"])
        if entry is None or entry.get("status") == "removed":
            if now <= deadline + CLOCK_SKEW:
                return 0
            try:
                current = self._chaster.current_task(row["lock_id"])
                if current is not None and current.get("id") == row["task_id"] and not self._ports.halted():
                    self._chaster.unassign(row["lock_id"])
            except Exception:
                logger.exception("A missed recovery task could not be taken back; it is still decided.")
            return self._fail(row, "missed", now, "No photo arrived in time, so nothing was earned this time.")
        submitted = _parse_iso(entry["createdAt"]) if isinstance(entry.get("createdAt"), str) else None
        if submitted is not None and submitted > deadline + CLOCK_SKEW:
            return self._fail(row, "late", now, "The photo came after the deadline, so nothing was earned this time.")
        review = entry.get("peerVerification") if isinstance(entry.get("peerVerification"), dict) else {}
        status = review.get("status")
        if row["status"] == "assigned" or row["review_status"] != status:
            self._update(row["id"], status="submitted", submitted_at=_iso(submitted) if submitted else None, review_status=status)
        if status == "verified":
            return self._pass(row, now)
        if status == "rejected":
            return self._fail(row, "rejected", now, "The community rejected the photo, so nothing was earned this time.")
        if submitted is not None and now > submitted + VOTE_LIMIT:
            self._close(row, "cancelled", "unknown", now, withdraw=True)
            return 1
        return 0

    def _pass(self, row: dict, now: datetime) -> int:
        self._complete(row, now)

        def write(tx: Transaction, _s: object) -> bool:
            done = tx.execute("UPDATE recovery_chaster_tasks SET status = 'passed', outcome = 'verified', decided_at = ? WHERE id = ? AND status IN "
                              "('assigned', 'submitted')", (_iso(now), row["id"])).rowcount
            if done:
                write_event(tx, DomainEvent(event_type="recovery_tasks.passed", source_module="recovery_tasks", occurred_at=now,
                                            payload={"recovery_chaster_task_id": row["id"], "recovery_task_id": row["recovery_task_id"]}))
                enqueue_in_transaction(tx, user_id=self.user_id, kind="recovery_task", dedupe_key=f"recovery:passed:{row['id']}", now=now,
                                       text=f"Verified: {row['chore']}. {CREDIT_HOURS:g} hours come off your Penalty Window.", immediate=True)
            return bool(done)

        if apply_transition(self._core, write=write):
            decision("recovery_tasks", "A recovery task passed; its Recovery Credit was requested.", recovery_chaster_task_id=row["id"])
        return 1

    def _complete(self, row: dict, now: datetime) -> None:
        """Completes the Recovery Plan task (its event becomes the credit). A window extended meanwhile regenerates its plan and expires open
        tasks; the earned credit is then recorded through a fresh task in the current plan, so a verified chore is never lost."""
        try:
            self._plans.complete_task(row["recovery_task_id"], now=now, notes="Verified by the Chaster community.")
            return
        except Exception:
            logger.info("The recovery task's plan task can no longer be completed; using a fresh one in the current plan.", exc_info=True)
        window_id = self._ports.active_window()
        plan_id = self._ports.plan_for(window_id) if window_id == row["penalty_window_id"] else None
        if plan_id is None:
            logger.info("The Penalty Window ended before the recovery task was verified; nothing to credit.")
            return
        task = self._plans.propose_task(plan_id, f"Show: {row['chore']}", "Verified by the Chaster community (re-recorded).", CREDIT_HOURS, now=now)
        self._plans.accept_task(task.id, now=now)
        self._plans.complete_task(task.id, now=now, notes="Verified by the Chaster community.")

    def _fail(self, row: dict, outcome: str, now: datetime, text: str) -> int:
        if self._close(row, "failed", outcome, now, withdraw=True, text=text):
            decision("recovery_tasks", "A recovery task failed; nothing earned, nothing else.", recovery_chaster_task_id=row["id"], outcome=outcome)
        return 1

    # ------------------------------------------------------------------ reads

    def is_recovery_task(self, task_id: str | None) -> bool:
        """For the community review watch: a recovery task's review is decided here, never as a rejected verification photo."""
        if not task_id:
            return False
        with self._core.transaction() as tx:
            return tx.fetch_one("SELECT 1 FROM recovery_chaster_tasks WHERE task_id = ?", (task_id,)) is not None

    def has_open_task(self) -> bool:
        with self._core.transaction() as tx:
            return tx.fetch_one("SELECT 1 FROM recovery_chaster_tasks WHERE user_id = ? AND status IN ('assigned', 'submitted')",
                                (self.user_id,)) is not None

    # ------------------------------------------------------------------ internals

    def _open(self) -> dict | None:
        with self._core.transaction() as tx:
            row = tx.fetch_one(f"SELECT * FROM recovery_chaster_tasks WHERE user_id = ? AND status IN {_OPEN}", (self.user_id,))
        return None if row is None else dict(row)

    def _row(self, row_id: str) -> dict | None:
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT * FROM recovery_chaster_tasks WHERE id = ?", (row_id,))
        return None if row is None else dict(row)

    def _recent_chores(self) -> set[str]:
        with self._core.transaction() as tx:
            return {r["chore"] for r in tx.fetch_all("SELECT chore FROM recovery_chaster_tasks WHERE user_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 3",
                                                     (self.user_id,))}

    def _paused_after_last(self, now: datetime) -> bool:
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT decided_at FROM recovery_chaster_tasks WHERE user_id = ? AND status IN ('declined', 'expired', 'failed', 'cancelled') "
                               "AND decided_at IS NOT NULL ORDER BY decided_at DESC LIMIT 1", (self.user_id,))
        return row is not None and now - _parse_iso(row["decided_at"]) < OFFER_PAUSE

    def _expire(self, now: datetime) -> None:
        with self._core.transaction() as tx:
            rows = [dict(r) for r in tx.fetch_all("SELECT * FROM recovery_chaster_tasks WHERE user_id = ? AND status = 'offered' AND offer_expires_at < ?",
                                                  (self.user_id, _iso(now)))]
        for row in rows:
            self._close(row, "expired", "expired", now, withdraw=True)

    def _close(self, row: dict, status: str, outcome: str, now: datetime, *, withdraw: bool, text: str | None = None) -> bool:
        def write(tx: Transaction, _s: object) -> bool:
            done = tx.execute(f"UPDATE recovery_chaster_tasks SET status = ?, outcome = ?, decided_at = ? WHERE id = ? AND status IN {_OPEN}",
                              (status, outcome, _iso(now), row["id"])).rowcount
            if done:
                write_event(tx, DomainEvent(event_type="recovery_tasks.closed", source_module="recovery_tasks", occurred_at=now,
                                            payload={"recovery_chaster_task_id": row["id"], "status": status, "outcome": outcome}))
                if text:
                    enqueue_in_transaction(tx, user_id=self.user_id, kind="recovery_task", dedupe_key=f"recovery:closed:{row['id']}", now=now, text=text,
                                           immediate=True)
            return bool(done)

        closed = apply_transition(self._core, write=write)
        if closed and withdraw:
            try:
                self._plans.withdraw_task(row["recovery_task_id"], now=now)
            except Exception:
                logger.warning("The Recovery Plan task of a closed recovery task could not be withdrawn.", exc_info=True)
        return closed

    def _update(self, row_id: str, **fields) -> None:
        def write(tx: Transaction, _s: object) -> None:
            sets = ", ".join(f"{k} = ?" for k in fields)
            tx.execute(f"UPDATE recovery_chaster_tasks SET {sets} WHERE id = ?", (*fields.values(), row_id))

        apply_transition(self._core, write=write)
