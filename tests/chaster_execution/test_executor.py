"""
Phase 3 executor tests. Fake transport only: no network, no real Chaster call,
no token anywhere.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from chaster.models import ChasterProviderObservation, ChasterProviderStatus
from chaster_execution.control import ExecutionControl
from chaster_execution.executor import ChasterExecutor
from chaster_execution.models import ExecStatus as S
from chaster_execution.models import TransportOutcome as O
from chaster_execution.models import TransportResult
from infrastructure.database import Database as CoreDatabase

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).parent.parent.parent


from infrastructure.time_format import iso


class FakeTransport:
    def __init__(self, result=None, raises=None, on_call=None) -> None:
        self.calls: list[tuple[str, str, int]] = []
        self.result = result or TransportResult(outcome=O.OK, http_status=204)
        self.raises = raises
        self.on_call = on_call

    def _do(self, kind, lock_id, seconds):
        self.calls.append((kind, lock_id, seconds))
        if self.on_call:
            self.on_call()
        if self.raises:
            raise self.raises
        return self.result

    def add_time(self, *, lock_id, seconds):
        return self._do("add", lock_id, seconds)

    def remove_time(self, *, lock_id, seconds):
        return self._do("remove", lock_id, seconds)

    def set_frozen(self, *, lock_id, frozen):
        return self._do("freeze" if frozen else "unfreeze", lock_id, 0)


@pytest.fixture
def core(tmp_path: Path) -> CoreDatabase:
    c = CoreDatabase(tmp_path / "test.db")
    with c.raw_connection() as conn:
        for path in sorted((ROOT / "database" / "migrations").glob("*.sql")):
            conn.executescript(path.read_text(encoding="utf-8"))
    return c


def obs(**kw) -> ChasterProviderObservation:
    base = dict(id="o1", connection_id="w", provider="chaster", chaster_lock_id="lock-1",
                status=ChasterProviderStatus.LOCKED, fetched_at=NOW - timedelta(minutes=1), created_at=NOW,
                is_frozen=False, has_keyholder=True, end_date=NOW + timedelta(days=2))
    base.update(kw)
    return ChasterProviderObservation(**base)


class World:
    def __init__(self, core, transport, *, enabled=True, halted=False, observation="default", scopes=("locks",),
                 refresh=None, max_per_run=5, allow_barrier=False, window_check=None):
        self.core = core
        self.transport = transport
        self.control = ExecutionControl(core.db_path, core=core)
        self.control.set_halted(halted, reason="test", now=NOW)
        self.observation = obs() if observation == "default" else observation
        self.refreshed = 0
        self.refresh_fn = refresh
        self.scopes = scopes

        def _refresh(now):
            self.refreshed += 1
            if self.refresh_fn:
                self.refresh_fn(self, now)

        self.executor = ChasterExecutor(
            core.db_path, core=core, enabled=enabled, control=self.control, transport=transport,
            get_latest_observation=lambda: self.observation, refresh_observation=_refresh,
            get_granted_scopes=lambda: self.scopes, max_per_run=max_per_run,
            allow_barrier_actions=allow_barrier, window_check=window_check,
        )

    def decision(self, *, action="add_time", hours=1, status="dry_run", age_minutes=1, now=NOW, rule_id="r") -> str:
        did, tid = str(uuid.uuid4()), str(uuid.uuid4())
        created = iso(now - timedelta(minutes=age_minutes))
        with self.core.transaction() as tx:
            tx.execute("INSERT INTO lock_decision_triggers (id, trust_domain, assessment_id, direction, created_at) "
                       "VALUES (?, 'd', 'a', 'setback', ?)", (tid, created))
            tx.execute(
                "INSERT INTO lock_decisions (id, trigger_id, trust_domain, created_at, action_type, hours, "
                "entitlement_class, rule_id, ruleset_version, reason, inputs_json, status) "
                "VALUES (?, ?, 'd', ?, ?, ?, 'guaranteed', ?, '1', 'because', '{}', ?)",
                (did, tid, created, action, hours, rule_id, status),
            )
        return did

    def status(self, did) -> str:
        with self.core.transaction() as tx:
            return tx.fetch_one("SELECT status FROM lock_decisions WHERE id = ?", (did,))["status"]

    def audit(self, did) -> list[tuple[str, str]]:
        with self.core.transaction() as tx:
            rows = tx.fetch_all("SELECT from_status, to_status FROM lock_decision_executions WHERE decision_id = ? "
                                "ORDER BY rowid", (did,))
        return [(r["from_status"], r["to_status"]) for r in rows]

    def detail(self, did) -> str:
        with self.core.transaction() as tx:
            return tx.fetch_all("SELECT detail FROM lock_decision_executions WHERE decision_id = ? "
                                "ORDER BY rowid DESC", (did,))[0]["detail"]

    def run(self, now=NOW):
        return self.executor.run_once(now=now)


class TestSwitches:
    def test_disabled_does_nothing(self, core) -> None:
        w = World(core, FakeTransport(), enabled=False)
        d = w.decision()
        assert w.run() == [] and w.status(d) == "dry_run" and not w.transport.calls

    def test_seed_row_is_halted_so_nothing_executes_by_default(self, core) -> None:
        w = World(core, FakeTransport())
        w.control.set_halted(True, reason="x", now=NOW)
        with core.transaction() as tx:
            tx.execute("DELETE FROM execution_control")
        d = w.decision()
        assert w.control.is_halted() is True            # missing row == halted
        assert w.run() == [] and w.status(d) == "dry_run" and not w.transport.calls

    def test_freshly_migrated_database_is_halted(self, core) -> None:
        assert ExecutionControl(core.db_path, core=core).is_halted() is True

    def test_halted_does_nothing(self, core) -> None:
        w = World(core, FakeTransport(), halted=True)
        d = w.decision()
        assert w.run() == [] and w.status(d) == "dry_run" and not w.transport.calls

    def test_halt_mid_run_stops_before_the_next_call(self, core) -> None:
        w = World(core, FakeTransport())
        w.transport.on_call = lambda: w.control.set_halted(True, reason="stop", now=NOW)
        a = w.decision(age_minutes=3)
        b = w.decision(age_minutes=2, hours=1)
        w.run()
        assert len(w.transport.calls) == 1
        assert w.status(a) == "executed" and w.status(b) == "pending"


class TestHappyPath:
    def test_add_time_executes_once_with_seconds(self, core) -> None:
        w = World(core, FakeTransport())
        d = w.decision(hours=2)
        w.run()
        assert w.transport.calls == [("add", "lock-1", 7200)]
        assert w.status(d) == "executed"
        assert w.audit(d) == [("dry_run", "pending"), ("pending", "executing"), ("executing", "executed")]

    def test_remove_time(self, core) -> None:
        w = World(core, FakeTransport())
        d = w.decision(action="remove_time", hours=1)
        w.run()
        assert w.transport.calls == [("remove", "lock-1", 3600)] and w.status(d) == "executed"

    def test_second_run_never_executes_again(self, core) -> None:
        w = World(core, FakeTransport())
        w.decision()
        w.run()
        w.run(NOW + timedelta(seconds=60))
        assert len(w.transport.calls) == 1

    def test_http_status_and_end_date_before_are_audited(self, core) -> None:
        w = World(core, FakeTransport())
        d = w.decision()
        w.run()
        with core.transaction() as tx:
            rows = tx.fetch_all("SELECT to_status, http_status, end_date_before FROM lock_decision_executions "
                                "WHERE decision_id = ?", (d,))
        by = {r["to_status"]: r for r in rows}
        assert by["executed"]["http_status"] == 204 and by["executing"]["end_date_before"] is not None

    def test_status_change_events_are_emitted(self, core) -> None:
        w = World(core, FakeTransport())
        w.decision()
        w.run()
        with core.transaction() as tx:
            n = tx.fetch_one("SELECT COUNT(*) AS n FROM domain_events WHERE event_type = 'lock_decision.status_changed'")["n"]
        assert n == 3


class TestOnlyTimeActionsExecute:
    @pytest.mark.parametrize("action", ["no_action", "freeze", "unfreeze"])
    def test_other_actions_are_never_touched(self, core, action) -> None:
        w = World(core, FakeTransport())
        d = w.decision(action=action, hours=0)
        assert w.run() == [] and w.status(d) == "dry_run" and not w.transport.calls


class TestStaleness:
    def test_old_dry_run_history_is_left_alone(self, core) -> None:
        w = World(core, FakeTransport())
        d = w.decision(age_minutes=16)
        assert w.run() == [] and w.status(d) == "dry_run" and not w.transport.calls

    def test_decision_exactly_15_minutes_old_is_still_executable(self, core) -> None:
        w = World(core, FakeTransport())
        d = w.decision(age_minutes=15)
        w.run()
        assert w.status(d) == "executed"

    def test_stale_pending_expires_without_a_call(self, core) -> None:
        w = World(core, FakeTransport())
        d = w.decision(status="pending", age_minutes=16)
        w.run()
        assert w.status(d) == "expired" and not w.transport.calls


class TestTransportOutcomes:
    @pytest.mark.parametrize("outcome,http,expected", [
        (O.FORBIDDEN, 403, "failed"), (O.NOT_ELIGIBLE, 400, "failed"), (O.NOT_FOUND, 404, "failed"),
        (O.UNAUTHORIZED, 401, "failed"), (O.NOT_SENT, None, "failed"),
        (O.UNKNOWN, 500, "needs_review"), (O.UNKNOWN, None, "needs_review"),
    ])
    def test_mapping(self, core, outcome, http, expected) -> None:
        w = World(core, FakeTransport(result=TransportResult(outcome=outcome, http_status=http)))
        d = w.decision()
        w.run()
        assert w.status(d) == expected and len(w.transport.calls) == 1

    def test_transport_exception_is_ambiguous(self, core) -> None:
        w = World(core, FakeTransport(raises=RuntimeError("boom")))
        d = w.decision()
        w.run()
        assert w.status(d) == "needs_review"

    def test_needs_review_and_failed_are_never_retried(self, core) -> None:
        w = World(core, FakeTransport(result=TransportResult(outcome=O.UNKNOWN, http_status=500)))
        d = w.decision()
        w.run()
        w.run(NOW + timedelta(seconds=60))
        w.run(NOW + timedelta(seconds=120))
        assert len(w.transport.calls) == 1 and w.status(d) == "needs_review"


class TestCrashRecovery:
    def test_orphaned_executing_becomes_needs_review_and_is_not_resent(self, core) -> None:
        w = World(core, FakeTransport())
        d = w.decision(status="executing")
        res = w.run()
        assert (d, S.NEEDS_REVIEW) in res and w.status(d) == "needs_review" and not w.transport.calls

    def test_claim_is_unique_per_decision(self, core) -> None:
        w = World(core, FakeTransport())
        d = w.decision(status="pending")
        ex = w.executor
        assert ex._transition(d, S.PENDING, S.EXECUTING, NOW, "claim") is True
        assert ex._transition(d, S.PENDING, S.EXECUTING, NOW, "claim again") is False
        assert w.audit(d) == [("pending", "executing")]

    def test_illegal_transition_is_refused(self, core) -> None:
        w = World(core, FakeTransport())
        d = w.decision(status="executed")
        assert w.executor._transition(d, S.PENDING, S.EXECUTING, NOW, "x") is False
        assert w.status(d) == "executed"


class TestPreflight:
    @pytest.mark.parametrize("kw,text", [
        (dict(scopes=None), "scope"),
        (dict(scopes=()), "scope"),
        (dict(scopes=("profile",)), "scope"),
        (dict(observation=None), "no lock observation"),
        (dict(observation=obs(status=ChasterProviderStatus.UNLOCKED)), "not in a locked state"),
        (dict(observation=obs(status=ChasterProviderStatus.DESERTED)), "not in a locked state"),
        (dict(observation=obs(has_keyholder=False)), "keyholder"),
        (dict(observation=obs(has_keyholder=None)), "keyholder"),
        (dict(observation=obs(is_frozen=True)), "frozen"),
        (dict(observation=obs(is_frozen=None)), "frozen"),
        (dict(observation=obs(chaster_lock_id=None)), "no lock id"),
        (dict(observation=obs(fetched_at=NOW + timedelta(minutes=5))), "stale"),
    ])
    def test_blocked_without_a_call(self, core, kw, text) -> None:
        w = World(core, FakeTransport(), **kw)
        d = w.decision()
        w.run()
        assert w.status(d) == "blocked" and text in w.detail(d) and not w.transport.calls

    def test_stale_observation_is_refreshed_then_used(self, core) -> None:
        def refresh(world, now):
            world.observation = obs(fetched_at=now)
        w = World(core, FakeTransport(), observation=obs(fetched_at=NOW - timedelta(minutes=20)), refresh=refresh)
        d = w.decision()
        w.run()
        assert w.refreshed == 1 and w.status(d) == "executed"

    def test_stale_observation_and_failed_refresh_blocks(self, core) -> None:
        def refresh(world, now):
            raise RuntimeError("network down")
        w = World(core, FakeTransport(), observation=obs(fetched_at=NOW - timedelta(minutes=20)), refresh=refresh)
        d = w.decision()
        w.run()
        assert w.status(d) == "blocked" and "stale" in w.detail(d) and not w.transport.calls

    def test_fresh_observation_is_not_refreshed(self, core) -> None:
        w = World(core, FakeTransport())
        w.decision()
        w.run()
        assert w.refreshed == 0


class TestLimitsOnExecutedHistory:
    def test_cooldown_after_a_real_execution_blocks_the_next(self, core) -> None:
        w = World(core, FakeTransport())
        a = w.decision(age_minutes=3)
        b = w.decision(age_minutes=2)
        w.run()
        assert w.status(a) == "executed" and w.status(b) == "blocked" and "cooldown" in w.detail(b)
        assert len(w.transport.calls) == 1

    def test_daily_add_cap_uses_executed_history(self, core) -> None:
        w = World(core, FakeTransport())
        old = w.decision(status="executed", hours=6, age_minutes=300)
        old2 = w.decision(status="executed", hours=6, age_minutes=400)
        for did, mins in ((old, 300), (old2, 400)):
            with core.transaction() as tx:
                tx.execute("INSERT INTO lock_decision_executions (id, decision_id, from_status, to_status, created_at, detail) "
                           "VALUES (?, ?, 'pending', 'executing', ?, 'x')",
                           (str(uuid.uuid4()), did, iso(NOW - timedelta(minutes=mins))))
        d = w.decision(hours=1)
        w.run()
        assert w.status(d) == "blocked" and not w.transport.calls

    def test_dry_run_history_does_not_consume_the_live_budget(self, core) -> None:
        w = World(core, FakeTransport())
        for i in range(3):
            w.decision(status="dry_run", hours=6, age_minutes=100 + i * 60)   # all too old to be picked up
        d = w.decision(hours=1)
        w.run()
        assert w.status(d) == "executed"

    def test_failed_and_blocked_do_not_consume_the_budget(self, core) -> None:
        w = World(core, FakeTransport())
        w.decision(status="failed", age_minutes=30)
        w.decision(status="blocked", age_minutes=30)
        d = w.decision(hours=1)
        w.run()
        assert w.status(d) == "executed"


class TestRunBound:
    def test_max_per_run(self, core) -> None:
        w = World(core, FakeTransport(), max_per_run=1)
        for i in range(3):
            w.decision(age_minutes=5 - i)
        w.run()
        assert len(w.transport.calls) == 1


class TestSecrets:
    def test_audit_rows_contain_no_secrets(self, core) -> None:
        w = World(core, FakeTransport())
        d = w.decision()
        w.run()
        with core.transaction() as tx:
            rows = tx.fetch_all("SELECT * FROM lock_decision_executions WHERE decision_id = ?", (d,))
        blob = json.dumps([dict(r) for r in rows]).lower()
        for word in ("token", "bearer", "authorization", "secret"):
            assert word not in blob
