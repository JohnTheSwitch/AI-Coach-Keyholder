"""
ReleaseEnginePeriodicTask (v0.39 Phase 2) end to end with the real release engine, rules engine, executor and
ledger, a fake transport, and a controllable observation. No network.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from chaster.models import ChasterProviderObservation, ChasterProviderStatus
from chaster_execution.control import ExecutionControl
from chaster_execution.executor import ChasterExecutor
from chaster_execution.models import TransportOutcome as O
from chaster_execution.models import TransportResult
from infrastructure.database import Database as CoreDatabase
from infrastructure.time_format import parse_iso
from release_engine.service import ProviderState as P
from release_engine.service import ReleaseEngine
from rules_engine.repository import RulesEngineService
from schedule_windows.windows import WindowConfig, WindowKind, classify
from system.release_tick import ReleaseEnginePeriodicTask

ROOT = Path(__file__).parent.parent.parent
PRAGUE = ZoneInfo("Europe/Prague")
CFG = WindowConfig.build(timezone_name="Europe/Prague", quiet_start="22:00", quiet_end="07:00",
                         work_start="07:00", work_end="16:30", holiday_country="CZ")
U = "wearer"
MON = date(2026, 10, 12)


def local(d: date, hh: int, mm: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=PRAGUE).astimezone(timezone.utc)


class FakeTransport:
    def __init__(self, result=None, raises=None) -> None:
        self.calls = []
        self.result = result or TransportResult(outcome=O.OK, http_status=204)
        self.raises = raises

    def _do(self, *a):
        self.calls.append(a)
        if self.raises:
            raise self.raises
        return self.result

    def add_time(self, *, lock_id, seconds):
        return self._do("add", lock_id, seconds)

    def remove_time(self, *, lock_id, seconds):
        return self._do("remove", lock_id, seconds)

    def set_frozen(self, *, lock_id, frozen):
        return self._do("freeze" if frozen else "unfreeze", lock_id)


class World:
    def __init__(self, tmp_path: Path, *, transport=None, can_execute=True, scopes=("locks",), frozen=True,
                 frozen_since: timedelta = timedelta(hours=1), penalty=False, supervised=False, missed=False,
                 window_ok=True, status=ChasterProviderStatus.LOCKED) -> None:
        self.core = CoreDatabase(tmp_path / "test.db")
        with self.core.raw_connection() as conn:
            for path in sorted((ROOT / "database" / "migrations").glob("*.sql")):
                conn.executescript(path.read_text(encoding="utf-8"))
        self.now = local(MON, 10, 0)
        self.frozen, self.frozen_since, self.status = frozen, frozen_since, status
        self.penalty, self.supervised, self.missed = penalty, supervised, missed
        self.refreshes = 0
        self.refresh_effective = True
        self.transport = transport or FakeTransport()
        self.engine = ReleaseEngine(self.core.db_path, core=self.core, window_config=CFG, min_offset_minutes=30,
                                    max_offset_minutes=210, max_hold_hours=72, seed_factory=lambda: bytes(range(32)))
        self.rules = RulesEngineService(self.core.db_path, core=self.core, get_trust_state=lambda d: None)
        self.control = ExecutionControl(self.core.db_path, core=self.core)
        self.control.set_halted(False, reason="test", now=self.now)
        self.executor = ChasterExecutor(
            self.core.db_path, core=self.core, enabled=True, control=self.control, transport=self.transport,
            get_latest_observation=self._obs, refresh_observation=self._refresh, get_granted_scopes=lambda: scopes,
            allow_barrier_actions=True,
            window_check=(lambda now: classify(CFG, now) is WindowKind.DISCRETIONARY) if window_ok else (lambda now: False),
        )
        self.task = ReleaseEnginePeriodicTask(
            engine=self.engine, rules=self.rules, user_id=U, get_observation=self._obs,
            refresh_observation=self._refresh, penalty_window_active=lambda: self.penalty,
            supervision_active=lambda: self.supervised, last_check_in_missed=lambda: self.missed,
            is_halted=self.control.is_halted, can_execute=can_execute, max_hold=timedelta(hours=72),
        )
        self.fetched_at = None          # None = always fresh relative to self.now

    def _obs(self):
        fetched = self.fetched_at or (self.now - timedelta(minutes=1))
        return ChasterProviderObservation(
            id="o", connection_id=U, provider="chaster", chaster_lock_id="lock-1", status=self.status,
            fetched_at=fetched, created_at=fetched, is_frozen=self.frozen, has_keyholder=True,
            frozen_at=(self.now - self.frozen_since) if self.frozen else None,
        )

    def _refresh(self, now):
        self.refreshes += 1
        if self.refresh_effective:
            self.fetched_at = None

    def tokens(self, n=1):
        self.engine.earn_tokens(user_id=U, amount=n, reason_code="goal", source_event_id=f"e{n}{self.now.isoformat()}", now=self.now)

    def target(self) -> datetime:
        with self.core.transaction() as tx:
            return parse_iso(tx.fetch_one("SELECT target_at FROM release_plans WHERE status = 'planned'")["target_at"])

    def at(self, instant: datetime):
        self.now = instant
        self.fetched_at = None

    def cycle(self, instant: datetime | None = None):
        if instant is not None:
            self.at(instant)
        report = self.task.run(self.now)
        self.executor.run_once(now=self.now)
        self.task.sync(self.now, report)
        return report

    def decisions(self):
        with self.core.transaction() as tx:
            return [dict(r) for r in tx.fetch_all("SELECT action_type, rule_id, status FROM lock_decisions ORDER BY rowid")]

    def plan_statuses(self):
        with self.core.transaction() as tx:
            return [r["status"] for r in tx.fetch_all("SELECT status FROM release_plans ORDER BY rowid")]

    def auth_states(self):
        with self.core.transaction() as tx:
            return [r["state"] for r in tx.fetch_all("SELECT state FROM release_authorizations ORDER BY rowid")]


@pytest.fixture
def w(tmp_path):
    return World(tmp_path)


class TestHappyPath:
    def test_requirement_met_plans_then_releases_at_the_secret_target(self, w) -> None:
        w.tokens()
        assert "plan_created" in w.cycle().notes
        assert w.engine.get_balance(U) == 1 and w.transport.calls == []         # never instant
        w.cycle(w.now + timedelta(hours=1))
        assert w.transport.calls == []                                          # still before the target
        target = w.target()
        report = w.cycle(target)
        assert "commit:authorized" in report.notes and "release_confirmed" in report.notes
        assert w.transport.calls == [("unfreeze", "lock-1")]
        assert w.decisions() == [{"action_type": "unfreeze", "rule_id": "barrier.release", "status": "executed"}]
        assert w.auth_states() == ["provider_confirmed"] and w.engine.get_balance(U) == 0

    def test_a_confirmed_release_does_not_plan_again_without_a_new_token(self, w) -> None:
        w.tokens()
        w.cycle()
        w.cycle(w.target())
        w.cycle(w.now + timedelta(days=1))
        assert len(w.transport.calls) == 1 and w.plan_statuses() == ["authorized"]

    def test_only_one_release_per_token(self, w) -> None:
        w.tokens()
        w.cycle()
        target = w.target()
        w.cycle(target)
        w.cycle(target + timedelta(minutes=1))
        w.cycle(target + timedelta(minutes=2))
        assert len(w.transport.calls) == 1

    def test_reports_carry_no_timestamps(self, w) -> None:
        w.tokens()
        notes = w.cycle().notes
        notes += w.cycle(w.target()).notes
        assert notes and all(re.fullmatch(r"[a-z_]+(:[a-z_]+)?", n) for n in notes)

    def test_coarse_phase_is_the_only_release_signal(self, w) -> None:
        assert w.engine.coarse_phase(U) == "requirements_not_met"
        w.tokens()
        assert w.engine.coarse_phase(U) == "requirements_met_pending"
        w.cycle()
        assert w.engine.coarse_phase(U) == "requirements_met_pending"
        w.cycle(w.target())
        assert w.engine.coarse_phase(U) == "requirements_not_met"


class TestPlanning:
    def test_no_token_no_plan(self, w) -> None:
        w.cycle()
        assert w.plan_statuses() == []

    def test_no_plan_unless_the_barrier_is_confirmed_up(self, tmp_path) -> None:
        for i, kw in enumerate((dict(frozen=False), dict(status=ChasterProviderStatus.UNLOCKED))):
            (tmp_path / str(i)).mkdir()
            world = World(tmp_path / str(i), **kw)
            world.tokens()
            world.cycle()
            assert world.plan_statuses() == []

    def test_stale_observation_is_refreshed_and_if_it_stays_stale_nothing_happens(self, w) -> None:
        w.tokens()
        w.fetched_at = w.now - timedelta(hours=1)
        w.refresh_effective = False
        report = w.task.run(w.now)
        assert w.plan_statuses() == [] and w.refreshes == 1 and "plan_created" not in report.notes

    def test_planning_without_execution_never_spends(self, tmp_path) -> None:
        world = World(tmp_path, can_execute=False)
        world.tokens()
        world.cycle()
        assert world.plan_statuses() == ["planned"]
        world.cycle(world.target())
        world.cycle(world.now + timedelta(hours=1))
        assert world.engine.get_balance(U) == 1 and world.transport.calls == [] and world.auth_states() == []

    def test_halt_blocks_the_commit(self, w) -> None:
        w.tokens()
        w.cycle()
        w.control.set_halted(True, reason="stop", now=w.now)
        w.cycle(w.target())
        assert w.engine.get_balance(U) == 1 and w.transport.calls == [] and w.auth_states() == []
        w.control.set_halted(False, reason="go", now=w.now)
        w.cycle(w.now + timedelta(minutes=2))
        assert w.transport.calls == [("unfreeze", "lock-1")]


class TestVetoes:
    @pytest.mark.parametrize("flag", ["penalty", "supervised", "missed"])
    def test_a_veto_cancels_the_plan_and_blocks_a_new_one(self, w, flag) -> None:
        w.tokens()
        w.cycle()
        assert w.plan_statuses() == ["planned"]
        setattr(w, flag, True)
        report = w.cycle(w.now + timedelta(minutes=5))
        assert w.plan_statuses() == ["cancelled"] and any(n.startswith("plan_cancelled") for n in report.notes)
        w.cycle(w.now + timedelta(minutes=10))
        assert w.plan_statuses() == ["cancelled"] and w.engine.get_balance(U) == 1 and not w.transport.calls

    def test_the_plan_returns_when_the_veto_lifts(self, w) -> None:
        w.tokens()
        w.penalty = True
        w.cycle()
        assert w.plan_statuses() == []
        w.penalty = False
        w.cycle(w.now + timedelta(minutes=5))
        assert w.plan_statuses() == ["planned"]


class TestGatesAtTheCommit:
    def test_unconfirmed_lock_state_at_target_keeps_the_plan_and_the_token(self, w) -> None:
        w.tokens()
        w.cycle()
        w.frozen = False
        report = w.cycle(w.target())
        assert "commit:gate_not_met" in report.notes
        assert w.plan_statuses() == ["planned"] and w.engine.get_balance(U) == 1 and not w.transport.calls

    def test_due_inside_quiet_or_work_hours_retargets(self, w) -> None:
        w.tokens()
        w.cycle()
        next_morning = local(date(2026, 10, 13), 10, 0)           # Tuesday, work hours
        report = w.cycle(next_morning)
        assert "commit:retargeted" in report.notes and not w.transport.calls and w.engine.get_balance(U) == 1
        assert w.target() > next_morning


class TestProviderOutcomes:
    def test_executor_block_restores_the_token_and_allows_a_new_plan(self, tmp_path) -> None:
        world = World(tmp_path, scopes=())
        world.tokens()
        world.cycle()
        report = world.cycle(world.target())
        assert "release_failed:blocked" in report.notes
        assert world.engine.get_balance(U) == 1 and world.auth_states() == ["provider_failed"]
        world.cycle(world.now + timedelta(minutes=5))
        assert world.plan_statuses()[-1] == "planned"

    def test_window_check_inside_the_executor_blocks_and_refunds(self, tmp_path) -> None:
        world = World(tmp_path, window_ok=False)
        world.tokens()
        world.cycle()
        world.cycle(world.target())
        assert world.transport.calls == [] and world.engine.get_balance(U) == 1
        assert world.decisions()[0]["status"] == "blocked"

    def test_rejected_by_chaster_is_a_refunded_failure(self, tmp_path) -> None:
        world = World(tmp_path, transport=FakeTransport(result=TransportResult(outcome=O.FORBIDDEN, http_status=403)))
        world.tokens()
        world.cycle()
        world.cycle(world.target())
        assert world.auth_states() == ["provider_failed"] and world.engine.get_balance(U) == 1

    def test_ambiguous_result_keeps_the_token_spent_and_blocks_further_plans(self, tmp_path) -> None:
        world = World(tmp_path, transport=FakeTransport(raises=RuntimeError("boom")))
        world.tokens()
        world.cycle()
        report = world.cycle(world.target())
        assert "release_needs_review" in report.notes
        assert world.auth_states() == ["physical_state_unknown"] and world.engine.get_balance(U) == 0
        world.tokens()
        report = world.cycle(world.now + timedelta(hours=1))
        assert "release_in_flight" in report.notes and len(world.transport.calls) == 1

    def test_a_decision_that_will_never_execute_is_refunded(self, w) -> None:
        w.tokens()
        w.cycle()
        target = w.target()
        w.at(target)
        w.task.run(w.now)                                     # authorised + decision recorded, executor not run
        assert w.auth_states() == ["authorized"] and w.engine.get_balance(U) == 0
        w.control.set_halted(True, reason="stop", now=w.now)  # the executor will never pick it up
        w.at(target + timedelta(minutes=17))
        w.task.sync(w.now)
        assert w.auth_states() == ["provider_failed"] and w.engine.get_balance(U) == 1


class TestBackstop:
    def test_releases_after_the_maximum_hold_without_a_token(self, tmp_path) -> None:
        world = World(tmp_path, frozen_since=timedelta(hours=73))
        world.at(local(MON, 18, 0))
        report = world.cycle()
        assert "backstop:authorized" in report.notes and world.transport.calls == [("unfreeze", "lock-1")]
        assert world.engine.get_balance(U) == 0
        with world.core.transaction() as tx:
            assert tx.fetch_one("SELECT COUNT(*) AS n FROM token_ledger")["n"] == 0
            assert tx.fetch_one("SELECT kind FROM release_authorizations")["kind"] == "backstop"

    def test_not_before_the_maximum_hold(self, tmp_path) -> None:
        world = World(tmp_path, frozen_since=timedelta(hours=71))
        world.at(local(MON, 18, 0))
        world.cycle()
        assert world.transport.calls == [] and world.auth_states() == []

    def test_still_respects_the_window(self, tmp_path) -> None:
        world = World(tmp_path, frozen_since=timedelta(days=5))
        report = world.cycle(local(MON, 10, 0))               # work hours
        assert "backstop:window_closed" in report.notes and world.transport.calls == []

    def test_ignores_vetoes(self, tmp_path) -> None:
        world = World(tmp_path, frozen_since=timedelta(days=5), penalty=True, supervised=True, missed=True)
        world.cycle(local(MON, 18, 0))
        assert world.transport.calls == [("unfreeze", "lock-1")]

    def test_unknown_frozen_since_never_fires(self, tmp_path) -> None:
        world = World(tmp_path, frozen_since=timedelta(days=5))
        world._obs = lambda: ChasterProviderObservation(
            id="o", connection_id=U, provider="chaster", chaster_lock_id="lock-1", status=ChasterProviderStatus.LOCKED,
            fetched_at=world.now - timedelta(minutes=1), created_at=world.now, is_frozen=True, has_keyholder=True)
        world.task._get_obs = world._obs
        world.cycle(local(MON, 18, 0))
        assert world.transport.calls == []

    def test_planning_only_mode_never_runs_the_backstop(self, tmp_path) -> None:
        world = World(tmp_path, can_execute=False, frozen_since=timedelta(days=5))
        world.cycle(local(MON, 18, 0))
        assert world.auth_states() == []
