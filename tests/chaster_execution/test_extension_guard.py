"""The extension guard: the bot keeps the owner's lock settings true by itself, without ever acting past a halt or claiming what a fresh read does not show. No network."""

from __future__ import annotations

import ast
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from chaster.extension_config import ConfigurationReport, ExtensionSnapshot, LockConfiguration
from chaster.extension_plan import REQUIRED_SETTINGS, plan_extension_policy
from chaster.managed_locks import LockRole
from chaster_execution.extension_guard import ACTOR, ExtensionGuard
from chaster_execution.models import TransportOutcome, TransportResult
from infrastructure.database import Database as CoreDatabase

ROOT = Path(__file__).parent.parent.parent
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
LOCK = "6aca95cdc5ee0f6d4989cd25"
OPENING = {"openingTime": 900, "penaltyTime": 43200, "allowOnlyKeyholderToOpen": False, "requireVerificationPictureBefore": False, "requireVerificationPictureAfter": False,
           "freezeLockWhileOpen": False}
TASKS = {"tasks": [{"task": "Private task text"}], "peerVerification": {"enabled": True}}


def _ext(slug, config, partner=False, mode="non_cumulative", regularity=172800.0):
    return ExtensionSnapshot(slug=slug, extension_id=f"id-{slug}", mode=mode, regularity=regularity, is_partner=partner, peer_verification=None, config=config)


def _config(*, penalty=43200, status="locked", role=LockRole.KEYHOLDER, subjects=frozenset({"keyholder"}), extra=()):
    return LockConfiguration(lock_id=LOCK, role=role, status=status, is_test_lock=True, edit_subjects=subjects,
                             extensions=(_ext("temporary-opening", {**OPENING, "penaltyTime": penalty}), _ext("tasks", TASKS), *extra))


class Reader:
    """Chaster as a list of states: each read takes the next, and the last one repeats."""

    def __init__(self, *states) -> None:
        self.states, self.reads = list(states), 0

    def read(self, *, user_id, lock_ids, now):
        self.reads += 1
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return ConfigurationReport(configurations=[state] if isinstance(state, LockConfiguration) else [], problems=[] if isinstance(state, LockConfiguration) else [state])


class Transport:
    def __init__(self, outcome=TransportOutcome.OK, raises=None) -> None:
        self.outcome, self.raises, self.calls = outcome, raises, []

    def set_extensions(self, *, lock_id, body):
        self.calls.append((lock_id, body))
        if self.raises:
            raise self.raises
        return TransportResult(outcome=self.outcome, http_status=201 if self.outcome is TransportOutcome.OK else 400)


class Control:
    def __init__(self, halted=False) -> None:
        self.halted = halted

    def is_halted(self):
        return self.halted


class World:
    def __init__(self, tmp_path, reader, *, transport=None, halted=False, lock_ids=(LOCK,)) -> None:
        self.core = CoreDatabase(tmp_path / "g.db")
        with self.core.raw_connection() as conn:
            for path in sorted((ROOT / "database" / "migrations").glob("*.sql")):
                conn.executescript(path.read_text(encoding="utf-8"))
        self.reader, self.transport, self.control, self.notices = reader, transport or Transport(), Control(halted), {}
        self.guard = ExtensionGuard(core=self.core, control=self.control, reader=reader, transport=self.transport, account_user_id="owner",
                                    list_lock_ids=lambda _n: (list(lock_ids), []), notify=lambda key, text, _now: self.notices.setdefault(key, text))

    def audit(self):
        with self.core.transaction() as tx:
            return [dict(r) for r in tx.fetch_all("SELECT * FROM manual_extension_actions ORDER BY created_at, rowid")]


def test_the_guard_is_off_without_a_connected_account(tmp_path) -> None:
    from types import SimpleNamespace

    from system.composition import build_extension_guard
    config = SimpleNamespace(enable_extension_guard=True, chaster_execution_keyholder_user_id=None, owner_user_id="o", db_path=tmp_path / "x.db")
    assert build_extension_guard(config, None, notifications=None, token_manager=object(), lock_client=object(), connections=object()) is None
    config.chaster_execution_keyholder_user_id, config.enable_extension_guard = "a", False
    assert build_extension_guard(config, None, notifications=None, token_manager=object(), lock_client=object(), connections=object()) is None


class TestThePlan:
    def test_the_rule_is_the_owners_decision(self) -> None:
        assert REQUIRED_SETTINGS == {"temporary-opening": {"penaltyTime": 0}}

    def test_only_that_field_changes_and_everything_is_sent_back(self) -> None:
        plan = plan_extension_policy(_config())
        assert [(c.slug, c.field, c.before, c.after) for c in plan.changes] == [("temporary-opening", "penaltyTime", "43200", "0")]
        entries = {e["slug"]: e for e in plan.body["extensions"]}
        assert entries["temporary-opening"]["config"] == {**OPENING, "penaltyTime": 0} and entries["tasks"]["config"] == TASKS
        assert entries["temporary-opening"]["regularity"] == 172800 and entries["temporary-opening"]["mode"] == "non_cumulative"

    def test_a_correct_lock_has_no_plan(self) -> None:
        assert not plan_extension_policy(_config(penalty=0)).has_changes

    def test_a_lock_without_the_extension_has_no_plan(self) -> None:
        assert not plan_extension_policy(replace(_config(), extensions=(_ext("tasks", TASKS),))).has_changes

    def test_a_partner_extension_refuses_the_rebuild(self) -> None:
        plan = plan_extension_policy(_config(extra=(_ext("wordle", {}, partner=True),)))
        assert plan.refusal == "partner_extension:wordle"

    def test_the_tasks_are_never_touched(self) -> None:
        """Removing or rebuilding `tasks` would cost the wearer's task list, so the plan must carry it through untouched."""
        assert plan_extension_policy(_config()).body["extensions"][1] == {"slug": "tasks", "config": TASKS, "mode": "non_cumulative", "regularity": 172800}


class TestCorrecting:
    def test_a_drifted_setting_is_corrected_verified_audited_and_reported(self, tmp_path) -> None:
        w = World(tmp_path, Reader(_config(), _config(penalty=0)))
        assert w.guard.run(NOW) == []
        assert len(w.transport.calls) == 1 and w.reader.reads == 2
        row = w.audit()[0]
        assert row["actor"] == ACTOR and row["outcome"] == "applied" and row["http_status"] == 201 and "penaltyTime 43200 -> 0" in row["changes"]
        assert len(w.notices) == 1 and "corrected" in next(iter(w.notices.values()))

    def test_a_correct_lock_is_left_alone(self, tmp_path) -> None:
        w = World(tmp_path, Reader(_config(penalty=0)))
        assert w.guard.run(NOW) == [] and w.transport.calls == [] and w.audit() == [] and w.notices == {}

    def test_it_comes_back_if_the_setting_drifts_again(self, tmp_path) -> None:
        w = World(tmp_path, Reader(_config(), _config(penalty=0), _config(), _config(penalty=0)))
        w.guard.run(NOW)
        w.guard.run(NOW + timedelta(days=1))
        assert len(w.transport.calls) == 2

    def test_a_201_that_changed_nothing_is_not_success(self, tmp_path) -> None:
        """Verified live: Chaster answers 201 to an edit it ignores."""
        w = World(tmp_path, Reader(_config()))
        problems = w.guard.run(NOW)
        assert problems == [f"{LOCK}: unverified"] and w.audit()[0]["outcome"] == "unverified"
        assert "does not show it" in next(iter(w.notices.values()))

    def test_an_unverified_write_is_not_repeated(self, tmp_path) -> None:
        w = World(tmp_path, Reader(_config()))
        w.guard.run(NOW)
        problems = w.guard.run(NOW + timedelta(hours=1))
        assert len(w.transport.calls) == 1 and "left for a person" in problems[0]

    @pytest.mark.parametrize("transport", [Transport(raises=OSError("reset")), Transport(outcome=TransportOutcome.UNKNOWN)])
    def test_an_ambiguous_write_is_never_retried(self, tmp_path, transport) -> None:
        w = World(tmp_path, Reader(_config()), transport=transport)
        w.guard.run(NOW)
        calls = len(transport.calls)
        problems = w.guard.run(NOW + timedelta(hours=1))
        assert len(transport.calls) == calls and "left for a person" in problems[0]

    def test_a_call_that_reached_nothing_tries_again(self, tmp_path) -> None:
        transport = Transport(outcome=TransportOutcome.NOT_SENT)
        w = World(tmp_path, Reader(_config()), transport=transport)
        w.guard.run(NOW)
        assert w.audit() == []
        w.guard.run(NOW + timedelta(minutes=15))
        assert len(transport.calls) == 2


class TestItNeverOversteps:
    def test_a_halt_stops_the_write_and_the_keyholder_hears_once(self, tmp_path) -> None:
        w = World(tmp_path, Reader(_config()), halted=True)
        w.guard.run(NOW)
        w.guard.run(NOW + timedelta(minutes=15))
        assert w.transport.calls == [] and len(w.notices) == 1 and "halted" in next(iter(w.notices.values()))
        assert [r["outcome"] for r in w.audit()] == ["halted"]

    def test_it_goes_out_once_the_halt_clears(self, tmp_path) -> None:
        w = World(tmp_path, Reader(_config(), _config(), _config(penalty=0)), halted=True)
        w.guard.run(NOW)
        w.control.halted = False
        w.guard.run(NOW + timedelta(minutes=15))
        assert len(w.transport.calls) == 1

    def test_a_lock_that_is_not_locked_is_left_alone(self, tmp_path) -> None:
        w = World(tmp_path, Reader(_config(status="unlocked")))
        w.guard.run(NOW)
        assert w.transport.calls == []

    def test_a_lock_this_account_may_not_edit_is_left_alone(self, tmp_path) -> None:
        w = World(tmp_path, Reader(_config(role=LockRole.WEARER)))
        w.guard.run(NOW)
        assert w.transport.calls == []

    def test_a_failed_read_writes_nothing(self, tmp_path) -> None:
        w = World(tmp_path, Reader("api_error:500"))
        assert w.guard.run(NOW) == ["api_error:500"] and w.transport.calls == []

    def test_a_list_it_cannot_rebuild_is_reported_not_sent(self, tmp_path) -> None:
        w = World(tmp_path, Reader(_config(extra=(_ext("wordle", {}, partner=True),))))
        assert w.guard.run(NOW) == [f"{LOCK}: partner_extension:wordle"] and w.transport.calls == [] and len(w.notices) == 1

    def test_no_notice_carries_a_task_text(self, tmp_path) -> None:
        w = World(tmp_path, Reader(_config(), _config(penalty=0)))
        w.guard.run(NOW)
        assert "Private task text" not in str(w.notices) and "Private task text" not in str(w.audit())

    def test_it_can_reach_only_the_policy_plan(self) -> None:
        """The community review plans stay out of reach: their forward write is CLI-only. The guard is excepted from that isolation test for this one plan and nothing else."""
        tree = ast.parse((ROOT / "chaster_execution" / "extension_guard.py").read_text(encoding="utf-8"))
        imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module == "chaster.extension_plan" for a in n.names}
        assert imported == {"ReviewPlan", "plan_extension_policy"}
        assert not any(isinstance(n, ast.ImportFrom) and (n.module or "").endswith("extension_review") for n in ast.walk(tree))

    def test_it_can_reach_only_set_extensions(self) -> None:
        tree = ast.parse((ROOT / "chaster_execution" / "extension_guard.py").read_text(encoding="utf-8"))
        calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and isinstance(n.func.value, ast.Attribute) and n.func.value.attr == "_transport"}
        assert calls == {"set_extensions"}

    def test_a_guard_failure_on_one_lock_does_not_stop_the_next(self, tmp_path) -> None:
        w = World(tmp_path, Reader(_config(penalty=0)))

        def boom(_config, _now):
            raise RuntimeError("boom")

        w.guard._check = boom
        assert w.guard.run(NOW) == [f"{LOCK}: failed"]
