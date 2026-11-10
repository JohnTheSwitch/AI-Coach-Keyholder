"""
Phase 2 integration (dry run): Goal assessment -> outbox -> rules_engine trigger
-> evaluate_pending -> persisted Decision. Also the isolation / flag guards.
"""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from goal_management.models import GoalInterventionType, GoalOutcome
from goal_management.repository import GoalManager
from infrastructure.clock import FrozenClock
from infrastructure.database import Database as CoreDatabase
from tests.rules_engine.test_engine import ENGINE_TEST_RULESET
from rules_engine.models import ActionType as A
from rules_engine.models import DecisionStatus, LockSnapshot
from rules_engine.repository import RulesEngineService
from system.startup import on_system_startup
from trust_manager.repository import TrustManager

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).parent.parent.parent


@pytest.fixture
def core(tmp_path: Path) -> CoreDatabase:
    c = CoreDatabase(tmp_path / "test.db")
    with c.raw_connection() as conn:
        for path in sorted((ROOT / "database" / "migrations").glob("*.sql")):
            conn.executescript(path.read_text(encoding="utf-8"))
    return c


def _world(core: CoreDatabase, snapshot=True):
    tm = TrustManager(core.db_path, core=core)
    tm.create_domain(domain_id="fitness", display_name="F", description="d", created_via_consent_id="c1", now=NOW)
    gm = GoalManager(core.db_path, core=core)
    calls = {"n": 0}

    def provider():
        calls["n"] += 1
        if not snapshot:
            return None
        return LockSnapshot(observed_at=NOW, status="locked", is_frozen=False, can_be_unlocked=False,
                            is_keyholder_lock=True)

    engine = RulesEngineService(core.db_path, core=core, get_trust_state=tm.get_domain_state, get_lock_snapshot=provider,
                                ruleset=ENGINE_TEST_RULESET)      # the pipeline itself; the shipped ruleset is tested below
    return tm, gm, engine, calls


def _assess(gm: GoalManager, outcome: GoalOutcome, *, relevant=True, at: datetime = NOW):
    goal = gm.create_goal("t", "d", "fitness", "user_proposed", now=at)
    ev = gm.record_evidence(goal.goal_group_id, goal.current_version_id, at, at + timedelta(days=7), outcome, "p",
                            "check_in", now=at)
    evaluation = gm.record_evaluation(goal.goal_group_id, (ev.id,), "f", GoalInterventionType.NO_CHANGE, "x", now=at)
    return gm.record_accountability_assessment(evaluation.id, relevant_to_trust=relevant, rationale="r", now=at)


def _startup(core, engine, minutes=1, name="p"):
    on_system_startup(core, name, FrozenClock(NOW + timedelta(minutes=minutes)), engine)


def _lock_decisions(core):
    with core.transaction() as tx:
        return tx.fetch_all("SELECT * FROM lock_decisions ORDER BY created_at, rowid")


class TestDryRunPipeline:
    def test_a_goal_assessment_records_one_dry_run_decision(self, core) -> None:
        tm, gm, engine, _ = _world(core)
        _assess(gm, GoalOutcome.MISSED)
        _startup(core, engine)
        (d,) = engine.list_decisions()
        assert d.action_type == A.FREEZE and d.hours == 0 and d.status == DecisionStatus.DRY_RUN
        assert d.rule_id == "setback" and d.reason and d.ruleset_version == "1"
        assert d.inputs["trust_domain"] == "fitness" and d.inputs["lock"]["is_keyholder_lock"] is True

    def test_nothing_is_executed_no_chaster_write_exists(self, core) -> None:
        tm, gm, engine, _ = _world(core)
        _assess(gm, GoalOutcome.MISSED)
        _startup(core, engine)
        assert {r["status"] for r in _lock_decisions(core)} == {"dry_run"}

    def test_no_lock_data_gives_no_action(self, core) -> None:
        tm, gm, engine, _ = _world(core, snapshot=False)
        _assess(gm, GoalOutcome.MISSED)
        _startup(core, engine)
        (d,) = engine.list_decisions()
        assert d.action_type == A.NO_ACTION and "no lock data" in d.reason

    def test_stale_lock_data_gives_no_action(self, core) -> None:
        tm, gm, engine, _ = _world(core)
        _assess(gm, GoalOutcome.MISSED)
        _startup(core, engine, minutes=30)   # snapshot observed at NOW is now 30 minutes old
        (d,) = engine.list_decisions()
        assert d.action_type == A.NO_ACTION and "too old" in d.reason

    def test_excused_or_not_relevant_is_neutral_no_action(self, core) -> None:
        tm, gm, engine, _ = _world(core)
        _assess(gm, GoalOutcome.MISSED, relevant=False)
        _startup(core, engine)
        (d,) = engine.list_decisions()
        assert d.action_type == A.NO_ACTION and d.inputs["direction"] == "neutral"

    def test_rerunning_startup_never_duplicates_a_decision(self, core) -> None:
        tm, gm, engine, _ = _world(core)
        _assess(gm, GoalOutcome.MISSED)
        _startup(core, engine, 1, "p1")
        _startup(core, engine, 2, "p2")
        assert len(_lock_decisions(core)) == 1

    def test_redelivered_event_records_one_trigger_and_one_decision(self, core) -> None:
        tm, gm, engine, _ = _world(core)
        _assess(gm, GoalOutcome.MISSED)
        _startup(core, engine, 1, "p1")
        with core.transaction() as tx:
            tx.execute("UPDATE domain_events SET published_at = NULL WHERE event_type = 'goal_accountability_assessment.recorded'")
            tx.execute("DELETE FROM domain_event_consumers WHERE consumer_name = 'rules_engine'")
        _startup(core, engine, 2, "p2")
        with core.transaction() as tx:
            assert tx.fetch_one("SELECT COUNT(*) AS n FROM lock_decision_triggers")["n"] == 1
        assert len(_lock_decisions(core)) == 1

    def test_a_freeze_is_never_rate_limited_across_triggers(self, core) -> None:
        tm, gm, engine, _ = _world(core)
        for i in range(3):
            _assess(gm, GoalOutcome.MISSED, at=NOW + timedelta(seconds=i))
        _startup(core, engine)
        decisions = sorted(engine.list_decisions(), key=lambda d: d.trigger_id)
        actionable = [d for d in decisions if d.action_type != A.NO_ACTION]
        assert len(actionable) == 3 and not [d for d in decisions if "cooldown" in d.reason]      # only releases are limited

    def test_setback_streak_uses_trigger_history(self, core) -> None:
        tm, gm, engine, _ = _world(core)
        _assess(gm, GoalOutcome.MISSED, at=NOW)
        _startup(core, engine, 1, "p1")
        _assess(gm, GoalOutcome.MISSED, at=NOW + timedelta(hours=5))
        on_system_startup(core, "p2", FrozenClock(NOW + timedelta(hours=5, minutes=1)), engine)
        first, second = sorted(engine.list_decisions(), key=lambda d: d.created_at)
        assert first.rule_id == "setback" and first.inputs["setback_streak"] == 1
        assert second.inputs["setback_streak"] == 2

    def test_decision_event_is_emitted(self, core) -> None:
        tm, gm, engine, _ = _world(core)
        _assess(gm, GoalOutcome.MISSED)
        _startup(core, engine)
        with core.transaction() as tx:
            n = tx.fetch_one("SELECT COUNT(*) AS n FROM domain_events WHERE event_type = 'lock_decision.recorded'")["n"]
        assert n == 1


class TestFlagAndIsolation:
    def test_without_the_engine_nothing_is_recorded(self, core) -> None:
        tm, gm, engine, calls = _world(core)
        _assess(gm, GoalOutcome.MISSED)
        on_system_startup(core, "p", FrozenClock(NOW + timedelta(minutes=1)))   # engine disabled (not passed)
        with core.transaction() as tx:
            assert tx.fetch_one("SELECT COUNT(*) AS n FROM lock_decision_triggers")["n"] == 0
        assert calls["n"] == 0

    def test_config_flag_defaults_on(self, monkeypatch) -> None:
        from core.config import Config
        assert Config.__dataclass_fields__["enable_rules_engine"].default is True
        text = (ROOT / ".env.example").read_text(encoding="utf-8")
        assert "ENABLE_RULES_ENGINE=true" in text

    def test_incident_driven_recalculations_do_not_trigger_the_engine(self) -> None:
        """Only goal assessments trigger evaluations (no double punishment of an incident)."""
        from rules_engine.repository import TRIGGER_EVENT_TYPE
        assert TRIGGER_EVENT_TYPE == "goal_accountability_assessment.recorded"

    def test_rules_engine_imports_nothing_that_can_reach_a_model_or_chaster(self) -> None:
        forbidden = ("discord", "bot", "conversation_engine", "application", "chaster", "memory_system", "ai")
        for path in (ROOT / "rules_engine").glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    assert name.split(".")[0] not in forbidden, (path.name, name)

    def test_no_other_package_except_wiring_imports_rules_engine(self) -> None:
        allowed = {"rules_engine", "chaster_execution", "system", "bot", "tests", ".venv", "venv", "env", "scripts"}
        for path in ROOT.rglob("*.py"):
            rel = path.relative_to(ROOT)
            if rel.parts[0] in allowed or rel.parts[0].startswith(".") or "__pycache__" in rel.parts:
                continue
            if len(rel.parts) == 1 and rel.name == "discord_bot.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == "rules_engine":
                    pytest.fail(f"{rel} imports rules_engine")
                if isinstance(node, ast.Import) and any(a.name.split(".")[0] == "rules_engine" for a in node.names):
                    pytest.fail(f"{rel} imports rules_engine")
