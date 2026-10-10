"""
goal_management/models.py

Data structures for Goal Management Slice 1. Canonical:
docs/architecture/goal_technical_design.md Sections 2, 4, 5.1, 5.3.
See goal_management/README.md for exactly what this slice covers and
what is deferred (GoalAccountabilityAssessment, GoalNegotiation, the
Trust Manager integration).

GOAL-1 is enforced structurally by this file's own imports: nothing
here imports trust_manager or penalty_engine, and nothing in this
package ever will for this module's own decisions (Section 1).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


def new_id() -> str:
    return str(uuid.uuid4())


# =============================================================================
# 2.2 — Goal, GoalVersion
# =============================================================================

class GoalLifecycleStatus(StrEnum):
    """ARCHIVED deliberately does NOT appear here (3.3, GOAL-11) --
    archiving is a separate, presentation-only field, never a status."""
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    ABANDONED = "abandoned"
    REPLACED = "replaced"


@dataclass(frozen=True, kw_only=True)
class GoalVersion:
    """Append-only (GOAL-5). Adapting a Goal creates a new GoalVersion
    under the same goal_group_id; it never edits an existing one."""
    id: str = field(default_factory=new_id)
    goal_group_id: str
    version: int
    title: str
    target_description: str
    trust_domain: str
    created_at: datetime
    created_via: str                # 'user_proposed' | 'coach_proposed_user_approved' | 'coach_initial_setup'
    adaptation_reason: str | None = None   # REQUIRED if version > 1 (GOAL-5)
    supersedes_id: str | None = None


@dataclass(kw_only=True)
class Goal:
    """
    Mutable current-state record for a goal_group_id (the same pattern
    as penalty_windows.status). `status` and `archived_at` are the only
    mutable fields (GOAL-5) -- everything else about a Goal's content
    lives in its (append-only) GoalVersion history.
    """
    goal_group_id: str = field(default_factory=new_id)
    current_version_id: str
    status: GoalLifecycleStatus = GoalLifecycleStatus.ACTIVE
    created_at: datetime
    status_changed_at: datetime
    replaces_goal_group_id: str | None = None
    archived_at: datetime | None = None    # independent of status (3.3, GOAL-11)


# =============================================================================
# 2.6, 4.1 — GoalOutcome, GoalEvidence
# =============================================================================

class GoalOutcome(StrEnum):
    """MET corresponds to Goal Success; MISSED corresponds to Goal
    Failure; PARTIALLY_MET is neither -- its own, distinct outcome."""
    MET = "met"
    PARTIALLY_MET = "partially_met"
    MISSED = "missed"


@dataclass(frozen=True, kw_only=True)
class GoalEvidence:
    """Append-only (GOAL-4). Represents ONE evaluation period's outcome
    against ONE GoalVersion's target. A fact, not a verdict (4.3,
    GOAL-2) -- never by itself triggers anything."""
    id: str = field(default_factory=new_id)
    goal_group_id: str
    goal_version_id: str
    period_start: datetime
    period_end: datetime
    outcome: GoalOutcome
    observed_progress: str
    source: str              # 'check_in' | 'user_report' | 'system_derived'
    created_at: datetime


# =============================================================================
# 5.1 — GoalEvaluation, GoalInterventionType
# =============================================================================

class GoalInterventionType(StrEnum):
    ADAPT_TARGET = "adapt_target"
    INCREASE_SUPPORT = "increase_support"       # no effect on the Goal itself in this slice
    NO_CHANGE = "no_change"                     # no effect; recorded for audit only
    PROPOSE_REPLACEMENT = "propose_replacement"
    PROPOSE_ABANDONMENT = "propose_abandonment"


@dataclass(frozen=True, kw_only=True)
class GoalEvaluation:
    """
    Append-only. The Coach's structured response to one or more
    GoalEvidence records (GOAL-3: triggering_evidence_ids non-empty).
    Deliberately has NO field answering the accountability question
    (GOAL-9) -- that belongs exclusively to GoalAccountabilityAssessment,
    which this slice defers (see goal_management/README.md).

    `findings`/`proposed_intervention` are recorded here as plain
    parameters, the same way `recovery_plan.propose_task()`'s
    title/description are -- this slice builds the mechanism for
    recording an evaluation's content, not the AI reasoning that will
    eventually author that content (no ai/coach_engine exists yet).
    """
    id: str = field(default_factory=new_id)
    goal_group_id: str
    created_at: datetime
    triggering_evidence_ids: tuple[str, ...]   # non-empty (GOAL-3)
    findings: str
    proposed_intervention: GoalInterventionType
    proposed_intervention_detail: str


# =============================================================================
# 5.3 — GoalChangeProposal, GoalChangeProposalContent
# =============================================================================

class GoalProposalStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    EXPIRED = "expired"


@dataclass(kw_only=True)
class GoalChangeProposal:
    """Mutable. The operation type and its expiry -- the confirmable
    CONTENT lives in GoalChangeProposalContent."""
    id: str = field(default_factory=new_id)
    evaluation_id: str | None = None    # None if user-initiated rather than Coach-proposed
    goal_group_id: str
    proposed_change: GoalInterventionType
    proposal_expires_at: datetime
    status: GoalProposalStatus = GoalProposalStatus.PENDING
    created_at: datetime
    resolved_at: datetime | None = None


@dataclass(frozen=True, kw_only=True)
class GoalChangeProposalContent:
    """
    Append-only, immutable (GOAL-6). The SPECIFIC content the user is
    actually confirming -- acceptance always applies exactly this
    recorded payload, never content reconstructed from context at
    acceptance time.
    """
    id: str = field(default_factory=new_id)
    proposal_id: str
    proposed_title: str | None = None
    proposed_target_description: str | None = None
    proposed_replacement_goal_group_id: str | None = None
    reason: str


class GoalNotFoundError(LookupError):
    def __init__(self, goal_group_id: str) -> None:
        super().__init__(f"No Goal with goal_group_id={goal_group_id!r}")
        self.goal_group_id = goal_group_id


class GoalChangeProposalNotFoundError(LookupError):
    def __init__(self, proposal_id: str) -> None:
        super().__init__(f"No GoalChangeProposal with id={proposal_id!r}")
        self.proposal_id = proposal_id


class InvalidGoalTransitionError(ValueError):
    """Raised when a Goal's CURRENT status does not permit the
    requested transition -- e.g. pausing an already-ABANDONED Goal, or
    archiving one that is not yet terminal (GOAL-11)."""
    def __init__(self, goal_group_id: str, current_status: str, requested_action: str, allowed_from: tuple[str, ...]) -> None:
        super().__init__(
            f"Cannot {requested_action} Goal {goal_group_id!r} from status {current_status!r} "
            f"-- only permitted from {allowed_from}."
        )
        self.goal_group_id = goal_group_id
        self.current_status = current_status


class InvalidProposalStateError(ValueError):
    """Raised when a GoalChangeProposal is not PENDING but
    accept/decline is attempted anyway."""
    def __init__(self, proposal_id: str, current_status: str) -> None:
        super().__init__(f"GoalChangeProposal {proposal_id!r} is {current_status!r}, not 'pending' -- cannot resolve it again.")
        self.proposal_id = proposal_id
        self.current_status = current_status


# =============================================================================
# 6 -- GoalAccountabilityAssessment (Phase 1: the Goal -> Trust bridge)
# =============================================================================

class GoalAccountabilityDirection(StrEnum):
    PROGRESS = "progress"
    SETBACK = "setback"
    NEUTRAL = "neutral"


def derive_accountability_direction(outcomes: tuple[GoalOutcome, ...]) -> GoalAccountabilityDirection:
    """
    Deterministic default direction for an evaluation, from the
    outcomes of the GoalEvidence it was triggered by. All MET ->
    PROGRESS; all MISSED -> SETBACK; anything else (PARTIALLY_MET, or a
    mix) -> NEUTRAL. Pure; no model involvement.
    """
    if outcomes and all(o == GoalOutcome.MET for o in outcomes):
        return GoalAccountabilityDirection.PROGRESS
    if outcomes and all(o == GoalOutcome.MISSED for o in outcomes):
        return GoalAccountabilityDirection.SETBACK
    return GoalAccountabilityDirection.NEUTRAL


@dataclass(frozen=True, kw_only=True)
class GoalAccountabilityAssessment:
    """Append-only. At most one per GoalEvaluation (GOAL-13). See
    migration 024. GoalNegotiation fields are deferred."""
    id: str = field(default_factory=new_id)
    evaluation_id: str
    goal_group_id: str
    trust_domain: str
    created_at: datetime
    relevant_to_trust: bool
    direction: GoalAccountabilityDirection
    rationale: str


class GoalEvaluationNotFoundError(LookupError):
    def __init__(self, evaluation_id: str) -> None:
        super().__init__(f"No GoalEvaluation with id={evaluation_id!r}")


class AssessmentAlreadyExistsError(ValueError):
    """GOAL-13: a GoalEvaluation already has its one assessment."""
    def __init__(self, evaluation_id: str) -> None:
        super().__init__(f"GoalEvaluation {evaluation_id!r} already has a GoalAccountabilityAssessment (GOAL-13)")
