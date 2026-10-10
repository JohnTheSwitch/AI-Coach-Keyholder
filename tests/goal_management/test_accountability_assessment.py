"""Tests for GoalManager.record_accountability_assessment (migration 024)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from goal_management.models import (
    AssessmentAlreadyExistsError,
    GoalAccountabilityDirection as Dir,
    GoalEvaluationNotFoundError,
    GoalInterventionType,
    GoalOutcome,
    derive_accountability_direction,
)
from goal_management.repository import GoalManager
from infrastructure.database import Database as CoreDatabase

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def core(tmp_path: Path) -> CoreDatabase:
    c = CoreDatabase(tmp_path / "test.db")
    migrations = Path(__file__).parent.parent.parent / "database" / "migrations"
    with c.raw_connection() as conn:
        for path in sorted(migrations.glob("*.sql")):
            conn.executescript(path.read_text(encoding="utf-8"))
    return c


@pytest.fixture
def gm(core: CoreDatabase) -> GoalManager:
    return GoalManager(core.db_path, core=core)


def _evaluation(gm: GoalManager, *outcomes: GoalOutcome):
    goal = gm.create_goal("t", "d", "fitness", "user_proposed", now=NOW)
    ids = tuple(
        gm.record_evidence(
            goal.goal_group_id, goal.current_version_id, NOW, NOW + timedelta(days=7), o, "p", "check_in", now=NOW,
        ).id
        for o in outcomes
    )
    return gm.record_evaluation(goal.goal_group_id, ids, "f", GoalInterventionType.NO_CHANGE, "x", now=NOW)


class TestDerivation:
    @pytest.mark.parametrize("outcomes,expected", [
        ((GoalOutcome.MET,), Dir.PROGRESS),
        ((GoalOutcome.MET, GoalOutcome.MET), Dir.PROGRESS),
        ((GoalOutcome.MISSED,), Dir.SETBACK),
        ((GoalOutcome.PARTIALLY_MET,), Dir.NEUTRAL),
        ((GoalOutcome.MET, GoalOutcome.MISSED), Dir.NEUTRAL),
        ((), Dir.NEUTRAL),
    ])
    def test_direction(self, outcomes, expected) -> None:
        assert derive_accountability_direction(outcomes) == expected


class TestRecordAssessment:
    def test_met_is_progress_with_goal_trust_domain(self, gm) -> None:
        ev = _evaluation(gm, GoalOutcome.MET)
        a = gm.record_accountability_assessment(ev.id, relevant_to_trust=True, rationale="r", now=NOW)
        assert a.direction == Dir.PROGRESS and a.trust_domain == "fitness"
        assert gm.get_accountability_assessment(a.id) == a

    def test_missed_is_setback(self, gm) -> None:
        ev = _evaluation(gm, GoalOutcome.MISSED)
        a = gm.record_accountability_assessment(ev.id, relevant_to_trust=True, rationale="r", now=NOW)
        assert a.direction == Dir.SETBACK

    def test_partially_met_is_neutral(self, gm) -> None:
        ev = _evaluation(gm, GoalOutcome.PARTIALLY_MET)
        a = gm.record_accountability_assessment(ev.id, relevant_to_trust=True, rationale="r", now=NOW)
        assert a.direction == Dir.NEUTRAL

    def test_caller_may_neutralise(self, gm) -> None:
        ev = _evaluation(gm, GoalOutcome.MISSED)
        a = gm.record_accountability_assessment(
            ev.id, relevant_to_trust=True, rationale="excused", direction=Dir.NEUTRAL, now=NOW,
        )
        assert a.direction == Dir.NEUTRAL

    def test_caller_cannot_assert_unsupported_direction(self, gm) -> None:
        ev = _evaluation(gm, GoalOutcome.MISSED)
        with pytest.raises(ValueError):
            gm.record_accountability_assessment(
                ev.id, relevant_to_trust=True, rationale="r", direction=Dir.PROGRESS, now=NOW,
            )

    def test_goal_13_one_assessment_per_evaluation(self, gm) -> None:
        ev = _evaluation(gm, GoalOutcome.MET)
        gm.record_accountability_assessment(ev.id, relevant_to_trust=True, rationale="r", now=NOW)
        with pytest.raises(AssessmentAlreadyExistsError):
            gm.record_accountability_assessment(ev.id, relevant_to_trust=True, rationale="r2", now=NOW)

    def test_unknown_evaluation(self, gm) -> None:
        with pytest.raises(GoalEvaluationNotFoundError):
            gm.record_accountability_assessment("nope", relevant_to_trust=True, rationale="r", now=NOW)

    def test_blank_rationale_rejected(self, gm) -> None:
        ev = _evaluation(gm, GoalOutcome.MET)
        with pytest.raises(ValueError):
            gm.record_accountability_assessment(ev.id, relevant_to_trust=True, rationale="  ", now=NOW)

    def test_event_is_emitted_even_when_not_relevant(self, gm, core) -> None:
        ev = _evaluation(gm, GoalOutcome.MET)
        a = gm.record_accountability_assessment(ev.id, relevant_to_trust=False, rationale="r", now=NOW)
        with core.transaction() as tx:
            row = tx.fetch_one(
                "SELECT payload_json FROM domain_events WHERE event_type = 'goal_accountability_assessment.recorded'"
            )
        assert a.id in row["payload_json"]
