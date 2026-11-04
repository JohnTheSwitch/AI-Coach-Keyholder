"""v0.39 dual-role Phase D: the guarded manual freeze / unfreeze (service and CLI). No network."""

from __future__ import annotations

import ast
import io
from datetime import datetime, timezone
from pathlib import Path

import pytest

from chaster.lock_directory import LockListing
from chaster.managed_locks import LockRole, ManagedLock
from chaster_execution.control import ExecutionControl
from chaster_execution.manual_freeze import ManualFreezeOutcome as O
from chaster_execution.manual_freeze import ManualFreezeService
from chaster_execution.models import TransportOutcome, TransportResult
from cli.main import describe, main
from infrastructure.clock import FrozenClock
from infrastructure.database import Database as CoreDatabase

ROOT = Path(__file__).parent.parent.parent
NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
OWNER = "owner-1"


def lock(lock_id="lock-1", *, frozen=False, status="locked", role=LockRole.KEYHOLDER):
    return ManagedLock(role=role, lock_id=lock_id, title="t", status=status, is_frozen=frozen, is_test_lock=True,
                       wearer_chaster_id="w", wearer_username="wearer", keyholder_chaster_id="k", keyholder_username="bot")


class FakeDirectory:
    def __init__(self, keyholder=(), wearer=(), problems=(), raises=False) -> None:
        self.listing = LockListing(wearer_locks=tuple(wearer), keyholder_locks=tuple(keyholder), problems=list(problems))
        self.raises, self.calls = raises, []

    def list_locks(self, *, wearer_user_id, keyholder_user_id, now):
        self.calls.append((wearer_user_id, keyholder_user_id))
        if self.raises:
            raise RuntimeError("boom")
        return self.listing


class FakeTransport:
    def __init__(self, result=None, raises=None, probe=None) -> None:
        self.calls, self.result, self.raises, self.probe = [], result or TransportResult(outcome=TransportOutcome.OK, http_status=204), raises, probe

    def set_frozen(self, *, lock_id, frozen):
        if self.probe:
            self.probe()
        self.calls.append((lock_id, frozen))
        if self.raises:
            raise self.raises
        return self.result


class World:
    def __init__(self, tmp_path: Path, *, directory=None, transport=None, owner=OWNER, halted=False) -> None:
        self.core = CoreDatabase(tmp_path / "t.db")
        migrations = sorted((ROOT / "database" / "migrations").glob("*.sql"))
        with self.core.raw_connection() as conn:
            for path in migrations:
                conn.executescript(path.read_text(encoding="utf-8"))
        self.control = ExecutionControl(self.core.db_path, core=self.core)
        self.control.set_halted(halted, reason="test", now=NOW)
        self.directory = directory or FakeDirectory(keyholder=[lock()])
        self.transport = transport or FakeTransport()
        self.service = ManualFreezeService(
            self.core.db_path, core=self.core, control=self.control, directory=self.directory, transport=self.transport,
            owner_user_id=owner, keyholder_user_id="kh-user", clock=FrozenClock(NOW))

    def run(self, *, actor=OWNER, lock_id="lock-1", frozen=True, confirmed=True):
        return self.service.run(actor=actor, lock_id=lock_id, frozen=frozen, confirmed=confirmed)

    def audit(self):
        with self.core.transaction() as tx:
            return [dict(r) for r in tx.fetch_all("SELECT * FROM manual_lock_actions ORDER BY rowid")]


@pytest.fixture
def w(tmp_path):
    return World(tmp_path)


class TestHappyPath:
    def test_freeze_is_applied_and_audited(self, w) -> None:
        result = w.run(frozen=True)
        assert result.outcome is O.APPLIED and result.applied and not result.refused
        assert w.transport.calls == [("lock-1", True)]
        [row] = w.audit()
        assert (row["actor"], row["lock_id"], row["requested_frozen"], row["outcome"], row["http_status"]) == (
            OWNER, "lock-1", 1, "applied", 204)
        assert row["created_at"].startswith("2026-10-12T12:00") and row["id"] == result.audit_id

    def test_unfreeze_sends_false(self, tmp_path) -> None:
        world = World(tmp_path, directory=FakeDirectory(keyholder=[lock(frozen=True)]))
        assert world.run(frozen=False).applied and world.transport.calls == [("lock-1", False)]
        assert world.audit()[0]["requested_frozen"] == 0

    def test_the_search_uses_the_keyholder_account_only(self, w) -> None:
        w.run()
        assert w.directory.calls == [(None, "kh-user")]


class TestRefusalsAreAuditedAndSendNothing:
    def _refused(self, world, outcome, **kw):
        result = world.run(**kw)
        assert result.outcome is outcome and result.refused and world.transport.calls == []
        assert [r["outcome"] for r in world.audit()] == [outcome.value]
        return result

    def test_no_owner_configured_refuses_everyone(self, tmp_path) -> None:
        for owner in (None, "", "  "):
            sub = tmp_path / f"o{owner!r}".replace("'", "").replace(" ", "_")
            sub.mkdir()
            self._refused(World(sub, owner=owner), O.OWNER_NOT_CONFIGURED)

    def test_wrong_or_empty_identity(self, w) -> None:
        self._refused(w, O.NOT_OWNER, actor="someone-else")
        assert w.directory.calls == []                             # nothing is even looked up
        assert [r["actor"] for r in w.audit()] == ["someone-else"]

    def test_the_identity_compare_is_exact(self, tmp_path) -> None:
        world = World(tmp_path)
        for actor in (OWNER.upper(), OWNER + " ", " " + OWNER, ""):
            assert world.run(actor=actor).outcome is O.NOT_OWNER
        assert world.transport.calls == []

    def test_confirmation_is_required(self, w) -> None:
        self._refused(w, O.NOT_CONFIRMED, confirmed=False)
        assert w.directory.calls == []

    @pytest.mark.parametrize("lock_id", ["", "a b", "x/../y", "x" * 65, "ü", "lock;rm"])
    def test_a_malformed_lock_id(self, w, lock_id) -> None:
        self._refused(w, O.INVALID_LOCK_ID, lock_id=lock_id)

    def test_a_halted_runtime_refuses_immediately(self, tmp_path) -> None:
        world = World(tmp_path, halted=True)
        self._refused(world, O.HALTED)
        assert world.directory.calls == []                        # refused before any provider call

    def test_an_unreadable_halt_control_counts_as_halted(self, w) -> None:
        with w.core.transaction() as tx:
            tx.execute("DROP TABLE execution_control")
        assert w.run().outcome is O.HALTED and w.transport.calls == []

    def test_a_self_lock_is_not_a_managed_lock(self, tmp_path) -> None:
        world = World(tmp_path, directory=FakeDirectory(keyholder=[], wearer=[lock("self-1", role=LockRole.WEARER)]))
        self._refused(world, O.NOT_MANAGED_LOCK, lock_id="self-1")

    def test_an_unknown_or_unlocked_lock_is_not_managed(self, tmp_path) -> None:
        world = World(tmp_path, directory=FakeDirectory(keyholder=[lock("done-1", status="unlocked")]))
        self._refused(world, O.NOT_MANAGED_LOCK, lock_id="done-1")
        self._refused_again = world.run(lock_id="nope-1")
        assert self._refused_again.outcome is O.NOT_MANAGED_LOCK

    @pytest.mark.parametrize("kw", [dict(problems=["keyholder: missing_scope:keyholder"]), dict(problems=["keyholder: not_connected"]),
                                    dict(raises=True)])
    def test_an_unverifiable_scope_refuses(self, tmp_path, kw) -> None:
        world = World(tmp_path, directory=FakeDirectory(keyholder=[lock()], **kw))
        self._refused(world, O.SCOPE_UNVERIFIABLE)

    def test_wearer_side_problems_do_not_matter(self, tmp_path) -> None:
        world = World(tmp_path, directory=FakeDirectory(keyholder=[lock()], problems=["wearer: not_configured"]))
        assert world.run().applied

    def test_an_unknown_frozen_state_refuses(self, tmp_path) -> None:
        bad = lock()
        object.__setattr__(bad, "is_frozen", None)
        self._refused(World(tmp_path, directory=FakeDirectory(keyholder=[bad])), O.STATE_UNKNOWN)

    def test_already_in_the_requested_state_sends_nothing(self, tmp_path) -> None:
        world = World(tmp_path, directory=FakeDirectory(keyholder=[lock(frozen=True)]))
        self._refused(world, O.ALREADY_IN_STATE, frozen=True)


class TestAttempts:
    @pytest.mark.parametrize("transport_outcome,expected,status", [
        (TransportOutcome.NOT_SENT, O.NOT_SENT, None), (TransportOutcome.UNAUTHORIZED, O.UNAUTHORIZED, 401),
        (TransportOutcome.FORBIDDEN, O.FORBIDDEN, 403), (TransportOutcome.NOT_ELIGIBLE, O.NOT_ELIGIBLE, 400),
        (TransportOutcome.NOT_FOUND, O.NOT_FOUND, 404), (TransportOutcome.UNKNOWN, O.UNKNOWN, 500)])
    def test_every_transport_outcome_is_mapped_and_audited(self, tmp_path, transport_outcome, expected, status) -> None:
        world = World(tmp_path, transport=FakeTransport(TransportResult(outcome=transport_outcome, http_status=status)))
        result = world.run()
        assert result.outcome is expected and not result.applied and not result.refused
        assert [(r["outcome"], r["http_status"]) for r in world.audit()] == [(expected.value, status)]

    def test_the_attempt_is_recorded_before_the_call(self, tmp_path) -> None:
        seen = []
        world = World(tmp_path)
        world.transport.probe = lambda: seen.append([r["outcome"] for r in world.audit()])
        world.run()
        assert seen == [["attempting"]] and [r["outcome"] for r in world.audit()] == ["applied"]

    def test_an_exception_during_the_call_is_unknown_and_never_retried(self, tmp_path) -> None:
        world = World(tmp_path, transport=FakeTransport(raises=RuntimeError("socket died")))
        result = world.run()
        assert result.outcome is O.UNKNOWN and len(world.transport.calls) == 1
        assert [r["outcome"] for r in world.audit()] == ["unknown"]

    def test_each_invocation_gets_its_own_audit_row(self, w) -> None:
        w.run(actor="x")
        w.run(confirmed=False)
        w.run()
        assert len(w.audit()) == 3 and len({r["id"] for r in w.audit()}) == 3

    def test_no_secret_or_token_text_reaches_the_audit(self, w) -> None:
        w.run()
        assert "token" not in str(w.audit()).lower() and "bearer" not in str(w.audit()).lower()

    def test_long_inputs_are_truncated_in_the_audit(self, w) -> None:
        w.run(actor="a" * 500, lock_id="b" * 500)
        row = w.audit()[0]
        assert len(row["actor"]) == 64 and len(row["lock_id"]) == 64


class TestCli:
    def _run(self, world, *args):
        out = io.StringIO()
        code = main(["freeze-lock", *args], out=out, freeze_factory=lambda: world.service)
        return code, out.getvalue()

    def test_success_exit_code_and_output(self, w) -> None:
        code, text = self._run(w, "lock-1", "--state", "true", "--owner-id", OWNER, "--yes")
        assert code == 0 and "Done" in text and "outcome=applied" in text and w.transport.calls == [("lock-1", True)]
        assert w.audit()[0]["id"] in text

    def test_state_false_unfreezes(self, tmp_path) -> None:
        world = World(tmp_path, directory=FakeDirectory(keyholder=[lock(frozen=True)]))
        code, _ = self._run(world, "lock-1", "--state", "false", "--owner-id", OWNER, "--yes")
        assert code == 0 and world.transport.calls == [("lock-1", False)]

    def test_without_yes_nothing_happens(self, w) -> None:
        code, text = self._run(w, "lock-1", "--state", "true", "--owner-id", OWNER)
        assert code == 3 and "--yes" in text and w.transport.calls == []

    def test_wrong_owner_is_refused(self, w) -> None:
        code, text = self._run(w, "lock-1", "--state", "true", "--owner-id", "mallory", "--yes")
        assert code == 3 and "not the owner" in text and w.transport.calls == []

    def test_halt_is_refused(self, tmp_path) -> None:
        world = World(tmp_path, halted=True)
        code, text = self._run(world, "lock-1", "--state", "true", "--owner-id", OWNER, "--yes")
        assert code == 3 and "halted" in text and world.transport.calls == []

    def test_an_ambiguous_result_exits_4_and_says_so(self, tmp_path) -> None:
        world = World(tmp_path, transport=FakeTransport(TransportResult(outcome=TransportOutcome.UNKNOWN)))
        code, text = self._run(world, "lock-1", "--state", "true", "--owner-id", OWNER, "--yes")
        assert code == 4 and "may or may not" in text

    def test_already_in_state_is_a_clean_exit(self, tmp_path) -> None:
        world = World(tmp_path, directory=FakeDirectory(keyholder=[lock(frozen=True)]))
        code, text = self._run(world, "lock-1", "--state", "true", "--owner-id", OWNER, "--yes")
        assert code == 0 and "already" in text

    @pytest.mark.parametrize("args", [["lock-1"], ["lock-1", "--state", "maybe", "--owner-id", OWNER], ["lock-1", "--state", "true"]])
    def test_bad_arguments_exit_before_anything_runs(self, w, args) -> None:
        with pytest.raises(SystemExit):
            self._run(w, *args)
        assert w.audit() == [] and w.transport.calls == []

    def test_every_outcome_has_a_description(self) -> None:
        for outcome in O:
            assert describe(outcome.value) != outcome.value or outcome.value == "unknown"


class TestIsolation:
    def test_nothing_but_tests_imports_the_cli_or_the_manual_service(self) -> None:
        offenders = []
        for path in ROOT.rglob("*.py"):
            rel = path.relative_to(ROOT)
            if rel.parts[0] in {"cli", "tests", "chaster_execution"} or rel.parts[0].startswith(".") or "__pycache__" in rel.parts:
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                    [node.module] if isinstance(node, ast.ImportFrom) and node.module else []
                offenders += [f"{rel}: {n}" for n in names if n.split(".")[0] == "cli" or n.endswith("manual_freeze")]
        assert offenders == []

    def test_the_manual_service_is_not_exported_to_the_wiring(self) -> None:
        text = (ROOT / "system" / "composition.py").read_text(encoding="utf-8")
        assert "manual_freeze" not in text and "ManualFreezeService" not in text

    def test_owner_setting_and_migration(self, tmp_path) -> None:
        from core.config import Config
        assert Config.__dataclass_fields__["owner_user_id"].default is None
        assert "OWNER_USER_ID=" in (ROOT / ".env.example").read_text(encoding="utf-8")
        world = World(tmp_path)
        with world.core.transaction() as tx:
            assert tx.fetch_one("SELECT version FROM schema_version WHERE version = 34")
