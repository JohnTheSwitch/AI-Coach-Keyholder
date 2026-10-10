"""
trust_manager/goal_scoring.py

Pure, deterministic scoring for Goal-sourced TrustEvidence
(trust_manager_technical_design.md Section 15; Phase 1). No database
access, no model involvement -- the same discipline as severity.py.

Setbacks deliberately weigh more than progress (asymmetric: trust is
lost faster than it is rebuilt). The values are approved Phase 1
defaults; the existing per-row cap (MAX_ABS_EFFECTIVE_WEIGHT) and the
per-recalculation delta cap (TI19) still bound their effect.
"""

from __future__ import annotations

from enum import StrEnum

from trust_manager.models import EvidenceType

__all__ = [
    "GoalAssessmentDirection",
    "GOAL_PROGRESS_BASE_WEIGHT",
    "GOAL_SETBACK_BASE_WEIGHT",
    "GOAL_ASSESSMENT_DEFAULT_CONFIDENCE",
    "goal_evidence_type",
    "goal_evidence_weight",
    "goal_evidence_confidence",
]


class GoalAssessmentDirection(StrEnum):
    """Mirrors goal_management's direction values; defined here so this
    module never imports goal_management (the event payload carries the
    plain string)."""

    PROGRESS = "progress"
    SETBACK = "setback"
    NEUTRAL = "neutral"


# BOOTSTRAP_DEFAULT(owner=undecided, mechanism=code):
# Approved Phase 1 value: a MET goal outcome is a modest positive signal.
GOAL_PROGRESS_BASE_WEIGHT = 0.05

# BOOTSTRAP_DEFAULT(owner=undecided, mechanism=code):
# Approved Phase 1 value: a MISSED goal outcome weighs more than progress.
GOAL_SETBACK_BASE_WEIGHT = -0.08

# BOOTSTRAP_DEFAULT(owner=undecided, mechanism=code):
# A Keyholder / system-verified assessment carries full confidence.
GOAL_ASSESSMENT_DEFAULT_CONFIDENCE = 1.0


def goal_evidence_type(direction: GoalAssessmentDirection) -> EvidenceType | None:
    """None for NEUTRAL: a neutral assessment never produces evidence."""
    if direction == GoalAssessmentDirection.PROGRESS:
        return EvidenceType.GOAL_PROGRESS
    if direction == GoalAssessmentDirection.SETBACK:
        return EvidenceType.GOAL_SETBACK
    return None


def goal_evidence_weight(direction: GoalAssessmentDirection) -> float:
    """Signed raw weight (before confidence scaling). NEUTRAL has no
    evidence and therefore no weight: raises ValueError."""
    if direction == GoalAssessmentDirection.PROGRESS:
        return GOAL_PROGRESS_BASE_WEIGHT
    if direction == GoalAssessmentDirection.SETBACK:
        return GOAL_SETBACK_BASE_WEIGHT
    raise ValueError("NEUTRAL assessments produce no TrustEvidence")


def goal_evidence_confidence(direction: GoalAssessmentDirection) -> float:
    return GOAL_ASSESSMENT_DEFAULT_CONFIDENCE
