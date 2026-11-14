"""Roadmap slice 6: `setup tasks` adds Chaster's Tasks extension through the guarded extension review; `setup check` reads whether it is there."""

from __future__ import annotations

import io
import json
from types import SimpleNamespace

from chaster.extension_config import parse_lock_configuration
from chaster.extension_plan import TASKS_EXTENSION, plan_add_tasks
from chaster_execution.extension_review import ExtensionReviewOutcome as O
from cli.main import lock_tasks_reader, main
from cli.setup import render_report, setup_report
from tests.chaster_execution.test_extension_review import LOCK, NOW, OWNER, FakeReader, World, raw_lock
from tests.system.test_setup_command import config, migrated


def without_tasks():
    lock = raw_lock()
    lock["extensions"] = [e for e in lock["extensions"] if e["slug"] != "tasks"]
    return lock


def with_added_tasks():
    lock = without_tasks()
    lock["extensions"].append({"_id": "id-tasks", **json.loads(json.dumps(TASKS_EXTENSION)), "isPartner": False})
    return lock


def plan():
    return plan_add_tasks(parse_lock_configuration(without_tasks()))


class TestPlan:
    def test_tasks_is_added_and_everything_else_sent_back_as_read(self) -> None:
        p = plan()
        assert p.refusal is None and [e["slug"] for e in p.body["extensions"]] == ["pillory", "verification-picture", "tasks"]
        assert set(p.unchanged) == {"pillory", "verification-picture"}
        [change] = p.changes
        assert (change.slug, change.field, change.before) == ("tasks", "extension", "absent")
        tasks = p.body["extensions"][-1]["config"]
        assert tasks["peerVerification"] == {"enabled": True, "visibility": "all_members", "punishments": []}
        assert tasks["enablePoints"] is False and tasks["actionsOnAbandonedTask"] == [] and tasks["preventWearerFromAssigningTasks"] is True
        assert tasks["tasks"] == [{"task": "Show your lockbox", "points": 0, "verificationRequired": True, "codeRequired": True}]

    def test_no_time_action_anywhere(self) -> None:
        text = json.dumps(TASKS_EXTENSION).lower()
        assert "time" not in text.replace("voteduration", "").replace("startvoteafterlastvote", "")

    def test_a_lock_that_has_tasks_is_left_alone(self) -> None:
        assert not plan_add_tasks(parse_lock_configuration(raw_lock())).has_changes

    def test_a_partner_extension_refuses(self) -> None:
        lock = without_tasks()
        lock["extensions"][0]["isPartner"] = True
        assert plan_add_tasks(parse_lock_configuration(lock)).refusal == "partner_extension:pillory"


class TestService:
    def test_preview_then_apply_verified_and_audited(self, tmp_path) -> None:
        world = World(tmp_path, reader=FakeReader(without_tasks(), without_tasks(), with_added_tasks()))
        preview = world.service.preview(actor=OWNER, lock_id=LOCK, add_tasks=True)
        assert preview.outcome is O.PLANNED and preview.add_tasks and preview.plan.plan_id == plan().plan_id and world.transport.calls == []
        result = world.service.apply(actor=OWNER, lock_id=LOCK, plan_id=plan().plan_id, confirmed=True, add_tasks=True)
        assert result.outcome is O.APPLIED and world.transport.calls == [(LOCK, plan().body)]
        assert [r["outcome"] for r in world.audit()] == ["applied"]

    def test_the_halt_and_the_consent_hold(self, tmp_path) -> None:
        world = World(tmp_path, reader=FakeReader(without_tasks()), halted=True)
        assert world.service.apply(actor=OWNER, lock_id=LOCK, plan_id=plan().plan_id, confirmed=True, add_tasks=True).outcome is O.HALTED
        (tmp_path / "c").mkdir()
        world = World(tmp_path / "c", reader=FakeReader(without_tasks()), consent=False)
        assert world.service.apply(actor=OWNER, lock_id=LOCK, plan_id=plan().plan_id, confirmed=True, add_tasks=True).outcome is O.NO_CONSENT
        assert world.transport.calls == []

    def test_a_lock_with_tasks_already(self, tmp_path) -> None:
        world = World(tmp_path, reader=FakeReader(raw_lock()))
        assert world.service.preview(actor=OWNER, lock_id=LOCK, add_tasks=True).outcome is O.ALREADY_ON_TARGET


class TestCli:
    def _run(self, world, *args):
        out = io.StringIO()
        code = main(["setup", "tasks", LOCK, "--owner-id", OWNER, *args], out=out, review_factory=lambda: world.service, now=lambda: NOW)
        return code, out.getvalue()

    def test_dry_run_names_the_apply_command(self, tmp_path) -> None:
        world = World(tmp_path, reader=FakeReader(without_tasks()))
        code, text = self._run(world)
        assert code == 0 and text.startswith("DRY RUN: nothing was sent.") and "added: tasks, none removed." in text
        assert f"python -m cli setup tasks {LOCK} --owner-id {OWNER} --apply --plan {plan().plan_id} --yes" in text
        assert "SECRET" not in text and world.transport.calls == []

    def test_apply(self, tmp_path) -> None:
        world = World(tmp_path, reader=FakeReader(without_tasks(), with_added_tasks()))
        code, text = self._run(world, "--apply", "--plan", plan().plan_id, "--yes")
        assert code == 0 and "outcome=applied" in text


class TestCheck:
    def _lines(self, tmp_path, lock_tasks):
        return render_report(setup_report(config(tmp_path, enable_proof_checks=True), migrated(tmp_path), lock_tasks=lock_tasks))

    def test_each_locked_lock_is_reported(self, tmp_path) -> None:
        text = self._lines(tmp_path, lambda: [("a", "on"), ("b", "missing"), ("c", "review off")])
        assert "Tasks on lock a" in text and "present with community review" in text
        assert "missing: every spot check is skipped; `python -m cli setup tasks <lock id> --owner-id <id>` adds it" in text
        assert "community review is off" in text

    def test_unknown_is_unknown(self, tmp_path) -> None:
        def boom():
            raise RuntimeError("x")
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        assert "could not be read" in self._lines(tmp_path / "a", boom)
        assert "no Chaster account is connected" in self._lines(tmp_path / "b", lambda: None)

    def test_the_reader_reads_only_locked_locks(self) -> None:
        listing = SimpleNamespace(wearer_locks=[SimpleNamespace(lock_id=LOCK, status="locked"), SimpleNamespace(lock_id="old", status="unlocked")],
                                  keyholder_locks=[])
        directory = SimpleNamespace(list_account_locks=lambda user_id, now: listing)
        reader = FakeReader(without_tasks())
        assert lock_tasks_reader(lambda: (directory, reader, "acct"), lambda: NOW)() == [(LOCK, "missing")]
        assert reader.calls == [("acct", [LOCK])]
        assert lock_tasks_reader(lambda: (directory, reader, None), lambda: NOW)() is None
