"""
chaster_execution/extension_guard.py

Keeps the settings the owner decided on true on Chaster, by itself. `chaster/extension_plan.py::REQUIRED_SETTINGS` says what must hold; this reads every locked lock of the
connected account and corrects a drifted setting. Today that is one rule: Chaster's own late-relock penalty on the hygiene opening stays 0, because a relock that is late for a
health reason must never cost time, and because the bot judges overruns itself (`docs/architecture/chaster_capability_map.md` 6).

The same guarantees as every other write here:

- **Runtime halt.** While halted nothing is sent. The keyholder hears once what is waiting, and the correction goes out when the halt clears. The guard never clears a halt and has no
  exception to it: `extension-review --revert` passes the halt only because the owner decided that for a consent revocation.
- **Whole list, one change.** `plan_extension_policy` resends every extension as read and changes only the required field, and it refuses a list it could not rebuild faithfully.
- **Proof by re-reading.** Chaster answers 201 even to an edit it ignores (verified: a partner's config), so only a fresh read counts as done.
- **At most once.** The attempt is audited before the call. An ambiguous result is never retried: a later pass sees the audit row and leaves it for a person. A call that provably
  reached nothing simply tries again.
- It only ever moves a setting to the required value, and only on a lock this account may edit. No time, freeze or unlock path exists here.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from chaster.extension_config import LockConfiguration
from chaster.extension_plan import ReviewPlan, plan_extension_policy
from chaster_execution.models import TransportOutcome
from infrastructure.clock import Clock
from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.time_format import iso as _iso

logger = logging.getLogger("ai_coach_keyholder.chaster_execution.extension_guard")

__all__ = ["ExtensionGuard", "extension_guard_loop", "ACTOR", "DEFAULT_INTERVAL_SECONDS"]

ACTOR = "extension_guard"
DEFAULT_INTERVAL_SECONDS = 900
_RETRYABLE = frozenset({"applied", "halted"})        # anything else on this exact plan waits for a person


def _describe(plan: ReviewPlan) -> str:
    return "; ".join(f"{c.slug} {c.field} {c.before} -> {c.after}" for c in plan.changes)


class ExtensionGuard:
    def __init__(self, db_path: str | Path | None = None, *, core: CoreDatabase | None = None, control, reader, transport, account_user_id: str,
                 list_lock_ids: Callable[[datetime], tuple[list[str], list[str]]], notify: Callable[[str, str, datetime], object] | None = None) -> None:
        """`list_lock_ids(now)` -> (locked lock ids, problems). `notify(key, text, now)` queues a line for the keyholder, once per key."""
        self._core = core if core is not None else CoreDatabase(Path(db_path))
        self._control = control
        self._reader = reader
        self._transport = transport
        self._account = account_user_id
        self._list = list_lock_ids
        self._notify = notify

    def run(self, now: datetime) -> list[str]:
        """One pass. Returns the problems; one lock's problem never stops the next lock."""
        lock_ids, problems = self._list(now)
        problems = list(problems)
        if not lock_ids:
            return problems
        report = self._reader.read(user_id=self._account, lock_ids=lock_ids, now=now)
        problems += report.problems
        for config in report.configurations:
            try:
                problem = self._check(config, now)
            except Exception:
                logger.exception("The extension guard failed on lock %s.", config.lock_id)
                problem = f"{config.lock_id}: failed"
            if problem:
                problems.append(problem)
        return problems

    # ------------------------------------------------------------------ internals

    def _check(self, config: LockConfiguration, now: datetime) -> str | None:
        if config.status != "locked" or not config.can_edit_extensions:
            return None
        plan = plan_extension_policy(config)
        if not plan.has_changes:
            return None
        if plan.refusal is not None:
            self._tell(f"refused:{plan.plan_id}", f"A setting on lock {config.lock_id} needs correcting but I can't rebuild its extension list safely ({plan.refusal}). Please check it: {_describe(plan)}.", now)
            return f"{config.lock_id}: {plan.refusal}"
        previous = self._last_outcome(config.lock_id, plan.plan_id)
        if previous is not None and previous not in _RETRYABLE:
            return f"{config.lock_id}: an earlier correction ended as {previous}; left for a person"
        if self._control.is_halted():
            if previous is None:
                self._audit(config.lock_id, plan, "halted", now)
            self._tell(f"halted:{plan.plan_id}", f"A setting on lock {config.lock_id} has drifted ({_describe(plan)}). The runtime is halted, so I'll correct it when the halt is cleared.", now)
            return None
        audit_id = self._audit(config.lock_id, plan, "attempting", now)          # claimed BEFORE the call
        try:
            sent = self._transport.set_extensions(lock_id=config.lock_id, body=plan.body)
        except Exception:
            logger.exception("The extension guard's write raised; the outcome is ambiguous.")
            self._finish(audit_id, "unknown", None, now)
            return f"{config.lock_id}: unknown"
        if sent.outcome is TransportOutcome.NOT_SENT:
            self._finish(audit_id, "not_sent", sent.http_status, now, drop=True)    # provably reached nothing: the next pass tries again
            return f"{config.lock_id}: not sent"
        if sent.outcome is not TransportOutcome.OK:
            self._finish(audit_id, sent.outcome.value, sent.http_status, now)
            return f"{config.lock_id}: {sent.outcome.value}"
        outcome = self._verify(config.lock_id, now)
        self._finish(audit_id, outcome, sent.http_status, now)
        if outcome != "applied":
            self._tell(f"unverified:{plan.plan_id}", f"I corrected a setting on lock {config.lock_id} but a fresh read does not show it ({_describe(plan)}). Please check it.", now)
            return f"{config.lock_id}: {outcome}"
        logger.info("Extension guard corrected lock %s: %s.", config.lock_id, _describe(plan))
        self._tell(f"applied:{audit_id}", f"I corrected a setting on lock {config.lock_id}: {_describe(plan)}.", now)
        return None

    def _verify(self, lock_id: str, now: datetime) -> str:
        report = self._reader.read(user_id=self._account, lock_ids=[lock_id], now=now)
        if not report.configurations:
            return "unverified"
        return "applied" if not plan_extension_policy(report.configurations[0]).has_changes else "unverified"

    def _last_outcome(self, lock_id: str, plan_id: str) -> str | None:
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT outcome FROM manual_extension_actions WHERE actor = ? AND lock_id = ? AND plan_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                               (ACTOR, lock_id, plan_id))
        return None if row is None else row["outcome"]

    def _audit(self, lock_id: str, plan: ReviewPlan, outcome: str, now: datetime) -> str:
        audit_id = str(uuid.uuid4())

        def write(tx: Transaction, _s: object) -> None:
            tx.execute("INSERT INTO manual_extension_actions (id, created_at, actor, lock_id, plan_id, changes, outcome, http_status, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)",
                       (audit_id, _iso(now), ACTOR, lock_id, plan.plan_id, _describe(plan), outcome, _iso(now)))

        apply_transition(self._core, write=write)
        return audit_id

    def _finish(self, audit_id: str, outcome: str, http_status: int | None, now: datetime, *, drop: bool = False) -> None:
        def write(tx: Transaction, _s: object) -> None:
            if drop:
                tx.execute("DELETE FROM manual_extension_actions WHERE id = ?", (audit_id,))
            else:
                tx.execute("UPDATE manual_extension_actions SET outcome = ?, http_status = ?, updated_at = ? WHERE id = ?", (outcome, http_status, _iso(now), audit_id))

        apply_transition(self._core, write=write)

    def _tell(self, key: str, text: str, now: datetime) -> None:
        if self._notify is None:
            return
        try:
            self._notify(key, text, now)
        except Exception:
            logger.exception("The extension guard could not queue its notice.")


async def extension_guard_loop(guard: ExtensionGuard, clock: Clock, *, interval_seconds: float = DEFAULT_INTERVAL_SECONDS, initial_delay_seconds: float = 45.0) -> None:
    await asyncio.sleep(initial_delay_seconds)
    while True:
        try:
            problems = await asyncio.to_thread(guard.run, clock.now())
            if problems:
                logger.warning("Extension guard: %s", "; ".join(problems)[:300])
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("The extension guard pass failed; will retry next interval.")
        await asyncio.sleep(interval_seconds)
