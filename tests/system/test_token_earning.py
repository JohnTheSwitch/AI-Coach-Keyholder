"""v0.39 Phase 3: a COMPLETED goal earns exactly one token, through the real outbox consumer wiring."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from goal_management.models import GoalInterventionType, GoalOutcome
from goal_management.repository import GoalManager
from infrastructure.clock import FrozenClock
from infrastructure.consumer_registry import process_pending_events
from infrastructure.database import Database as CoreDatabase
from penalty_engine.repository import PenaltyEngine
from recovery_plan.repository import RecoveryPlanManager
from release_engine.service import ReleaseEngine
from schedule_windows.windows import WindowConfig
from system.periodic import run_periodic_cycle
from system.startup import build_consumer_registry
from trust_manager.repository import TrustManager

NOW = datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).parent.parent.parent
CFG = WindowConfig.build(timezone_name="Europe/Prague", quiet_start="22:00", quiet_end="07:00",
                         work_start="07:00", work_end="16:30", holiday_country="CZ")
U = "wearer"


@pytest.fixture
def core(tmp_path: Path) -> CoreDatabase:
    c = CoreDatabase(tmp_path / "test.db")
    with c.raw_connection() as conn:
        for path in sorted((ROOT / "database" / "migrations").glob("*.sql")):
            conn.executescript(path.read_text(encoding="utf-8"))
    return c


def engine(core) -> ReleaseEngine:
    return ReleaseEngine(core.db_path, core=core, window_config=CFG, min_offset_minutes=30, max_offset_minutes=210,
                         max_hold_hours=72)


def drain(core, release_engine, minutes=1, user=U) -> None:
    registry = build_consumer_registry(
        TrustManager(core.db_path, core=core), PenaltyEngine(core.db_path, core=core),
        RecoveryPlanManager(core.db_path, core=core), release_engine=release_engine, release_user_id=user,
    )
    process_pending_events(core, registry, claimant="t", now=NOW + timedelta(minutes=minutes))


def new_goal(core, name="g"):
    TrustManager(core.db_path, core=core).create_domain(
        domain_id="fitness", display_name="F", description="d", created_via_consent_id="c1", now=NOW) \
        if TrustManager(core.db_path, core=core).get_domain_state("fitness") is None else None
    return GoalManager(core.db_path, core=core).create_goal(name, "d", "fitness", "user_proposed", now=NOW)


def ledger(core):
    with core.transaction() as tx:
        return [dict(r) for r in tx.fetch_all("SELECT delta, entry_type, reason_code, user_id FROM token_ledger ORDER BY rowid")]


def test_a_completed_goal_earns_one_token(core) -> None:
    eng = engine(core)
    gm = GoalManager(core.db_path, core=core)
    goal = new_goal(core)
    gm.complete_goal(goal.goal_group_id, "done", now=NOW)
    assert eng.get_balance(U) == 0                      # nothing before the event is consumed
    drain(core, eng)
    assert ledger(core) == [{"delta": 1, "entry_type": "earn", "reason_code": "goal.completed", "user_id": U}]
    assert eng.get_balance(U) == 1


def test_redelivery_never_earns_twice(core) -> None:
    eng = engine(core)
    goal = new_goal(core)
    GoalManager(core.db_path, core=core).complete_goal(goal.goal_group_id, "done", now=NOW)
    drain(core, eng)
    with core.transaction() as tx:
        tx.execute("UPDATE domain_events SET published_at = NULL WHERE event_type = 'goal.completed'")
        tx.execute("DELETE FROM domain_event_consumers")
    drain(core, eng, minutes=2)
    assert eng.get_balance(U) == 1


def test_each_completed_goal_earns_its_own_token(core) -> None:
    eng = engine(core)
    gm = GoalManager(core.db_path, core=core)
    first, second = new_goal(core, "a"), new_goal(core, "b")
    gm.complete_goal(first.goal_group_id, "done", now=NOW)
    gm.complete_goal(second.goal_group_id, "done", now=NOW)
    drain(core, eng)
    assert eng.get_balance(U) == 2


@pytest.mark.parametrize("outcome", [GoalOutcome.MISSED, GoalOutcome.PARTIALLY_MET])
def test_missed_or_partially_met_outcomes_earn_nothing(core, outcome) -> None:
    eng = engine(core)
    gm = GoalManager(core.db_path, core=core)
    goal = new_goal(core)
    evidence = gm.record_evidence(goal.goal_group_id, goal.current_version_id, NOW, NOW + timedelta(days=7), outcome,
                                  "p", "check_in", now=NOW)
    evaluation = gm.record_evaluation(goal.goal_group_id, (evidence.id,), "f", GoalInterventionType.NO_CHANGE, "x", now=NOW)
    gm.record_accountability_assessment(evaluation.id, relevant_to_trust=True, rationale="r", now=NOW)
    drain(core, eng)
    assert eng.get_balance(U) == 0 and ledger(core) == []


def test_paused_abandoned_and_archived_goals_earn_nothing(core) -> None:
    eng = engine(core)
    gm = GoalManager(core.db_path, core=core)
    paused = new_goal(core, "p")
    gm.pause_goal(paused.goal_group_id, "later", now=NOW)
    done = new_goal(core, "d")
    gm.complete_goal(done.goal_group_id, "done", now=NOW)
    gm.archive_goal(done.goal_group_id, now=NOW)
    drain(core, eng)
    assert eng.get_balance(U) == 1                     # only the completion; the archive event earns nothing


def test_without_a_release_engine_completed_goals_are_not_credited_later(core) -> None:
    goal = new_goal(core)
    GoalManager(core.db_path, core=core).complete_goal(goal.goal_group_id, "done", now=NOW)
    drain(core, None, user=None)                       # engine disabled: the event is consumed by nobody
    eng = engine(core)
    drain(core, eng, minutes=2)                        # enabling it later does not back-pay
    assert eng.get_balance(U) == 0


def test_the_periodic_cycle_credits_the_token(core) -> None:
    from tests.system.test_release_tick import FakeTransport  # noqa: F401  (fake edges only; no network)
    from system.release_tick import ReleaseEnginePeriodicTask
    from datetime import timedelta as td
    from rules_engine.repository import RulesEngineService
    eng = engine(core)
    rules = RulesEngineService(core.db_path, core=core, get_trust_state=lambda d: None)
    task = ReleaseEnginePeriodicTask(
        engine=eng, rules=rules, user_id=U, get_observation=lambda: None, refresh_observation=lambda now: None,
        penalty_window_active=lambda: False, supervision_active=lambda: False, last_check_in_missed=lambda: False,
        is_halted=lambda: False, can_execute=False, max_hold=td(hours=72),
    )
    goal = new_goal(core)
    GoalManager(core.db_path, core=core).complete_goal(goal.goal_group_id, "done", now=NOW)
    assert run_periodic_cycle(core, "p", FrozenClock(NOW + timedelta(minutes=1)), rules_engine=rules, executor=None,
                              release_task=task)
    assert eng.get_balance(U) == 1
