"""
trust_manager/models.py

Data structures for Trust Manager Slice 1 — Domain Registry, Domain
State, and the Incident/Confirmation/Severity model. Canonical
definitions: docs/architecture/trust_manager_technical_design.md
Sections 2.1, 2.2, 2.4, 2.8, 2.10, 5.1-5.4.

Deferred to a later slice (see trust_manager/README.md): TrustEvidenceDispute,
TrustRecalculation, OverallTrustReport, the Goal Accountability Assessment
evidence types (GOAL_PROGRESS/GOAL_SETBACK are defined in EvidenceType
below since the enum is canonical as a whole, but nothing in this slice
writes them).

These classes carry no business logic beyond their own shape — the
rubric (assess_severity, cooperation_trust_offset) lives in
trust_manager/severity.py; database access lives in
trust_manager/repository.py. Every dataclass with `created_at` follows
this project's Clock-injection convention (implementation_conventions.md;
infrastructure/clock.py) — no default_factory, no hidden timestamp.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


def new_id() -> str:
    return str(uuid.uuid4())


# =============================================================================
# 2.1 — Domain Registry
# =============================================================================

@dataclass(frozen=True, kw_only=True)
class TrustDomain:
    domain_id: str
    display_name: str
    description: str
    created_at: datetime
    created_via_consent_id: str          # TI1 -- never created without consent
    is_active: bool = True
    deactivated_at: datetime | None = None
    deactivated_via_consent_id: str | None = None
    # Specified at creation for domains introduced with pre-existing
    # context (e.g. migration) -- part of the approved consent request,
    # never a runtime decision (3.4).
    initial_score_override: float | None = None
    initial_confidence_override: float | None = None


@dataclass(frozen=True, kw_only=True)
class TrustDomainState:
    domain_id: str
    score: float                        # 0.0-1.0
    confidence: float                   # 0.0-1.0
    trend: str                          # 'improving' | 'declining' | 'stable'
    last_recalculated_at: datetime
    last_relevant_event_at: datetime | None = None


# =============================================================================
# 2.4 — Evidence Types (the enum is canonical as a whole; GOAL_PROGRESS/
# GOAL_SETBACK are defined here even though Slice 1 never writes them,
# so the type stays complete rather than partially defined)
# =============================================================================

class EvidenceType(StrEnum):
    INCIDENT_IMPACT = "incident_impact"
    RECOVERY_PROGRESS = "recovery_progress"
    SUSTAINED_PERIOD = "sustained_period"
    MANUAL_REVIEW = "manual_review"
    GOAL_PROGRESS = "goal_progress"
    GOAL_SETBACK = "goal_setback"
    CHECKIN_MISSED = "checkin_missed"                 # v0.39
    CHECKIN_INSUFFICIENT = "checkin_insufficient"     # v0.39
    CLEAN_STREAK_BONUS = "clean_streak_bonus"         # v0.39: daily recovery of a Clean Day streak
    COMMITMENT_NOT_KEPT = "commitment_not_kept"       # Phase J3: the wearer reported a daily commitment as not kept
    CHECKIN_ANSWERED = "checkin_answered"             # owner, 2026-11-13: a check-in answered on time and well enough
    HYGIENE_ON_TIME = "hygiene_on_time"               # owner, 2026-11-13: a planned hygiene opening relocked on time
    COMMITMENT_KEPT = "commitment_kept"               # owner, 2026-11-13: the wearer reported a daily commitment as kept
    PROOF_VERIFIED = "proof_verified"                 # owner, 2026-11-13: a spot check's photo was verified by the Chaster community (Honesty)


@dataclass(frozen=True, kw_only=True)
class TrustEvidence:
    """
    Append-only (TI3): no field changes after creation, and the access
    layer (trust_manager/repository.py) provides no UPDATE/DELETE for
    this table at all.
    """
    id: str = field(default_factory=new_id)
    domain_id: str
    created_at: datetime
    evidence_type: EvidenceType
    source_entity_type: str             # 'incident' | 'recovery_credit_ledger' | 'manual_review' | ...
    source_entity_id: str
    raw_weight: float                   # signed, BEFORE confidence scaling
    evidence_confidence: float          # 0.0-1.0
    explanation: str


# =============================================================================
# 2.8 — Confirmation
# =============================================================================

class IncidentConfirmation(StrEnum):
    UNCONFIRMED = "unconfirmed"
    PROVISIONAL = "provisional"
    CONFIRMED = "confirmed"


class ConfirmationSource(StrEnum):
    SYSTEM_VERIFIED = "system_verified"
    USER_ACKNOWLEDGED = "user_acknowledged"
    KEYHOLDER_REVIEW = "keyholder_review"


@dataclass(frozen=True, kw_only=True)
class ConfirmationRecord:
    """Append-only. The only way an Incident advances between confirmation levels."""
    id: str = field(default_factory=new_id)
    incident_id: str
    created_at: datetime
    previous_confirmation: IncidentConfirmation
    new_confirmation: IncidentConfirmation
    source: ConfirmationSource
    evidence_description: str           # TI16 -- always required


# =============================================================================
# 2.10 — Incident and Severity
# =============================================================================

class ImpactLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class IntentAssessment(StrEnum):
    UNINTENTIONAL = "unintentional"
    UNCLEAR = "unclear"                 # default
    DELIBERATE = "deliberate"


class BreachDirectness(StrEnum):
    INDIRECT = "indirect"
    PARTIAL = "partial"
    DIRECT = "direct"


class EvidenceConfidenceLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True, kw_only=True)
class RepetitionEvidence:
    """
    Replaces a boolean 'repetition' flag. same_rule_confirmed_count
    counts ONLY Incidents with confirmation=CONFIRMED (TI17).
    source_incident_ids may include already-consumed Incidents (5.4) --
    this is historical evidence of a pattern for Trust, a read-only fact
    entirely independent of the Penalty Engine's own single-use
    consumption bookkeeping, which the Trust Manager never reads.
    """
    same_rule_confirmed_count: int
    evaluation_window_days: int
    source_incident_ids: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class IncidentEvidence:
    actual_or_potential_impact: ImpactLevel
    rule_breach_directness: BreachDirectness
    repetition: RepetitionEvidence
    evidence_confidence: EvidenceConfidenceLevel
    intentionality: IntentAssessment = IntentAssessment.UNCLEAR


@dataclass(frozen=True, kw_only=True)
class CooperationAssessment:
    """
    Separate from IncidentEvidence -- cooperation is a property of the
    RESPONSE to an Incident, never of the Incident itself (5.3). Never
    part of assess_severity()'s input (TI5).
    """
    self_disclosed: bool = False
    active_cooperation_in_resolution: bool = False
    notes: str | None = None


class SeverityTier(StrEnum):
    MINOR = "minor"
    MODERATE = "moderate"
    MAJOR = "major"
    CRITICAL = "critical"


@dataclass(frozen=True, kw_only=True)
class IncidentAssessment:
    """
    intrinsic_severity DOES NOT depend on cooperation or confirmation
    source (TI5) -- two Incidents with identical impact/circumstances
    always produce the same intrinsic_severity, self-reported or not.
    """
    intrinsic_severity: SeverityTier
    confirmation: IncidentConfirmation
    cooperation: CooperationAssessment
    evidence: IncidentEvidence
    rubric_explanation: str


@dataclass(kw_only=True)
class Incident:
    """
    Owned entirely by the Trust Manager -- confirmation, assessment, and
    all descriptive fields (TI22). Does NOT track which Penalty Window
    (if any) consumed it -- that bookkeeping belongs exclusively to the
    Penalty Engine's own incident_consumption table (a future slice),
    which references this id but never duplicates this shape.

    Mutable-with-status, not append-only (implementation_conventions.md
    Section 7): `confirmation` and `assessment` are "what is true right
    now," with history living in the append-only ConfirmationRecord
    trail, not in old field values of this row.
    """
    id: str = field(default_factory=new_id)
    created_at: datetime
    rule_group_id: str
    trust_domain: str
    description: str
    evidence: IncidentEvidence
    confirmation: IncidentConfirmation = IncidentConfirmation.UNCONFIRMED
    assessment: IncidentAssessment | None = None    # None until confirmation reaches CONFIRMED (TI15)


@dataclass(frozen=True, kw_only=True)
class ConfirmedIncidentSummary:
    """
    A minimal, read-only projection -- deliberately NOT the full
    Incident/IncidentAssessment (13). Exposes only what a future Penalty
    Engine needs to decide whether to start a new Penalty Window.

    `rule_group_id` added for Extension (extension_technical_design.md
    EXT-2): the Penalty Engine's own current-window-scoped repetition
    count needs it, and it is a plain descriptive fact already on
    Incident, not an interpretation -- exposing it here does not violate
    Domain Interpretation (2.11) the way exposing `assessment` directly
    would.
    """
    id: str
    trust_domain: str
    rule_group_id: str
    created_at: datetime


# =============================================================================
# 2.6 — Score Recalculation (Slice 2)
# =============================================================================

@dataclass(frozen=True, kw_only=True)
class TrustRecalculation:
    """Append-only. TrustDomainState.score/confidence change exclusively
    as a side effect of writing one of these, in the same transaction (TI2)."""
    id: str = field(default_factory=new_id)
    domain_id: str
    created_at: datetime
    previous_score: float
    new_score: float
    previous_confidence: float
    new_confidence: float
    triggered_by: str            # 'incident' | 'window_completion' | 'scheduled_review' | 'manual'
    explanation: str             # TI10 -- always required


@dataclass(frozen=True, kw_only=True)
class ExposureRecord:
    """
    Input to deciding whether SUSTAINED_PERIOD evidence is created at
    all (3.7) -- defined here for completeness of the canonical model;
    not yet produced or consumed by any Slice 2 code path (that requires
    the scheduled_review trigger, deferred -- see trust_manager/README.md).
    """
    domain_id: str
    period_start: datetime
    period_end: datetime
    opportunity_count: int
    successful_observation_count: int
    monitoring_coverage: float    # 0.0-1.0
