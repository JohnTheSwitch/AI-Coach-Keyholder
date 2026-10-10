"""
Barrier management in the executor (v0.39): freeze/unfreeze execute ONLY for barrier decisions, with their own
limit pool (2 per 24 h, 4 h cooldown), and an unfreeze needs the barrier up and a discretionary window.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from chaster.models import ChasterProviderStatus
from chaster_execution.models import ExecStatus as S
from chaster_execution.models import TransportOutcome as O
from chaster_execution.models import TransportResult
from infrastructure.time_format import iso
from rules_engine.limits import apply_limits
from rules_engine.models import ActionType as A
from rules_engine.models import RecentAction
from tests.chaster_execution.test_executor import NOW, FakeTransport, World, core, obs  # noqa: F401

RULE = "barrier.release"
FROZEN = dict(is_frozen=True)


def bw(core, transport=None, **kw):
    kw.setdefault("observation", obs(**FROZEN))
    kw.setdefault("allow_barrier", True)
    return World(core, transport or FakeTransport(), **kw)


class TestOnlyBarrierDecisionsMayFreezeOrUnfreeze:
    def test_unfreeze_executes_for_the_barrier_rule(self, core) -> None:
        w = bw(core)
        d = w.decision(action="unfreeze", hours=0, rule_id=RULE)
        w.run()
        assert w.transport.calls == [("unfreeze", "lock-1", 0)] and w.status(d) == "executed"
        assert w.audit(d) == [("dry_run", "pending"), ("pending", "executing"), ("executing", "executed")]

    def test_freeze_executes_when_the_barrier_is_down(self, core) -> None:
        w = bw(core, observation=obs(is_frozen=False))
        d = w.decision(action="freeze", hours=0, rule_id=RULE)
        w.run()
        assert w.transport.calls == [("freeze", "lock-1", 0)] and w.status(d) == "executed"

    @pytest.mark.parametrize("action", ["freeze", "unfreeze"])
    @pytest.mark.parametrize("rule_id", ["r", "setback", "custom.rule", None, "barrier"])
    def test_other_rules_never_execute_a_barrier_action(self, core, action, rule_id) -> None:
        w = bw(core)
        d = w.decision(action=action, hours=0, rule_id=rule_id)
        assert w.run() == [] and w.status(d) == "dry_run" and not w.transport.calls

    def test_disabled_without_the_barrier_permission(self, core) -> None:
        w = bw(core, allow_barrier=False)
        d = w.decision(action="unfreeze", hours=0, rule_id=RULE)
        assert w.run() == [] and w.status(d) == "dry_run" and not w.transport.calls

    def test_time_actions_still_work_alongside(self, core) -> None:
        w = bw(core, observation=obs(is_frozen=False))
        d = w.decision(action="add_time", hours=1)
        w.run()
        assert w.transport.calls == [("add", "lock-1", 3600)] and w.status(d) == "executed"


class TestBarrierPreflight:
    @pytest.mark.parametrize("action,observation,text", [
        ("unfreeze", obs(is_frozen=False), "expects"),                      # nothing to release
        ("unfreeze", obs(is_frozen=None), "expects"),
        ("freeze", obs(is_frozen=True), "expects"),                         # already frozen
        ("freeze", obs(is_frozen=None), "expects"),
        ("unfreeze", obs(is_frozen=True, status=ChasterProviderStatus.UNLOCKED), "locked"),
        ("unfreeze", obs(is_frozen=True, has_keyholder=False), "keyholder"),
        ("unfreeze", obs(is_frozen=True, has_keyholder=None), "keyholder"),
        ("unfreeze", None, "no lock observation"),
        ("unfreeze", obs(is_frozen=True, fetched_at=NOW - timedelta(minutes=20)), "stale"),
        ("unfreeze", obs(is_frozen=True, chaster_lock_id=None), "no lock id"),
    ])
    def test_blocked_without_a_call(self, core, action, observation, text) -> None:
        w = bw(core, observation=observation)
        d = w.decision(action=action, hours=0, rule_id=RULE)
        w.run()
        assert w.status(d) == "blocked" and text in w.detail(d) and not w.transport.calls

    def test_scope_is_required(self, core) -> None:
        w = bw(core, scopes=("profile",))
        d = w.decision(action="unfreeze", hours=0, rule_id=RULE)
        w.run()
        assert w.status(d) == "blocked" and not w.transport.calls

    def test_unfreeze_outside_a_discretionary_window_is_blocked(self, core) -> None:
        w = bw(core, window_check=lambda now: False)
        d = w.decision(action="unfreeze", hours=0, rule_id=RULE)
        w.run()
        assert w.status(d) == "blocked" and "window" in w.detail(d) and not w.transport.calls

    def test_the_window_check_receives_the_execution_time(self, core) -> None:
        seen = []
        w = bw(core, window_check=lambda now: seen.append(now) or True)
        w.decision(action="unfreeze", hours=0, rule_id=RULE)
        w.run()
        assert seen == [NOW]

    def test_the_window_check_does_not_gate_raising_the_barrier(self, core) -> None:
        w = bw(core, observation=obs(is_frozen=False), window_check=lambda now: False)
        d = w.decision(action="freeze", hours=0, rule_id=RULE)
        w.run()
        assert w.status(d) == "executed"

    def test_halted_stops_a_barrier_action_too(self, core) -> None:
        w = bw(core, halted=True)
        d = w.decision(action="unfreeze", hours=0, rule_id=RULE)
        assert w.run() == [] and w.status(d) == "dry_run" and not w.transport.calls


class TestBarrierOutcomes:
    @pytest.mark.parametrize("outcome,http,expected", [
        (O.FORBIDDEN, 403, "failed"), (O.NOT_ELIGIBLE, 400, "failed"), (O.NOT_FOUND, 404, "failed"),
        (O.UNKNOWN, 500, "needs_review"), (O.NOT_SENT, None, "failed"),
    ])
    def test_mapping_and_no_retry(self, core, outcome, http, expected) -> None:
        w = bw(core, FakeTransport(result=TransportResult(outcome=outcome, http_status=http)))
        d = w.decision(action="unfreeze", hours=0, rule_id=RULE)
        w.run()
        w.run(NOW + timedelta(seconds=60))
        assert w.status(d) == expected and len(w.transport.calls) == 1

    def test_transport_exception_is_ambiguous(self, core) -> None:
        w = bw(core, FakeTransport(raises=RuntimeError("x")))
        d = w.decision(action="unfreeze", hours=0, rule_id=RULE)
        w.run()
        assert w.status(d) == "needs_review"

    def test_orphaned_barrier_claim_is_not_resent(self, core) -> None:
        w = bw(core)
        d = w.decision(action="unfreeze", hours=0, rule_id=RULE, status="executing")
        w.run()
        assert w.status(d) == "needs_review" and not w.transport.calls


def _executing_row(w, did, minutes_ago):
    with w.core.transaction() as tx:
        tx.execute("INSERT INTO lock_decision_executions (id, decision_id, from_status, to_status, created_at, detail) "
                   "VALUES (?, ?, 'pending', 'executing', ?, 'x')", (str(uuid.uuid4()), did, iso(NOW - timedelta(minutes=minutes_ago))))


class TestSeparateLimitPools:
    def test_release_pool_allows_two_unfreezes_per_24h_with_a_4h_cooldown(self, core) -> None:
        recent = (RecentAction(created_at=NOW - timedelta(hours=10), action_type=A.UNFREEZE, hours=0),)
        assert apply_limits(A.FREEZE, 0, recent, NOW) == (0, None)
        assert apply_limits(A.UNFREEZE, 0, recent, NOW) == (0, None)
        two = recent + (RecentAction(created_at=NOW - timedelta(hours=5), action_type=A.UNFREEZE, hours=0),)
        assert "action limit" in apply_limits(A.UNFREEZE, 0, two, NOW)[1]
        assert "cooldown" in apply_limits(A.UNFREEZE, 0, (RecentAction(created_at=NOW - timedelta(hours=3), action_type=A.UNFREEZE, hours=0),), NOW)[1]

    def test_a_refreeze_neither_is_limited_nor_uses_the_release_budget(self, core) -> None:
        freezes = tuple(RecentAction(created_at=NOW - timedelta(hours=h), action_type=A.FREEZE, hours=0) for h in (1, 2, 3))
        assert apply_limits(A.FREEZE, 0, freezes, NOW) == (0, None)
        assert apply_limits(A.UNFREEZE, 0, freezes, NOW) == (0, None)

    def test_time_actions_do_not_consume_the_barrier_pool(self, core) -> None:
        time_actions = (
            RecentAction(created_at=NOW - timedelta(hours=5), action_type=A.ADD_TIME, hours=1),
            RecentAction(created_at=NOW - timedelta(hours=9), action_type=A.REMOVE_TIME, hours=1),
        )
        assert apply_limits(A.UNFREEZE, 0, time_actions, NOW) == (0, None)
        assert apply_limits(A.FREEZE, 0, time_actions, NOW) == (0, None)

    def test_barrier_actions_do_not_consume_the_time_pool(self, core) -> None:
        barrier = (
            RecentAction(created_at=NOW - timedelta(hours=1), action_type=A.UNFREEZE, hours=0),
            RecentAction(created_at=NOW - timedelta(hours=2), action_type=A.FREEZE, hours=0),
        )
        assert apply_limits(A.ADD_TIME, 2, barrier, NOW) == (2, None)

    def test_executor_applies_the_barrier_pool_to_executed_history(self, core) -> None:
        w = bw(core)
        first = w.decision(action="unfreeze", hours=0, rule_id=RULE, status="executed", age_minutes=300)
        _executing_row(w, first, 300)
        d = w.decision(action="unfreeze", hours=0, rule_id=RULE)
        w.run()
        assert w.status(d) == "executed"                     # 5 h later: outside the 4 h cooldown, 1 of 2 used

    def test_executor_cooldown_blocks_a_second_barrier_action(self, core) -> None:
        w = bw(core)
        first = w.decision(action="unfreeze", hours=0, rule_id=RULE, status="executed", age_minutes=60)
        _executing_row(w, first, 60)
        d = w.decision(action="unfreeze", hours=0, rule_id=RULE)
        w.run()
        assert w.status(d) == "blocked" and "cooldown" in w.detail(d)

    def test_executor_time_action_ignores_a_recent_barrier_action(self, core) -> None:
        w = bw(core, observation=obs(is_frozen=False))
        first = w.decision(action="unfreeze", hours=0, rule_id=RULE, status="executed", age_minutes=10)
        _executing_row(w, first, 10)
        d = w.decision(action="add_time", hours=1)
        w.run()
        assert w.status(d) == "executed"
