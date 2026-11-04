"""
rules_engine/models.py

Data structures for the Rules Engine (Phase 2, dry-run).
Canonical: docs/architecture/rules_engine_phase2_proposal.md and
relationship_decision_engine_technical_design.md (Decision, Entitlement).

Nothing here imports conversation_engine, application, bot or chaster:
the engine is pure domain logic that can never be reached by a model.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


def new_id() -> str:
    return str(uuid.uuid4())


class ActionType(StrEnum):
    NO_ACTION = "no_action"
    FREEZE = "freeze"
    UNFREEZE = "unfreeze"
    # Deliberately absent: any code / combination reveal (excluded from
    # Phase 2 by owner decision; irreversible and in tension with
    # philosophy.md 4.2), and any time change: the system never adds to or
    # removes from a lock's duration (owner decision, Zero-Time-Modification).


class EntitlementClass(StrEnum):
    ABSOLUTE = "absolute"          # safety gates; outcome fixed regardless of anything else
    GUARANTEED = "guaranteed"      # fixed pre-agreed rule applied to current state
    DISCRETIONARY = "discretionary"  # reserved; the Phase 2 engine never emits it


class DecisionStatus(StrEnum):
    DRY_RUN = "dry_run"            # recorded, not executed
    # Advanced only by chaster_execution (the single writer of lock_decisions.status):
    PENDING = "pending"
    EXECUTING = "executing"
    EXECUTED = "executed"
    FAILED = "failed"
    BLOCKED = "blocked"
    EXPIRED = "expired"
    NEEDS_REVIEW = "needs_review"


class GoalDirection(StrEnum):
    PROGRESS = "progress"
    SETBACK = "setback"
    NEUTRAL = "neutral"


@dataclass(frozen=True, kw_only=True)
class LockSnapshot:
    """Provider-reported lock state, as the engine needs it. Any field the
    provider did not confirm is None, and None always fails closed."""
    observed_at: datetime
    status: str | None             # 'locked' | 'unlocked' | 'deserted'
    is_frozen: bool | None
    can_be_unlocked: bool | None
    is_keyholder_lock: bool | None


@dataclass(frozen=True, kw_only=True)
class RecentAction:
    """A previously recorded actionable (non-NO_ACTION) decision."""
    created_at: datetime
    action_type: ActionType
    hours: int


@dataclass(frozen=True, kw_only=True)
class RuleInputs:
    now: datetime
    trust_domain: str
    trust_score: float | None
    trust_confidence: float | None
    previous_trust_score: float | None     # score recorded by the previous decision for this domain
    direction: GoalDirection
    setback_streak: int                    # consecutive latest non-neutral setbacks incl. the trigger
    progress_streak: int
    lock: LockSnapshot | None
    recent_actions: tuple[RecentAction, ...] = ()


@dataclass(frozen=True, kw_only=True)
class Rule:
    """
    One row of the ruleset. All stated conditions must hold (AND). A rule
    with no condition at all is rejected by validation. `direction` is the
    trigger's goal direction.
    """
    id: str
    description: str
    action: ActionType
    hours: int = 0
    direction: GoalDirection | None = None
    min_setback_streak: int | None = None
    min_progress_streak: int | None = None
    trust_below: float | None = None           # score < value
    trust_at_least: float | None = None        # score >= value
    min_confidence: float = 0.0
    crossed_below: float | None = None         # previous >= value > current
    crossed_above: float | None = None         # previous < value <= current


@dataclass(frozen=True, kw_only=True)
class Ruleset:
    version: str
    rules: tuple[Rule, ...]                    # priority order: the first match wins


@dataclass(frozen=True, kw_only=True)
class DecisionOutcome:
    """The pure result of `evaluate()`: no id, no timestamp."""
    action_type: ActionType
    hours: int
    entitlement_class: EntitlementClass
    rule_id: str | None
    ruleset_version: str
    reason: str


@dataclass(frozen=True, kw_only=True)
class Decision:
    """Persisted, append-only. Phase 2: status is always DRY_RUN."""
    id: str = field(default_factory=new_id)
    trigger_id: str
    trust_domain: str
    created_at: datetime
    action_type: ActionType
    hours: int
    entitlement_class: EntitlementClass
    rule_id: str | None
    ruleset_version: str
    reason: str
    inputs: dict
    status: DecisionStatus = DecisionStatus.DRY_RUN
