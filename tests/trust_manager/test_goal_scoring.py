"""Pure tests for trust_manager/goal_scoring.py (Phase 1)."""

from __future__ import annotations

import pytest

from trust_manager.goal_scoring import (
    GOAL_ASSESSMENT_DEFAULT_CONFIDENCE,
    GOAL_PROGRESS_BASE_WEIGHT,
    GOAL_SETBACK_BASE_WEIGHT,
    GoalAssessmentDirection as D,
    goal_evidence_confidence,
    goal_evidence_type,
    goal_evidence_weight,
)
from trust_manager.models import EvidenceType


class TestGoalScoring:
    def test_approved_constants(self) -> None:
        assert GOAL_PROGRESS_BASE_WEIGHT == 0.05
        assert GOAL_SETBACK_BASE_WEIGHT == -0.08
        assert GOAL_ASSESSMENT_DEFAULT_CONFIDENCE == 1.0

    def test_setback_weighs_more_than_progress(self) -> None:
        assert abs(goal_evidence_weight(D.SETBACK)) > abs(goal_evidence_weight(D.PROGRESS))

    def test_weights_have_the_right_sign(self) -> None:
        assert goal_evidence_weight(D.PROGRESS) > 0 > goal_evidence_weight(D.SETBACK)

    def test_evidence_types(self) -> None:
        assert goal_evidence_type(D.PROGRESS) == EvidenceType.GOAL_PROGRESS
        assert goal_evidence_type(D.SETBACK) == EvidenceType.GOAL_SETBACK
        assert goal_evidence_type(D.NEUTRAL) is None

    def test_neutral_has_no_weight(self) -> None:
        with pytest.raises(ValueError):
            goal_evidence_weight(D.NEUTRAL)

    def test_confidence_is_default_for_every_direction(self) -> None:
        for d in D:
            assert goal_evidence_confidence(d) == 1.0
