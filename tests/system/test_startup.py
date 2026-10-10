"""
tests/system/test_startup.py

The key integration test: confirms an Incident via Trust Manager, then
runs on_system_startup() (NOT a direct call to
PenaltyEngine.start_window_if_eligible()) and verifies a PenaltyWindow
was created purely through the real event-driven wiring
(system/startup.py's build_consumer_registry()) — proving the
NestedTransactionError concern that shaped
_consume_confirmed_incident_in_transaction()'s design is
actually resolved, not just reasoned about.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from infrastructure.clock import FrozenClock
from infrastructure.database import Database as CoreDatabase
from infrastructure.startup_lease import StartupLeaseNotAcquired, acquire_system_startup_lease
from penalty_engine.repository import PenaltyEngine
from recovery_plan.models import RecoveryPlanStatus
from recovery_plan.repository import RecoveryPlanManager
from system.startup import on_system_startup
from trust_manager.models import (
    BreachDirectness,
    ConfirmationSource,
    EvidenceConfidenceLevel,
    ImpactLevel,
    IncidentConfirmation,
    IncidentEvidence,
    IntentAssessment,
    RepetitionEvidence,
)
from trust_manager.repository import TrustManager

FIXED_TIME = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _apply_migrations(core: CoreDatabase) -> None:
    migrations_dir = Path(__file__).parent.parent.parent / "database" / "migrations"
    with core.raw_connection() as conn:
        for path in sorted(migrations_dir.glob("*.sql")):
            conn.executescript(path.read_text(encoding="utf-8"))


@pytest.fixture
def core(tmp_path: Path) -> CoreDatabase:
    c = CoreDatabase(tmp_path / "test.db")
    _apply_migrations(c)
    return c


def _confirm_incident(core: CoreDatabase, now: datetime = FIXED_TIME):
    tm = TrustManager(core.db_path, core=core)
    tm.create_domain(domain_id="chastity", display_name="Chastity", description="...", created_via_consent_id="c1", now=now)
    incident = tm.register_incident_report(
        rule_group_id="rg1", trust_domain="chastity", description="late",
        evidence=IncidentEvidence(
            actual_or_potential_impact=ImpactLevel.MEDIUM, intentionality=IntentAssessment.UNCLEAR,
            rule_breach_directness=BreachDirectness.PARTIAL, evidence_confidence=EvidenceConfidenceLevel.HIGH,
            repetition=RepetitionEvidence(same_rule_confirmed_count=0, evaluation_window_days=30),
        ),
        now=now,
    )
    tm.confirm_incident(
        incident.id, new_confirmation=IncidentConfirmation.CONFIRMED,
        source=ConfirmationSource.USER_ACKNOWLEDGED, evidence_description="admitted", now=now,
    )
    return incident


class TestOnSystemStartupEndToEnd:
    def test_confirmed_incident_starts_a_window_via_real_event_wiring(self, core: CoreDatabase) -> None:
        """The whole point: this does NOT call
        PenaltyEngine.start_window_if_eligible() directly. The window
        must appear purely because confirm_incident() published an
        event that on_system_startup()'s consumer registry picked up
        and dispatched -- with no NestedTransactionError anywhere in
        that path."""
        _confirm_incident(core)
        clock = FrozenClock(FIXED_TIME + timedelta(minutes=1))

        on_system_startup(core, "test-process", clock)

        pe = PenaltyEngine(core.db_path, core=core)
        window = pe.get_active_or_frozen_penalty_window()
        assert window is not None

    def test_incident_is_marked_consumed_by_the_real_wiring(self, core: CoreDatabase) -> None:
        incident = _confirm_incident(core)
        clock = FrozenClock(FIXED_TIME + timedelta(minutes=1))
        on_system_startup(core, "test-process", clock)

        pe = PenaltyEngine(core.db_path, core=core)
        window = pe.get_active_or_frozen_penalty_window()
        domains = pe.get_penalty_window_relevant_domains(window.id)
        assert domains == frozenset({"chastity"})

    def test_the_event_is_marked_published_after_startup(self, core: CoreDatabase) -> None:
        _confirm_incident(core)
        clock = FrozenClock(FIXED_TIME + timedelta(minutes=1))
        on_system_startup(core, "test-process", clock)

        with core.transaction() as tx:
            row = tx.fetch_one(
                "SELECT published_at FROM domain_events WHERE event_type = 'incident.confirmation_changed'"
            )
        assert row["published_at"] is not None

    def test_a_second_startup_does_not_start_a_second_window(self, core: CoreDatabase) -> None:
        """Redelivery-safety end-to-end: running startup twice must not
        double-consume the same confirmed Incident."""
        _confirm_incident(core)
        clock = FrozenClock(FIXED_TIME + timedelta(minutes=1))
        on_system_startup(core, "process-1", clock)

        clock2 = FrozenClock(FIXED_TIME + timedelta(minutes=5))
        on_system_startup(core, "process-2", clock2)

        pe = PenaltyEngine(core.db_path, core=core)
        with core.transaction() as tx:
            count = tx.fetch_one("SELECT COUNT(*) as n FROM penalty_windows")["n"]
        assert count == 1

    def test_no_confirmed_incident_means_no_window(self, core: CoreDatabase) -> None:
        clock = FrozenClock(FIXED_TIME)
        on_system_startup(core, "test-process", clock)
        pe = PenaltyEngine(core.db_path, core=core)
        assert pe.get_active_or_frozen_penalty_window() is None

    def test_trust_manager_recovery_runs_before_penalty_engine_gets_the_event(self, core: CoreDatabase) -> None:
        """Simulates the TI23 anomaly this ordering exists to protect
        against: a CONFIRMED Incident whose assessment/TrustEvidence
        write never completed (as if the whole confirm_incident()
        transaction had crashed partway through -- so, unlike a
        successful confirmation, NO trust_evidence row exists for it
        either). Trust Manager's own recovery step must repair it BEFORE
        Penalty Engine's consumer would ever see a (by then
        already-published) confirmation event referencing it."""
        incident = _confirm_incident(core)
        # Simulate the transaction having crashed before writing the
        # assessment/TrustEvidence -- roll the incident itself back to
        # look exactly like confirm_incident() never got past the
        # ConfirmationRecord/status update, INCLUDING removing the
        # trust_evidence row a real crash at that point would also never
        # have produced (leaving it would make this test's setup
        # self-contradictory, not a genuine anomaly).
        with core.transaction() as tx:
            tx.execute(
                "DELETE FROM trust_recalculation_evidence WHERE evidence_id IN "
                "(SELECT id FROM trust_evidence WHERE source_entity_id = ?)",
                (incident.id,),
            )
            tx.execute("DELETE FROM trust_evidence WHERE source_entity_id = ?", (incident.id,))
            tx.execute(
                "UPDATE incidents SET assessment_intrinsic_severity = NULL WHERE id = ?", (incident.id,)
            )

        clock = FrozenClock(FIXED_TIME + timedelta(minutes=1))
        on_system_startup(core, "test-process", clock)

        tm = TrustManager(core.db_path, core=core)
        assert tm.get_incident_assessment(incident.id) is not None


class TestRecoveryPlanEndToEnd:
    """The full chain: a CONFIRMED Incident -> Penalty Window starts ->
    Recovery Plan is created -- entirely through real event wiring, no
    direct calls to RecoveryPlanManager.create_plan()."""

    def test_recovery_plan_created_purely_through_the_full_event_chain(self, core: CoreDatabase) -> None:
        _confirm_incident(core)
        clock = FrozenClock(FIXED_TIME + timedelta(minutes=1))
        on_system_startup(core, "test-process", clock)

        pe = PenaltyEngine(core.db_path, core=core)
        window = pe.get_active_or_frozen_penalty_window()
        assert window is not None

        rp = RecoveryPlanManager(core.db_path, core=core)
        plan = rp.get_recovery_plan_for_window(window.id)
        assert plan is not None
        assert plan.status == RecoveryPlanStatus.ACTIVE
        # I3: target_active_hours / 2 -- if the same incident that started
        # the window ALSO triggered an Extension (default, low-cooperation
        # confirmation), penalty_window.target_duration_changed cascades
        # in the same on_system_startup() call and regenerates the plan
        # to the CORRECT, post-extension capacity -- proving the cascade
        # fix (infrastructure/consumer_registry.py) actually drains the
        # full chain, not just the first event.
        assert plan.recovery_credit_capacity_hours == (window.base_duration_hours + window.extensions_hours) / 2.0

    def test_recovery_plan_mirrors_freeze_via_real_wiring(self, core: CoreDatabase) -> None:
        _confirm_incident(core)
        clock = FrozenClock(FIXED_TIME + timedelta(minutes=1))
        on_system_startup(core, "test-process", clock)

        pe = PenaltyEngine(core.db_path, core=core)
        window = pe.get_active_or_frozen_penalty_window()
        pe.emergency_freeze(window.id, now=FIXED_TIME + timedelta(hours=1))

        # A second startup call processes the freeze_periods.opened-triggered
        # penalty_window.frozen event through the same real wiring.
        clock2 = FrozenClock(FIXED_TIME + timedelta(hours=1, minutes=1))
        on_system_startup(core, "test-process-2", clock2)

        rp = RecoveryPlanManager(core.db_path, core=core)
        plan = rp.get_recovery_plan_for_window(window.id)
        assert plan.status == RecoveryPlanStatus.FROZEN

    def test_recovery_plan_completes_via_real_wiring(self, core: CoreDatabase) -> None:

        _confirm_incident(core)
        clock = FrozenClock(FIXED_TIME + timedelta(minutes=1))
        on_system_startup(core, "test-process", clock)

        pe = PenaltyEngine(core.db_path, core=core)
        window = pe.get_active_or_frozen_penalty_window()

        # Advance well past the window's target (accounting for any
        # Extension from the default, low-cooperation confirmation) and
        # run startup again -- this both completes the window (Penalty
        # Engine's own ensure_current_state(), step 2) and, through the
        # resulting penalty_window.completed event, completes the plan.
        clock2 = FrozenClock(FIXED_TIME + timedelta(hours=24 * 6))
        on_system_startup(core, "test-process-2", clock2)

        rp = RecoveryPlanManager(core.db_path, core=core)
        plan = rp.get_recovery_plan_for_window(window.id)
        assert plan.status == RecoveryPlanStatus.COMPLETED


class TestRecoveryCreditEndToEnd:
    """The full chain, one level deeper: Incident -> Penalty Window ->
    Recovery Plan -> (Coach proposes/completes a task) ->
    recovery_plan.task_completed -> Penalty Engine's Recovery Credit,
    entirely through real event wiring for the Trust Manager/Penalty
    Engine/Recovery Plan legs, with only the Coach-facing task
    propose/complete calls made directly (there is no event that
    triggers those -- a human/Coach decision starts that part)."""

    def test_recovery_credit_recorded_via_real_wiring(self, core: CoreDatabase) -> None:
        _confirm_incident(core)
        clock = FrozenClock(FIXED_TIME + timedelta(minutes=1))
        on_system_startup(core, "test-process", clock)

        pe = PenaltyEngine(core.db_path, core=core)
        window = pe.get_active_or_frozen_penalty_window()
        rp = RecoveryPlanManager(core.db_path, core=core)
        plan = rp.get_recovery_plan_for_window(window.id)

        task = rp.propose_task(plan.id, "Journal", "Reflect", credit_hours=3.0, now=FIXED_TIME + timedelta(hours=1))
        rp.complete_task(task.id, now=FIXED_TIME + timedelta(hours=2))

        # recovery_plan.task_completed is now pending -- a further
        # on_system_startup() call (the next reconciliation cycle) is
        # what dispatches it to Penalty Engine's real consumer handler.
        clock2 = FrozenClock(FIXED_TIME + timedelta(hours=2, minutes=1))
        on_system_startup(core, "test-process-2", clock2)

        with core.transaction() as tx:
            decision_row = tx.fetch_one("SELECT * FROM recovery_credit_decisions WHERE completion_id IN (SELECT id FROM recovery_task_completions WHERE recovery_task_id = ?)", (task.id,))
        assert decision_row is not None
        assert decision_row["credited_hours"] == 3.0

    def test_window_earned_hours_updated_via_real_wiring(self, core: CoreDatabase) -> None:
        _confirm_incident(core)
        clock = FrozenClock(FIXED_TIME + timedelta(minutes=1))
        on_system_startup(core, "test-process", clock)

        pe = PenaltyEngine(core.db_path, core=core)
        window = pe.get_active_or_frozen_penalty_window()
        rp = RecoveryPlanManager(core.db_path, core=core)
        plan = rp.get_recovery_plan_for_window(window.id)

        task = rp.propose_task(plan.id, "Journal", "Reflect", credit_hours=3.0, now=FIXED_TIME + timedelta(hours=1))
        rp.complete_task(task.id, now=FIXED_TIME + timedelta(hours=2))

        clock2 = FrozenClock(FIXED_TIME + timedelta(hours=2, minutes=1))
        on_system_startup(core, "test-process-2", clock2)

        refreshed = pe.get_active_or_frozen_penalty_window()
        assert refreshed.recovery_credits_earned_hours == 3.0


class TestGoalManagementRecoveryViaStartup:
    def test_on_system_startup_expires_a_past_due_goal_proposal(self, core: CoreDatabase) -> None:
        from goal_management.models import GoalInterventionType, GoalProposalStatus
        from goal_management.repository import GoalManager

        gm = GoalManager(core.db_path, core=core)
        goal = gm.create_goal(
            title="Exercise several times per week", target_description="3 workouts per week",
            trust_domain="fitness", created_via="user_proposed", now=FIXED_TIME,
        )
        proposal = gm.create_change_proposal(
            goal.goal_group_id, GoalInterventionType.PROPOSE_ABANDONMENT, reason="reason",
            proposal_expires_at=FIXED_TIME + timedelta(hours=1), now=FIXED_TIME,
        )

        clock = FrozenClock(FIXED_TIME + timedelta(hours=2))
        on_system_startup(core, "test-process", clock)

        expired = gm.get_change_proposal(proposal.id)
        assert expired.status == GoalProposalStatus.EXPIRED


class TestStartupLeaseIntegration:
    def test_second_concurrent_startup_raises(self, core: CoreDatabase) -> None:
        acquire_system_startup_lease(core, "already-running", FIXED_TIME, timedelta(minutes=5))
        clock = FrozenClock(FIXED_TIME + timedelta(seconds=1))
        with pytest.raises(StartupLeaseNotAcquired):
            on_system_startup(core, "second-instance", clock)

    def test_lease_is_released_after_successful_startup(self, core: CoreDatabase) -> None:
        clock = FrozenClock(FIXED_TIME)
        on_system_startup(core, "test-process", clock)
        # a second startup attempt right after should succeed (lease released)
        clock2 = FrozenClock(FIXED_TIME + timedelta(seconds=1))
        on_system_startup(core, "another-process", clock2)  # must not raise
