"""Phase 3 wiring: hydration, halt control, composition flags, periodic cycle, and guards."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from cryptography.fernet import Fernet

from chaster.models import ChasterProviderObservation, ChasterProviderStatus
from chaster.provider_observation_repository import ChasterProviderObservationRecorder, ChasterProviderObservations
from chaster.repository import ChasterConnectionRepository
from chaster.token_encryptor import TokenEncryptor
from chaster.token_manager import ChasterTokenManager
from chaster_execution.control import ExecutionControl
from chaster_execution.hydration import snapshot_from_observation
from chaster_execution.models import TransportOutcome, TransportResult
from goal_management.models import GoalInterventionType, GoalOutcome
from goal_management.repository import GoalManager
from infrastructure.clock import FrozenClock
from infrastructure.database import Database as CoreDatabase
from system.composition import build_rules_and_execution
from system.periodic import run_periodic_cycle
from trust_manager.repository import TrustManager

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).parent.parent.parent


class FakeTransport:
    def __init__(self) -> None:
        self.calls = []

    def add_time(self, *, lock_id, seconds):
        self.calls.append(("add", lock_id, seconds))
        return TransportResult(outcome=TransportOutcome.OK, http_status=204)

    def remove_time(self, *, lock_id, seconds):
        self.calls.append(("remove", lock_id, seconds))
        return TransportResult(outcome=TransportOutcome.OK, http_status=204)


@pytest.fixture
def core(tmp_path: Path) -> CoreDatabase:
    c = CoreDatabase(tmp_path / "test.db")
    with c.raw_connection() as conn:
        for path in sorted((ROOT / "database" / "migrations").glob("*.sql")):
            conn.executescript(path.read_text(encoding="utf-8"))
        for uid in ("w", "k"):
            conn.execute("INSERT INTO user_accounts (id, created_at, last_seen_at) VALUES (?, ?, ?)",
                         (uid, NOW.isoformat(), NOW.isoformat()))
        conn.commit()
    return c


class TestHydration:
    def test_none(self) -> None:
        assert snapshot_from_observation(None) is None

    def test_maps_persisted_fields_and_never_reads_can_be_unlocked(self) -> None:
        o = ChasterProviderObservation(
            id="1", connection_id="w", provider="chaster", chaster_lock_id="l", status=ChasterProviderStatus.LOCKED,
            fetched_at=NOW, created_at=NOW, is_frozen=False, has_keyholder=True,
        )
        s = snapshot_from_observation(o)
        assert (s.observed_at, s.status, s.is_frozen, s.is_keyholder_lock, s.can_be_unlocked) == (
            NOW, "locked", False, True, None)

    def test_unknown_fields_stay_unknown(self) -> None:
        o = ChasterProviderObservation(
            id="1", connection_id="w", provider="chaster", chaster_lock_id="l", status=ChasterProviderStatus.LOCKED,
            fetched_at=NOW, created_at=NOW,
        )
        s = snapshot_from_observation(o)
        assert s.is_frozen is None and s.is_keyholder_lock is None


class TestObservationColumns:
    def test_service_extracts_fields_only_when_well_typed(self, core) -> None:
        from chaster.provider_observation_service import ChasterProviderObservationService

        encryptor = TokenEncryptor(Fernet.generate_key())
        conns = ChasterConnectionRepository(core.db_path, core=core, encryptor=encryptor)
        conns.create_or_replace(
            user_id="w", chaster_account_id="a", chaster_username="n", access_token="x", refresh_token="y",
            access_token_expires_at=NOW + timedelta(hours=1), refresh_token_expires_at=NOW + timedelta(days=30),
            granted_scopes=("locks",), now=NOW,
        )
        lock_client = MagicMock()
        svc = ChasterProviderObservationService(
            connections=conns, oauth_client=MagicMock(), lock_client=lock_client,
            observations=ChasterProviderObservations(core.db_path, core=core),
            recorder=ChasterProviderObservationRecorder(core.db_path, core=core),
        )
        lock_client.list_active_locks.return_value = [{
            "_id": "l1", "status": "locked", "isFrozen": False, "canBeUnlocked": True,
            "endDate": "2026-10-15T14:30:00.000Z", "keyholder": {"_id": "k", "username": "kh"},
        }]
        full = svc.fetch_and_record(connection_id="w", now=NOW).observation
        assert full.is_frozen is False and full.has_keyholder is True
        assert full.end_date == datetime(2026, 10, 15, 14, 30, tzinfo=timezone.utc)

        lock_client.list_active_locks.return_value = [{"_id": "l1", "status": "locked", "isFrozen": "no",
                                                       "keyholder": None, "endDate": "garbage"}]
        odd = svc.fetch_and_record(connection_id="w", now=NOW + timedelta(minutes=1)).observation
        assert odd.is_frozen is None and odd.has_keyholder is False and odd.end_date is None

        lock_client.list_active_locks.return_value = [{"_id": "l1", "status": "locked"}]
        bare = svc.fetch_and_record(connection_id="w", now=NOW + timedelta(minutes=2)).observation
        assert bare.is_frozen is None and bare.has_keyholder is None


class TestControl:
    def test_set_and_clear(self, core) -> None:
        c = ExecutionControl(core.db_path, core=core)
        assert c.is_halted() is True
        c.set_halted(False, reason="ok", now=NOW)
        assert c.is_halted() is False and c.reason() == "ok"
        c.set_halted(True, reason="stop", now=NOW)
        assert c.is_halted() is True

    def test_reason_is_required(self, core) -> None:
        with pytest.raises(ValueError):
            ExecutionControl(core.db_path, core=core).set_halted(False, reason=" ", now=NOW)


def _config(core, **kw):
    base = dict(db_path=core.db_path, enable_rules_engine=True, enable_chaster_execution=False,
                chaster_execution_wearer_user_id="w", chaster_execution_keyholder_user_id="k")
    base.update(kw)
    return SimpleNamespace(**base)


def _chaster_parts(core):
    conns = ChasterConnectionRepository(core.db_path, core=core, encryptor=TokenEncryptor(Fernet.generate_key()))
    conns.create_or_replace(
        user_id="k", chaster_account_id="ka", chaster_username="kh", access_token="x", refresh_token="y",
        access_token_expires_at=NOW + timedelta(hours=3), refresh_token_expires_at=NOW + timedelta(days=30),
        granted_scopes=("locks",), now=NOW,
    )
    conns.create_or_replace(
        user_id="w", chaster_account_id="wa", chaster_username="w", access_token="x", refresh_token="y",
        access_token_expires_at=NOW + timedelta(hours=3), refresh_token_expires_at=NOW + timedelta(days=30),
        granted_scopes=("locks",), now=NOW,
    )
    oauth = MagicMock()
    return dict(connections=conns, oauth_client=oauth, lock_client=MagicMock(),
                token_manager=ChasterTokenManager(connections=conns, oauth_client=oauth))


class TestComposition:
    def test_everything_off_by_default(self, core) -> None:
        w = build_rules_and_execution(_config(core, enable_rules_engine=False), core)
        assert w.rules_engine is None and w.executor is None

    def test_rules_engine_only_is_dry_run(self, core) -> None:
        w = build_rules_and_execution(_config(core), core, **_chaster_parts(core))
        assert w.rules_engine is not None and w.executor is None

    def test_execution_builds_the_real_http_transport_by_default(self, core) -> None:
        from chaster_execution.transport import ChasterHttpTransport
        w = build_rules_and_execution(_config(core, enable_chaster_execution=True), core, **_chaster_parts(core))
        assert w.executor is not None and isinstance(w.executor._transport, ChasterHttpTransport)

    def test_execution_flag_off_builds_no_transport_and_no_executor(self, core) -> None:
        w = build_rules_and_execution(_config(core), core, **_chaster_parts(core))
        assert w.executor is None

    def test_execution_needs_both_identities(self, core) -> None:
        w = build_rules_and_execution(
            _config(core, enable_chaster_execution=True, chaster_execution_keyholder_user_id=None), core,
            transport=FakeTransport(), **_chaster_parts(core))
        assert w.executor is None and any("NOT active" in n for n in w.notes)

    def test_execution_active_with_a_transport(self, core) -> None:
        w = build_rules_and_execution(_config(core, enable_chaster_execution=True), core,
                                      transport=FakeTransport(), **_chaster_parts(core))
        assert w.executor is not None


def _world_with_missed_goal(core):
    tm = TrustManager(core.db_path, core=core)
    tm.create_domain(domain_id="fitness", display_name="F", description="d", created_via_consent_id="c1", now=NOW)
    gm = GoalManager(core.db_path, core=core)
    goal = gm.create_goal("t", "d", "fitness", "user_proposed", now=NOW)
    ev = gm.record_evidence(goal.goal_group_id, goal.current_version_id, NOW, NOW + timedelta(days=7),
                            GoalOutcome.MISSED, "p", "check_in", now=NOW)
    evaluation = gm.record_evaluation(goal.goal_group_id, (ev.id,), "f", GoalInterventionType.NO_CHANGE, "x", now=NOW)
    gm.record_accountability_assessment(evaluation.id, relevant_to_trust=True, rationale="r", now=NOW)


def _record_observation(core, **kw):
    args = dict(connection_id="w", provider="chaster", chaster_lock_id="lock-9", status=ChasterProviderStatus.LOCKED,
                fetched_at=NOW, now=NOW, is_frozen=False, has_keyholder=True)
    args.update(kw)
    return ChasterProviderObservationRecorder(core.db_path, core=core).record(**args)


def _statuses(core):
    with core.transaction() as tx:
        return [r["status"] for r in tx.fetch_all("SELECT status FROM lock_decisions ORDER BY rowid")]


class TestPeriodicCycleEndToEnd:
    def test_missed_goal_to_executed_decision_through_the_whole_pipeline(self, core) -> None:
        transport = FakeTransport()
        parts = _chaster_parts(core)
        wiring = build_rules_and_execution(_config(core, enable_chaster_execution=True), core,
                                           transport=transport, **parts)
        ExecutionControl(core.db_path, core=core).set_halted(False, reason="test", now=NOW)
        _world_with_missed_goal(core)
        _record_observation(core)
        clock = FrozenClock(NOW + timedelta(minutes=1))

        assert run_periodic_cycle(core, "p1", clock, rules_engine=wiring.rules_engine, executor=wiring.executor)
        assert transport.calls == [("add", "lock-9", 3600)]
        assert _statuses(core) == ["executed"]

        run_periodic_cycle(core, "p2", FrozenClock(NOW + timedelta(minutes=2)),
                           rules_engine=wiring.rules_engine, executor=wiring.executor)
        assert len(transport.calls) == 1

    def test_halted_by_default_so_nothing_executes_but_the_decision_is_recorded(self, core) -> None:
        transport = FakeTransport()
        wiring = build_rules_and_execution(_config(core, enable_chaster_execution=True), core,
                                           transport=transport, **_chaster_parts(core))
        _world_with_missed_goal(core)
        _record_observation(core)
        run_periodic_cycle(core, "p1", FrozenClock(NOW + timedelta(minutes=1)),
                           rules_engine=wiring.rules_engine, executor=wiring.executor)
        assert transport.calls == [] and _statuses(core) == ["dry_run"]

    def test_execution_flag_off_stays_dry_run_even_when_not_halted(self, core) -> None:
        transport = FakeTransport()
        wiring = build_rules_and_execution(_config(core), core, transport=transport, **_chaster_parts(core))
        ExecutionControl(core.db_path, core=core).set_halted(False, reason="test", now=NOW)
        _world_with_missed_goal(core)
        _record_observation(core)
        run_periodic_cycle(core, "p1", FrozenClock(NOW + timedelta(minutes=1)),
                           rules_engine=wiring.rules_engine, executor=wiring.executor)
        assert transport.calls == [] and _statuses(core) == ["dry_run"]

    def test_frozen_lock_blocks(self, core) -> None:
        transport = FakeTransport()
        wiring = build_rules_and_execution(_config(core, enable_chaster_execution=True), core,
                                           transport=transport, **_chaster_parts(core))
        ExecutionControl(core.db_path, core=core).set_halted(False, reason="test", now=NOW)
        _world_with_missed_goal(core)
        _record_observation(core, is_frozen=True)
        run_periodic_cycle(core, "p1", FrozenClock(NOW + timedelta(minutes=1)),
                           rules_engine=wiring.rules_engine, executor=wiring.executor)
        assert transport.calls == [] and _statuses(core) == ["dry_run"]    # engine itself said NO_ACTION

    def test_another_instance_holding_the_lease_skips_the_cycle(self, core) -> None:
        from infrastructure.startup_lease import acquire_system_startup_lease
        assert acquire_system_startup_lease(core, "other", NOW + timedelta(minutes=1), timedelta(minutes=2))
        ran = run_periodic_cycle(core, "me", FrozenClock(NOW + timedelta(minutes=1)), rules_engine=None, executor=None)
        assert ran is False


class TestIsolationGuards:
    def _imports(self, path: Path) -> list[str]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.append(node.module)
        return names

    def test_execution_plane_cannot_reach_a_model_the_bot_or_the_emergency_plane(self) -> None:
        forbidden = ("discord", "bot", "conversation_engine", "application", "memory_system", "ai")
        for path in (ROOT / "chaster_execution").glob("*.py"):
            for name in self._imports(path):
                assert name.split(".")[0] not in forbidden, (path.name, name)
                assert "emergency" not in name, (path.name, name)

    def test_only_wiring_imports_the_execution_plane(self) -> None:
        for path in ROOT.rglob("*.py"):
            rel = path.relative_to(ROOT)
            if rel.parts[0] in {"chaster_execution", "system", "scripts", "cli", "tests", ".venv", "venv", "env"} \
                    or rel.parts[0].startswith(".") or "__pycache__" in rel.parts:
                continue
            for name in self._imports(path):
                assert name.split(".")[0] != "chaster_execution", f"{rel} imports chaster_execution"

    def test_transport_interface_has_only_time_and_barrier_methods(self) -> None:
        from chaster_execution.models import LockActionTransport
        methods = {m for m in vars(LockActionTransport) if not m.startswith("_")}
        assert methods == {"add_time", "remove_time", "set_frozen"}

    def test_no_tls_weakening_anywhere_in_the_new_code(self) -> None:
        for pkg in ("chaster_execution", "rules_engine", "system"):
            for path in (ROOT / pkg).glob("*.py"):
                assert "verify=False" not in path.read_text(encoding="utf-8")

    def test_application_service_never_mentions_emergency(self) -> None:
        assert "emergency" not in (ROOT / "application" / "service.py").read_text(encoding="utf-8").lower()
