"""
database/models.py

Python dataclass representations of the data contracts agreed between
the Context, Coach, Keyholder, and Decision engines. These classes are
the "runtime" shape of the data; (de)serialization to SQLite (see
database.py) converts nested structures into JSON strings for the
*_json columns, per the schema in migrations/001_initial_schema.sql.

Targets Python 3.13 -- uses `from __future__ import annotations`,
`dataclasses`, `enum.StrEnum`, and modern typing (`list[X]`, `X | None`).

Principle: these classes contain no business logic or validation
beyond the shape of the data. Business logic belongs in
core/*_engine.py.

Phase 1.2: `created_at` is now a required constructor parameter (no
`default_factory=utc_now`; `utc_now()` removed) -- the model itself
never generates its own timestamp; the application layer supplies it
explicitly from an injected `infrastructure.clock.Clock`. Every
dataclass with `created_at` is therefore `@dataclass(kw_only=True)`,
since Python requires that required fields not follow fields with a
default (`id` has `default_factory=new_id`) -- `kw_only=True` works
around this by constructing exclusively via keyword arguments, which
matches how these classes are already called elsewhere in the code
today.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import StrEnum


# =============================================================================
# Helper functions
# =============================================================================

def new_id() -> str:
    """Generates a new UUID4 as a string -- the uniform ID format across the whole system."""
    return str(uuid.uuid4())


def iso(dt: datetime) -> str:
    """Formats a datetime into the ISO 8601 string as stored in the DB (TEXT columns)."""
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    """Parses an ISO 8601 string back into a timezone-aware datetime (UTC)."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# =============================================================================
# Enums
# =============================================================================

class FactorSource(StrEnum):
    APPLE_HEALTH = "apple_health"
    USER_REPORTED = "user_reported"
    INFERRED = "inferred"
    MANUAL_LOG = "manual_log"


class RiskDirection(StrEnum):
    OVERLOAD = "overload"
    STAGNATION = "stagnation"
    NONE = "none"


class ResolutionMethod(StrEnum):
    RULE_BASED = "rule_based"
    WEIGHTED_SCORE = "weighted_score"
    LLM_ARBITRATION = "llm_arbitration"


class ApprovalStatus(StrEnum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class ObservationType(StrEnum):
    DECISION_MADE = "decision_made"
    PERSPECTIVE_CONFLICT = "perspective_conflict"
    UNEXPECTED_OUTCOME = "unexpected_outcome"
    RECURRING_PATTERN = "recurring_pattern"
    ESTIMATION_ERROR = "estimation_error"


class ConsentTargetType(StrEnum):
    RULE = "rule"
    PHILOSOPHY = "philosophy"
    TRUST_ALGORITHM = "trust_algorithm"
    REWARD_ALGORITHM = "reward_algorithm"
    INTEGRATION = "integration"


class ConsentAction(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
    REVOKED = "revoked"


class CreatedBy(StrEnum):
    USER = "user"
    AI_PROPOSAL = "ai_proposal"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


# =============================================================================
# Context Engine
# =============================================================================

@dataclass
class ContextFactor:
    name: str
    value: float | str | bool
    source: FactorSource
    confidence: float
    observed_at: datetime

    def to_dict(self) -> dict:
        d = asdict(self)
        d["source"] = self.source.value
        d["observed_at"] = iso(self.observed_at)
        return d

    @staticmethod
    def from_dict(d: dict) -> ContextFactor:
        return ContextFactor(
            name=d["name"],
            value=d["value"],
            source=FactorSource(d["source"]),
            confidence=d["confidence"],
            observed_at=parse_iso(d["observed_at"]),
        )


@dataclass
class RelevantPattern:
    pattern_id: str
    description: str
    strength: float
    last_confirmed: datetime

    def to_dict(self) -> dict:
        d = asdict(self)
        d["last_confirmed"] = iso(self.last_confirmed)
        return d

    @staticmethod
    def from_dict(d: dict) -> RelevantPattern:
        return RelevantPattern(
            pattern_id=d["pattern_id"],
            description=d["description"],
            strength=d["strength"],
            last_confirmed=parse_iso(d["last_confirmed"]),
        )


@dataclass(kw_only=True)
class ContextSnapshot:
    id: str = field(default_factory=new_id)
    created_at: datetime
    engine_version: str = "context_engine@0.1.0"

    context_factors: list[ContextFactor] = field(default_factory=list)
    relevant_patterns: list[RelevantPattern] = field(default_factory=list)
    overall_confidence: float = 0.0
    data_freshness_hours: float = 0.0


# =============================================================================
# Coach Engine
# =============================================================================

@dataclass(kw_only=True)
class CoachAssessment:
    id: str = field(default_factory=new_id)
    created_at: datetime
    engine_version: str = "coach_engine@0.1.0"
    context_snapshot_id: str = ""

    recommendation: str = ""
    reasoning: str = ""
    confidence: float = 0.0

    risk_direction: RiskDirection = RiskDirection.NONE
    sustainability_score: float = 0.0
    supporting_factors: list[str] = field(default_factory=list)


# =============================================================================
# Keyholder Engine (+ internal TrustManager / RewardManager state)
# =============================================================================

@dataclass
class TrustState:
    trust_score: float = 0.5
    recent_trend: str = "stable"          # "improving" | "declining" | "stable"
    consecutive_failures: int = 0
    consecutive_successes: int = 0


@dataclass
class RewardState:
    eligible_rewards: list[str] = field(default_factory=list)
    pending_consequence: str | None = None
    # Reward as positive feedback (philosophy.md 2.7): this list is
    # evaluated independently of the "failure streak" logic, not as its mirror.
    positive_streak_note: str | None = None


@dataclass(kw_only=True)
class KeyholderAssessment:
    id: str = field(default_factory=new_id)
    created_at: datetime
    engine_version: str = "keyholder_engine@0.1.0"
    context_snapshot_id: str = ""

    recommendation: str = ""
    reasoning: str = ""
    confidence: float = 0.0

    consistency_score: float = 0.0
    trust_state: TrustState = field(default_factory=TrustState)
    reward_state: RewardState = field(default_factory=RewardState)
    rule_relevance: list[str] = field(default_factory=list)   # rule_group_id values


# =============================================================================
# Decision Engine
# =============================================================================

@dataclass
class ImpactScore:
    value: float = 0.0
    is_significant: bool = False
    contributing_factors: dict[str, float] = field(default_factory=dict)


@dataclass(kw_only=True)
class DecisionResult:
    id: str = field(default_factory=new_id)
    created_at: datetime
    engine_version: str = "decision_engine@0.1.0"

    context_snapshot_id: str = ""
    coach_assessment_id: str = ""
    keyholder_assessment_id: str = ""

    final_decision: str = ""
    resolution_method: ResolutionMethod = ResolutionMethod.RULE_BASED
    impact_score: ImpactScore = field(default_factory=ImpactScore)

    # Two-layer requires_user_approval -- see philosophy.md 2.5 and the
    # database schema comment: is_critical_change (hard rules) OR impact.is_significant,
    # and the safety override (philosophy.md 2.3) runs entirely outside this consideration.
    is_critical_change: bool = False
    safety_override: bool = False
    requires_user_approval: bool = False

    explanation: str | None = None
    approval_status: ApprovalStatus = ApprovalStatus.NOT_REQUIRED

    def compute_requires_approval(self) -> bool:
        """
        The canonical requires_user_approval computation. Called explicitly
        from decision_engine.py after is_critical_change and impact_score
        are populated -- not an automatic property, so tests can see
        exactly when the field gets set.
        """
        return self.is_critical_change or self.impact_score.is_significant or self.safety_override


# =============================================================================
# Observations (write-only from the runtime's perspective)
# =============================================================================

@dataclass(kw_only=True)
class ObservationRecord:
    id: str = field(default_factory=new_id)
    created_at: datetime

    observation_type: ObservationType = ObservationType.DECISION_MADE
    related_decision_id: str | None = None

    description: str = ""
    raw_data: dict = field(default_factory=dict)
    flagged_for_review: bool = False

    # Filled in exclusively by the audit export tool, never by the runtime.
    reviewed_at: datetime | None = None
    review_notes: str | None = None


# =============================================================================
# Rules & Consent
# =============================================================================

@dataclass(kw_only=True)
class Rule:
    id: str = field(default_factory=new_id)
    rule_group_id: str = field(default_factory=new_id)   # a new rule = a new group_id; a new version = the same group_id
    version: int = 1
    title: str = ""
    description: str = ""
    category: str = ""
    parameters: dict = field(default_factory=dict)

    is_active: bool = True
    supersedes_id: str | None = None

    created_at: datetime
    created_by: CreatedBy = CreatedBy.USER
    is_critical: bool = False


@dataclass(kw_only=True)
class ConsentRecord:
    id: str = field(default_factory=new_id)
    created_at: datetime

    target_type: ConsentTargetType = ConsentTargetType.RULE
    target_id: str | None = None
    target_version: str | None = None

    action: ConsentAction = ConsentAction.APPROVED
    decision_result_id: str | None = None

    explanation_shown: str | None = None
    user_comment: str | None = None


# =============================================================================
# Conversation (short-term memory / raw log)
# =============================================================================

@dataclass(kw_only=True)
class ConversationMessage:
    id: str = field(default_factory=new_id)
    created_at: datetime
    role: MessageRole = MessageRole.USER
    content: str = ""
    discord_channel_id: str | None = None
    discord_message_id: str | None = None
    related_decision_id: str | None = None
    from_model: bool | None = None          # 2026-11-13 (migration 058): for an assistant reply, whether the conversation model wrote it
