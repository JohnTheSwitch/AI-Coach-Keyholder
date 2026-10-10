"""
rules_engine/repository.py

Persistence and orchestration for the Rules Engine (Phase 2, STRICT
DRY-RUN: nothing here ever calls Chaster or any write API of another module).

Flow:
1. The outbox handler `record_trigger_in_transaction()` runs inside
   consume_event()'s transaction. It only records a trigger row from the
   event payload (a handler may not call other modules' public APIs).
2. `evaluate_pending()` runs afterwards, outside that transaction. It reads
   inputs through narrow read callables, runs the pure `evaluate()`, and
   persists ONE append-only Decision per trigger together with an outbox
   event, and marks the trigger processed.

Incident-driven time is deliberately NOT a trigger: it derives from
ExtensionDecision in the Penalty Engine, so a single incident can never be
punished twice. Only goal accountability assessments trigger evaluations.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Callable

from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.outbox import ClaimedDomainEvent, DomainEvent, write_event
from infrastructure.time_format import iso as _iso
from infrastructure.time_format import parse_iso as _parse_iso
from rules_engine.engine import evaluate
from rules_engine.limits import ROLLING_WINDOW
from rules_engine.models import (
    ActionType,
    Decision,
    DecisionStatus,
    EntitlementClass,
    GoalDirection,
    LockSnapshot,
    RecentAction,
    RuleInputs,
    Ruleset,
    new_id,
)
from rules_engine.ruleset import DEFAULT_RULESET, validate_ruleset

logger = logging.getLogger("ai_coach_keyholder.rules_engine")

__all__ = ["RulesEngineService", "TRIGGER_EVENT_TYPE"]

TRIGGER_EVENT_TYPE = "goal_accountability_assessment.recorded"


class RulesEngineService:
    def __init__(
        self,
        db_path: str | Path,
        *,
        core: CoreDatabase | None = None,
        get_trust_state: Callable[[str], object | None],
        get_lock_snapshot: Callable[[], LockSnapshot | None] | None = None,
        ruleset: Ruleset = DEFAULT_RULESET,
    ) -> None:
        """
        `get_trust_state(domain_id)` returns an object with `.score` and
        `.confidence` (TrustManager.get_domain_state), or None.
        `get_lock_snapshot()` returns the freshest provider-reported
        snapshot, or None when there is none (the engine then decides
        NO_ACTION). Both are read-only.
        """
        self.db_path = Path(db_path)
        self._core = core if core is not None else CoreDatabase(self.db_path)
        self._get_trust_state = get_trust_state
        self._get_lock_snapshot = get_lock_snapshot
        self._ruleset = validate_ruleset(ruleset)

    # -------------------------------------------------------------------
    # Outbox handler (inside consume_event()'s transaction)
    # -------------------------------------------------------------------

    @staticmethod
    def record_trigger_in_transaction(tx: Transaction, event: ClaimedDomainEvent) -> None:
        payload = event.payload
        relevant = bool(payload["relevant_to_trust"])
        direction = payload["direction"] if relevant else GoalDirection.NEUTRAL.value
        GoalDirection(direction)  # fail fast on an unknown value
        tx.execute(
            """
            INSERT OR IGNORE INTO lock_decision_triggers (id, trust_domain, assessment_id, direction, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (event.id, payload["trust_domain"], payload["assessment_id"], direction, _iso(event.occurred_at)),
        )

    # -------------------------------------------------------------------
    # Evaluation (outside any consumer transaction)
    # -------------------------------------------------------------------

    def evaluate_pending(self, *, now: datetime) -> list[Decision]:
        with self._core.transaction() as tx:
            pending = tx.fetch_all(
                "SELECT * FROM lock_decision_triggers WHERE processed_at IS NULL ORDER BY created_at, rowid"
            )
        decisions = []
        for row in pending:
            decisions.append(self._evaluate_trigger(row, now))
        return decisions

    def _evaluate_trigger(self, trigger, now: datetime) -> Decision:
        domain = trigger["trust_domain"]
        state = self._get_trust_state(domain)
        lock = self._get_lock_snapshot() if self._get_lock_snapshot is not None else None

        with self._core.transaction() as tx:
            history = tx.fetch_all(
                "SELECT direction FROM lock_decision_triggers WHERE trust_domain = ? AND direction != 'neutral' "
                "AND (created_at < ? OR (created_at = ? AND rowid <= (SELECT rowid FROM lock_decision_triggers WHERE id = ?))) "
                "ORDER BY created_at DESC, rowid DESC",
                (domain, trigger["created_at"], trigger["created_at"], trigger["id"]),
            )
            prev = tx.fetch_one(
                "SELECT inputs_json FROM lock_decisions WHERE trust_domain = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (domain,),
            )
            recent_rows = tx.fetch_all(
                "SELECT created_at, action_type, hours FROM lock_decisions "
                "WHERE action_type != ? AND created_at >= ? ORDER BY created_at",
                (ActionType.NO_ACTION.value, _iso(now - ROLLING_WINDOW)),
            )

        direction = GoalDirection(trigger["direction"])
        setback_streak = progress_streak = 0
        if direction != GoalDirection.NEUTRAL:
            for h in history:
                if h["direction"] != direction.value:
                    break
                if direction == GoalDirection.SETBACK:
                    setback_streak += 1
                else:
                    progress_streak += 1
        previous_score = json.loads(prev["inputs_json"]).get("trust_score") if prev is not None else None
        recent = tuple(
            RecentAction(created_at=_parse_iso(r["created_at"]), action_type=ActionType(r["action_type"]), hours=r["hours"])
            for r in recent_rows
        )
        inputs = RuleInputs(
            now=now, trust_domain=domain,
            trust_score=getattr(state, "score", None), trust_confidence=getattr(state, "confidence", None),
            previous_trust_score=previous_score, direction=direction,
            setback_streak=setback_streak, progress_streak=progress_streak,
            lock=lock, recent_actions=recent,
        )
        outcome = evaluate(inputs, self._ruleset)
        decision = Decision(
            trigger_id=trigger["id"], trust_domain=domain, created_at=now,
            action_type=outcome.action_type, hours=outcome.hours, entitlement_class=outcome.entitlement_class,
            rule_id=outcome.rule_id, ruleset_version=outcome.ruleset_version, reason=outcome.reason,
            inputs=_inputs_to_dict(inputs), status=DecisionStatus.DRY_RUN,
        )

        def write(tx: Transaction, _state: object) -> Decision | None:
            if tx.fetch_one("SELECT 1 FROM lock_decisions WHERE trigger_id = ?", (decision.trigger_id,)):
                return None  # already decided (a concurrent or repeated run)
            tx.execute(
                """
                INSERT INTO lock_decisions
                    (id, trigger_id, trust_domain, created_at, action_type, hours, entitlement_class,
                     rule_id, ruleset_version, reason, inputs_json, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (decision.id, decision.trigger_id, decision.trust_domain, _iso(now), decision.action_type.value,
                 decision.hours, decision.entitlement_class.value, decision.rule_id, decision.ruleset_version,
                 decision.reason, json.dumps(decision.inputs, sort_keys=True), decision.status.value),
            )
            tx.execute("UPDATE lock_decision_triggers SET processed_at = ? WHERE id = ?", (_iso(now), decision.trigger_id))
            return decision

        def events(tx: Transaction, _state: object, result: Decision | None) -> None:
            if result is None:
                return
            write_event(
                tx,
                DomainEvent(
                    event_type="lock_decision.recorded", source_module="rules_engine",
                    payload={
                        "decision_id": result.id, "trust_domain": result.trust_domain,
                        "action_type": result.action_type.value, "hours": result.hours,
                        "status": result.status.value,
                    },
                    occurred_at=now,
                ),
            )

        written = apply_transition(self._core, write=write, events=events)
        return written if written is not None else self.get_decision_for_trigger(decision.trigger_id)

    # -------------------------------------------------------------------
    # Reads
    # -------------------------------------------------------------------

    def get_decision_for_trigger(self, trigger_id: str) -> Decision | None:
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT * FROM lock_decisions WHERE trigger_id = ?", (trigger_id,))
        return _row_to_decision(row) if row is not None else None

    def list_decisions(self, *, limit: int = 50) -> list[Decision]:
        with self._core.transaction() as tx:
            rows = tx.fetch_all("SELECT * FROM lock_decisions ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,))
        return [_row_to_decision(r) for r in rows]


def _inputs_to_dict(i: RuleInputs) -> dict:
    return {
        "now": _iso(i.now), "trust_domain": i.trust_domain, "trust_score": i.trust_score,
        "trust_confidence": i.trust_confidence, "previous_trust_score": i.previous_trust_score,
        "direction": i.direction.value, "setback_streak": i.setback_streak, "progress_streak": i.progress_streak,
        "lock": None if i.lock is None else {
            "observed_at": _iso(i.lock.observed_at), "status": i.lock.status, "is_frozen": i.lock.is_frozen,
            "can_be_unlocked": i.lock.can_be_unlocked, "is_keyholder_lock": i.lock.is_keyholder_lock,
        },
        "recent_actions": [
            {"created_at": _iso(a.created_at), "action_type": a.action_type.value, "hours": a.hours}
            for a in i.recent_actions
        ],
    }


def _row_to_decision(row) -> Decision:
    return Decision(
        id=row["id"], trigger_id=row["trigger_id"], trust_domain=row["trust_domain"],
        created_at=_parse_iso(row["created_at"]), action_type=ActionType(row["action_type"]), hours=row["hours"],
        entitlement_class=EntitlementClass(row["entitlement_class"]), rule_id=row["rule_id"],
        ruleset_version=row["ruleset_version"], reason=row["reason"], inputs=json.loads(row["inputs_json"]),
        status=DecisionStatus(row["status"]),
    )
