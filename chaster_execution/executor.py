"""
chaster_execution/executor.py

The ONLY component that can reach a LockActionTransport. It consumes persisted
`lock_decisions` rows produced by the Rules Engine and nothing else: there is no
entry point that accepts free text, a Discord message, a model output or ad-hoc
parameters. Only barrier actions can execute; a time change no longer exists anywhere.

Guarantees (see docs/architecture/chaster_execution_phase3_proposal.md):
- master switch + runtime halt are checked before every run and again before
  every call; a missing/unreadable halt row means halted;
- barrier management (v0.39): freeze/unfreeze are the only actions, and execute ONLY when `allow_barrier_actions` is on AND the
  decision comes from a barrier rule id (BARRIER_RULE_IDS); they draw on their own limit pool
  (rules_engine.limits) and an UNFREEZE additionally needs the barrier to be up and, if a
  `window_check` is supplied, a discretionary window at the moment of the call;
- at-most-once: the 'executing' claim is committed BEFORE the network call and
  is unique per decision (migration 026). An ambiguous outcome (crash between
  the claim and the result, timeout, 5xx) ends as needs_review and is never
  retried automatically;
- a decision older than 15 minutes expires without executing;
- pre-flight on fresh data: scopes, snapshot freshness, locked + keyholder +
  not frozen, and the hard limits re-applied against EXECUTED history only;
- every transition is appended to lock_decision_executions (no token, header or
  request body is ever stored or logged).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from chaster.models import ChasterProviderObservation, ChasterProviderStatus
from chaster_execution.control import ExecutionControl
from chaster_execution.models import (
    BARRIER_ACTIONS,
    BARRIER_RULE_IDS,
    DECISION_MAX_AGE_MINUTES,
    ExecStatus,
    LockActionTransport,
    TransportOutcome,
)
from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.outbox import DomainEvent, write_event
from infrastructure.time_format import iso as _iso
from infrastructure.time_format import parse_iso as _parse_iso
from rules_engine.limits import LOCK_DATA_MAX_AGE, ROLLING_WINDOW, apply_limits
from rules_engine.models import ActionType, RecentAction

logger = logging.getLogger("ai_coach_keyholder.chaster_execution")

__all__ = ["ChasterExecutor", "REQUIRED_SCOPE"]

REQUIRED_SCOPE = "locks"
_COUNTED_STATUSES = (ExecStatus.EXECUTED.value, ExecStatus.EXECUTING.value, ExecStatus.NEEDS_REVIEW.value)

_FAILURE_DETAIL = {
    TransportOutcome.NOT_SENT: "request was not sent",
    TransportOutcome.UNAUTHORIZED: "Chaster rejected the credentials (401)",
    TransportOutcome.FORBIDDEN: "keyholder lacks the time permission (403)",
    TransportOutcome.NOT_ELIGIBLE: "Chaster refused: lock cannot be changed (400)",
    TransportOutcome.NOT_FOUND: "lock not found (404)",
}


class ChasterExecutor:
    def __init__(
        self,
        db_path: str | Path,
        *,
        core: CoreDatabase | None = None,
        enabled: bool,
        control: ExecutionControl,
        transport: LockActionTransport,
        get_latest_observation: Callable[[], ChasterProviderObservation | None],
        refresh_observation: Callable[[datetime], None],
        get_granted_scopes: Callable[[], tuple[str, ...] | None],
        max_per_run: int = 5,
        allow_barrier_actions: bool = False,
        window_check: Callable[[datetime], bool] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self._core = core if core is not None else CoreDatabase(self.db_path)
        self._enabled = enabled
        self._control = control
        self._transport = transport
        self._get_latest = get_latest_observation
        self._refresh = refresh_observation
        self._get_scopes = get_granted_scopes
        self._max_per_run = max_per_run
        self._allow_barrier = allow_barrier_actions
        self._window_check = window_check

    # -------------------------------------------------------------------

    def run_once(self, *, now: datetime) -> list[tuple[str, ExecStatus]]:
        """One sequential pass. Returns (decision_id, resulting status) for every
        decision it advanced. Does nothing when disabled or halted."""
        if not self._enabled or self._control.is_halted():
            return []
        results: list[tuple[str, ExecStatus]] = []
        self._reconcile_orphans(now, results)
        if not self._allow_barrier:              # nothing else can execute
            return results

        cutoff = _iso(now - timedelta(minutes=DECISION_MAX_AGE_MINUTES))
        marks = ",".join("?" for _ in BARRIER_RULE_IDS)
        eligible = f"action_type IN ('freeze', 'unfreeze') AND rule_id IN ({marks})"
        args: list = sorted(BARRIER_RULE_IDS)
        with self._core.transaction() as tx:
            rows = tx.fetch_all(
                f"SELECT * FROM lock_decisions WHERE {eligible} AND "
                "(status = 'pending' OR (status = 'dry_run' AND created_at >= ?)) "
                "ORDER BY created_at, rowid LIMIT ?",
                (*args, cutoff, self._max_per_run),
            )

        for row in rows:
            decision_id = row["id"]
            age = now - _parse_iso(row["created_at"])
            if row["status"] == ExecStatus.DRY_RUN.value:
                if not self._transition(decision_id, ExecStatus.DRY_RUN, ExecStatus.PENDING, now, "queued for execution"):
                    continue
                results.append((decision_id, ExecStatus.PENDING))
            if age > timedelta(minutes=DECISION_MAX_AGE_MINUTES):
                if self._transition(decision_id, ExecStatus.PENDING, ExecStatus.EXPIRED, now, "decision older than 15 minutes"):
                    results.append((decision_id, ExecStatus.EXPIRED))
                continue
            outcome = self._execute(row, now)
            if outcome is None:        # halted mid-run: stop, leave the rest pending
                break
            results.append((decision_id, outcome))
        return results

    # -------------------------------------------------------------------

    def _reconcile_orphans(self, now: datetime, results: list) -> None:
        """Runs are sequential under the single-instance lease, so any decision
        still 'executing' at the start of a run was interrupted. Its outcome is
        unknown: fail closed to needs_review; never retry."""
        with self._core.transaction() as tx:
            orphans = tx.fetch_all("SELECT id FROM lock_decisions WHERE status = 'executing'")
        for r in orphans:
            if self._transition(r["id"], ExecStatus.EXECUTING, ExecStatus.NEEDS_REVIEW, now,
                                "interrupted while executing; outcome unknown"):
                results.append((r["id"], ExecStatus.NEEDS_REVIEW))

    def _execute(self, row, now: datetime) -> ExecStatus | None:
        decision_id = row["id"]
        action = ActionType(row["action_type"])

        if self._control.is_halted():   # also checked before every call, not only at run start
            return None

        refusal, lock_id, end_before = self._preflight(decision_id, action, now, row["rule_id"])
        if refusal is not None:
            self._transition(decision_id, ExecStatus.PENDING, ExecStatus.BLOCKED, now, refusal)
            return ExecStatus.BLOCKED

        if self._control.is_halted():
            return None
        if not self._transition(
            decision_id, ExecStatus.PENDING, ExecStatus.EXECUTING, now, "claimed", end_date_before=end_before,
        ):
            return ExecStatus.PENDING   # lost the claim; someone else owns it

        try:
            result = self._transport.set_frozen(lock_id=lock_id, frozen=(action == ActionType.FREEZE))
        except Exception:
            logger.exception("Transport raised for decision %s; outcome unknown.", decision_id)
            self._transition(decision_id, ExecStatus.EXECUTING, ExecStatus.NEEDS_REVIEW, now, "transport error; outcome unknown")
            return ExecStatus.NEEDS_REVIEW

        http = result.http_status
        if result.outcome == TransportOutcome.OK:
            self._transition(decision_id, ExecStatus.EXECUTING, ExecStatus.EXECUTED, now, "accepted by Chaster", http_status=http)
            return ExecStatus.EXECUTED
        if result.outcome == TransportOutcome.UNKNOWN:
            self._transition(decision_id, ExecStatus.EXECUTING, ExecStatus.NEEDS_REVIEW, now,
                             "ambiguous outcome; inspect the lock manually", http_status=http)
            return ExecStatus.NEEDS_REVIEW
        self._transition(decision_id, ExecStatus.EXECUTING, ExecStatus.FAILED, now,
                         _FAILURE_DETAIL.get(result.outcome, "failed"), http_status=http)
        return ExecStatus.FAILED

    def _preflight(self, decision_id: str, action: ActionType, now: datetime, rule_id: str | None = None):
        def refuse(reason: str):
            return reason, None, None

        if action.value not in BARRIER_ACTIONS or not self._allow_barrier or rule_id not in BARRIER_RULE_IDS:
            return refuse("barrier actions execute only for barrier management")
        scopes = self._get_scopes()
        if not scopes or REQUIRED_SCOPE not in scopes:
            return refuse("keyholder connection lacks the required scope; re-authorisation needed")

        obs = self._get_latest()
        if obs is None or now - obs.fetched_at > LOCK_DATA_MAX_AGE:
            try:
                self._refresh(now)
            except Exception:
                logger.exception("Observation refresh failed before execution.")
            obs = self._get_latest()
        if obs is None:
            return refuse("no lock observation available")
        age = now - obs.fetched_at
        if age.total_seconds() < 0 or age > LOCK_DATA_MAX_AGE:
            return refuse("lock observation is stale")
        if obs.status != ChasterProviderStatus.LOCKED:
            return refuse("lock is not in a locked state")
        if obs.has_keyholder is not True:
            return refuse("lock is not confirmed to be a keyholder lock")
        want_frozen = action == ActionType.UNFREEZE           # an unfreeze needs the barrier to be up
        if obs.is_frozen is not want_frozen:
            return refuse("lock is not in the state this barrier action expects")
        if action == ActionType.UNFREEZE and self._window_check is not None and not self._window_check(now):
            return refuse("barrier release outside a discretionary window")
        if not obs.chaster_lock_id or not obs.chaster_lock_id.strip():
            return refuse("no lock id")

        recent = self._recent_executed(decision_id, now)
        _, why = apply_limits(action, 0, recent, now)
        if why is not None:
            return refuse(f"limits re-check refused: {why}")
        return None, obs.chaster_lock_id, None if obs.end_date is None else _iso(obs.end_date)

    def _recent_executed(self, decision_id: str, now: datetime) -> tuple[RecentAction, ...]:
        marks = ",".join("?" for _ in _COUNTED_STATUSES)
        with self._core.transaction() as tx:
            rows = tx.fetch_all(
                "SELECT d.action_type, d.hours, e.created_at FROM lock_decisions d "
                "JOIN lock_decision_executions e ON e.decision_id = d.id AND e.to_status = 'executing' "
                f"WHERE d.action_type IN ('freeze', 'unfreeze') AND d.status IN ({marks}) AND d.id != ? AND e.created_at >= ?",
                (*_COUNTED_STATUSES, decision_id, _iso(now - ROLLING_WINDOW)),
            )
        return tuple(
            RecentAction(created_at=_parse_iso(r["created_at"]), action_type=ActionType(r["action_type"]), hours=r["hours"])
            for r in rows
        )

    def _transition(
        self, decision_id: str, from_status: ExecStatus, to_status: ExecStatus, now: datetime, detail: str,
        *, http_status: int | None = None, end_date_before: str | None = None,
    ) -> bool:
        """The single writer of lock_decisions.status. Compare-and-set: succeeds
        only if the decision is currently in from_status; appends the audit row
        and emits an outbox event in the same transaction."""
        import uuid

        def write(tx: Transaction, _state: object) -> bool:
            row = tx.fetch_one("SELECT status FROM lock_decisions WHERE id = ?", (decision_id,))
            if row is None or row["status"] != from_status.value:
                return False
            tx.execute(
                "INSERT INTO lock_decision_executions "
                "(id, decision_id, from_status, to_status, created_at, detail, http_status, end_date_before) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), decision_id, from_status.value, to_status.value, _iso(now), detail,
                 http_status, end_date_before),
            )
            tx.execute("UPDATE lock_decisions SET status = ? WHERE id = ?", (to_status.value, decision_id))
            return True

        def events(tx: Transaction, _state: object, changed: bool) -> None:
            if changed:
                write_event(
                    tx,
                    DomainEvent(
                        event_type="lock_decision.status_changed", source_module="chaster_execution",
                        payload={"decision_id": decision_id, "from_status": from_status.value,
                                 "to_status": to_status.value},
                        occurred_at=now,
                    ),
                )

        return apply_transition(self._core, write=write, events=events)
