"""
Phase 1 end-to-end: Goal outcome -> accountability assessment -> outbox
event -> TrustEvidence -> immediate recalculation, purely through the real
consumer wiring in system/startup.py. No model or Discord path involved.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from goal_management.models import GoalInterventionType, GoalOutcome
from goal_management.repository import GoalManager
from infrastructure.clock import FrozenClock
from infrastructure.database import Database as CoreDatabase
from system.startup import on_system_startup
from trust_manager.repository import TrustManager

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def core(tmp_path: Path) -> CoreDatabase:
    c = CoreDatabase(tmp_path / "test.db")
    migrations = Path(__file__).parent.parent.parent / "database" / "migrations"
    with c.raw_connection() as conn:
        for path in sorted(migrations.glob("*.sql")):
            conn.executescript(path.read_text(encoding="utf-8"))
    return c


def _setup(core: CoreDatabase, outcome: GoalOutcome):
    tm = TrustManager(core.db_path, core=core)
    tm.create_domain(domain_id="fitness", display_name="Fitness", description="d", created_via_consent_id="c1", now=NOW)
    gm = GoalManager(core.db_path, core=core)
    goal = gm.create_goal("t", "d", "fitness", "user_proposed", now=NOW)
    evidence = gm.record_evidence(
        goal.goal_group_id, goal.current_version_id, NOW, NOW + timedelta(days=7), outcome, "p", "check_in", now=NOW,
    )
    evaluation = gm.record_evaluation(
        goal.goal_group_id, (evidence.id,), "f", GoalInterventionType.NO_CHANGE, "x", now=NOW,
    )
    return tm, gm, evaluation


def _run_startup(core: CoreDatabase, name: str, minutes: int) -> None:
    on_system_startup(core, name, FrozenClock(NOW + timedelta(minutes=minutes)))


def test_met_goal_raises_trust_only_after_the_event_is_consumed(core: CoreDatabase) -> None:
    tm, gm, evaluation = _setup(core, GoalOutcome.MET)
    before = tm.get_domain_state("fitness").score
    gm.record_accountability_assessment(evaluation.id, relevant_to_trust=True, rationale="r", now=NOW)
    assert tm.get_domain_state("fitness").score == before
    _run_startup(core, "p1", 1)
    assert tm.get_domain_state("fitness").score == pytest.approx(before + 0.05)


def test_missed_goal_lowers_trust_and_redelivery_is_a_noop(core: CoreDatabase) -> None:
    tm, gm, evaluation = _setup(core, GoalOutcome.MISSED)
    before = tm.get_domain_state("fitness").score
    gm.record_accountability_assessment(evaluation.id, relevant_to_trust=True, rationale="r", now=NOW)
    _run_startup(core, "p1", 1)
    after = tm.get_domain_state("fitness").score
    assert after == pytest.approx(before - 0.08)

    # Force a genuine redelivery: re-open the event AND forget the consumer
    # dedup row, so only the TrustEvidence uniqueness guard (TI25) protects.
    with core.transaction() as tx:
        tx.execute(
            "UPDATE domain_events SET published_at = NULL WHERE event_type = 'goal_accountability_assessment.recorded'"
        )
        tx.execute("DELETE FROM domain_event_consumers")
    _run_startup(core, "p2", 2)
    assert tm.get_domain_state("fitness").score == pytest.approx(after)
    with core.transaction() as tx:
        assert tx.fetch_one("SELECT COUNT(*) AS n FROM trust_evidence")["n"] == 1


def test_partially_met_changes_nothing(core: CoreDatabase) -> None:
    tm, gm, evaluation = _setup(core, GoalOutcome.PARTIALLY_MET)
    before = tm.get_domain_state("fitness").score
    gm.record_accountability_assessment(evaluation.id, relevant_to_trust=True, rationale="r", now=NOW)
    _run_startup(core, "p1", 1)
    assert tm.get_domain_state("fitness").score == before


def test_not_relevant_changes_nothing(core: CoreDatabase) -> None:
    tm, gm, evaluation = _setup(core, GoalOutcome.MET)
    before = tm.get_domain_state("fitness").score
    gm.record_accountability_assessment(evaluation.id, relevant_to_trust=False, rationale="r", now=NOW)
    _run_startup(core, "p1", 1)
    assert tm.get_domain_state("fitness").score == before


def test_excused_missed_goal_changes_nothing(core: CoreDatabase) -> None:
    from goal_management.models import GoalAccountabilityDirection

    tm, gm, evaluation = _setup(core, GoalOutcome.MISSED)
    before = tm.get_domain_state("fitness").score
    gm.record_accountability_assessment(
        evaluation.id, relevant_to_trust=True, rationale="excused", direction=GoalAccountabilityDirection.NEUTRAL,
        now=NOW,
    )
    _run_startup(core, "p1", 1)
    assert tm.get_domain_state("fitness").score == before
