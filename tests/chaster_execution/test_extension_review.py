"""v0.39 Phase I2: the review-settings plan, the extensions write in the transport, the guarded service and the `extension-review` CLI.
No network."""

from __future__ import annotations

import ast
import copy
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

from chaster.extension_config import ConfigurationReport, parse_lock_configuration, review_gaps
from chaster.extension_plan import canonical_digest, plan_review_update
from chaster_execution.control import ExecutionControl
from chaster_execution.extension_review import ExtensionReviewOutcome as O
from chaster_execution.extension_review import ExtensionReviewService
from chaster_execution.models import TransportOutcome, TransportResult
from chaster_execution.review_consent import ConsentDecision, ReviewConsentLedger
from chaster_execution.transport import ChasterHttpTransport
from cli.main import main, render_review
from infrastructure.clock import FrozenClock
from infrastructure.database import Database as CoreDatabase

ROOT = Path(__file__).parent.parent.parent
NOW = datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc)
OWNER = "owner-1"
LOCK = "6aca95cdc5ee0f6d4989cd25"


def _ext(slug, config, mode="non_cumulative", regularity=3600, **extra):
    return {"_id": f"id-{slug}", "slug": slug, "mode": mode, "regularity": regularity, "config": config, "isPartner": False,
            "userData": {"currentVerificationCode": "SECRET-CODE"}, "nbActionsRemaining": 1, **extra}


def raw_lock(**overrides):
    lock = {
        "_id": LOCK, "role": "keyholder", "status": "locked", "isTestLock": True, "combination": "SECRET-COMBINATION",
        "permissions": {"preset": "trusted_keyholder", "grants": [
            {"resource": "lock.extensions.edit", "category": "extensions", "subjects": {"wearer": [], "keyholder": ["edit"]}}]},
        "extensions": [
            _ext("pillory", {"timeToAdd": 3600, "limitToLoggedUsers": True}, mode="unlimited"),
            _ext("tasks", {"tasks": [{"task": "SECRET TASK TEXT", "points": 0}], "voteEnabled": False,
                           "peerVerification": {"enabled": True, "visibility": "keyholder", "punishments": [{"name": "add_time", "params": 3600}],
                                                "futureField": "kept"}}),
            _ext("verification-picture", {"visibility": "keyholder", "triggerInitialVerification": False,
                                          "peerVerification": {"enabled": False, "punishments": []}}, regularity=86400),
        ],
    }
    lock.update(overrides)
    return lock


def on_target(lock):
    lock = copy.deepcopy(lock)
    for e in lock["extensions"]:
        if e["slug"] in ("tasks", "verification-picture"):
            e["config"]["peerVerification"].update({"enabled": True, "visibility": "all_members", "punishments": []})
    return lock


def plan_for(lock=None):
    return plan_review_update(parse_lock_configuration(lock or raw_lock()))


# ---------------------------------------------------------------------------------------------------------------- the plan

class TestPlan:
    def test_the_body_is_the_complete_list_in_order_with_only_the_documented_fields(self) -> None:
        plan = plan_for()
        assert [e["slug"] for e in plan.body["extensions"]] == ["pillory", "tasks", "verification-picture"]
        assert all(set(e) == {"slug", "config", "mode", "regularity"} for e in plan.body["extensions"])
        assert set(plan.body) == {"extensions"} and plan.refusal is None

    def test_only_the_three_review_fields_change(self) -> None:
        raw = raw_lock()
        plan = plan_for(raw)
        sent = {e["slug"]: e for e in plan.body["extensions"]}
        read = {e["slug"]: e for e in raw["extensions"]}
        assert sent["pillory"]["config"] == read["pillory"]["config"] and sent["pillory"]["mode"] == "unlimited"
        for slug in ("tasks", "verification-picture"):
            before, after = dict(read[slug]["config"]), dict(sent[slug]["config"])
            pv_before, pv_after = before.pop("peerVerification"), after.pop("peerVerification")
            assert after == before                                          # every other config field is untouched
            assert {k: v for k, v in pv_after.items() if k not in ("enabled", "visibility", "punishments")} == \
                   {k: v for k, v in pv_before.items() if k not in ("enabled", "visibility", "punishments")}
            assert (pv_after["enabled"], pv_after["visibility"], pv_after["punishments"]) == (True, "all_members", [])
        assert sent["tasks"]["config"]["peerVerification"]["futureField"] == "kept"
        assert sent["tasks"]["config"]["tasks"][0]["task"] == "SECRET TASK TEXT"         # the task list is sent back, not dropped

    def test_the_changes_are_listed_exactly(self) -> None:
        assert [(c.slug, c.field, c.before, c.after) for c in plan_for().changes] == [
            ("tasks", "peerVerification.visibility", "keyholder", "all_members"),
            ("tasks", "peerVerification.punishments", "[add_time(3600)]", "[]"),
            ("verification-picture", "peerVerification.enabled", "off", "on"),
            ("verification-picture", "peerVerification.visibility", "unset", "all_members")]
        assert plan_for().unchanged == ("pillory",)

    def test_a_lock_on_target_has_no_changes_and_the_target_has_no_gaps(self) -> None:
        plan = plan_for(on_target(raw_lock()))
        assert not plan.has_changes and plan.refusal is None
        assert review_gaps(parse_lock_configuration(on_target(raw_lock()))) == ()

    def test_a_missing_peer_verification_object_is_created(self) -> None:
        raw = raw_lock()
        del raw["extensions"][2]["config"]["peerVerification"]
        sent = plan_for(raw).body["extensions"][2]["config"]["peerVerification"]
        assert sent == {"enabled": True, "visibility": "all_members", "punishments": []}

    def test_the_plan_never_changes_the_read_configuration(self) -> None:
        config = parse_lock_configuration(raw_lock())
        plan_review_update(config)
        assert config.extension("tasks").config["peerVerification"]["visibility"] == "keyholder"

    def test_the_plan_id_is_deterministic_and_follows_any_change(self) -> None:
        assert plan_for().plan_id == plan_for().plan_id == canonical_digest(plan_for().body)
        assert len(plan_for().plan_id) == 16 and int(plan_for().plan_id, 16) >= 0
        changed = raw_lock()
        changed["extensions"][0]["config"]["timeToAdd"] = 7200
        assert plan_for(changed).plan_id != plan_for().plan_id

    @pytest.mark.parametrize("mutate, refusal", [
        (lambda l: l["extensions"].pop(2), "missing_extension:verification-picture"),
        (lambda l: l["extensions"][0].update(isPartner=True), "partner_extension:pillory"),
        (lambda l: l["extensions"][0].update(mode="sometimes"), "incomplete_extension:pillory"),
        (lambda l: l["extensions"][0].pop("mode"), "incomplete_extension:pillory"),
        (lambda l: l["extensions"][1].update(regularity=None), "incomplete_extension:tasks"),
        (lambda l: l["extensions"][1].update(regularity=-1), "incomplete_extension:tasks"),
    ])
    def test_a_body_that_cannot_be_rebuilt_faithfully_is_refused(self, mutate, refusal) -> None:
        raw = raw_lock()
        mutate(raw)
        assert plan_for(raw).refusal == refusal

    def test_no_secret_reaches_repr(self) -> None:
        assert "SECRET" not in repr(plan_for())


# ---------------------------------------------------------------------------------------------------------------- the transport

class FakeTokens:
    def __init__(self, raises=None) -> None:
        self.raises, self.calls = raises, []

    def call_with_token(self, user_id, *, now, operation):
        self.calls.append(user_id)
        if self.raises:
            raise self.raises
        return operation("tok")


class TestTransport:
    def _transport(self, response=None, raises=None, tokens=None):
        sent = []

        def send(method, url, **kw):
            sent.append((method, url, kw))
            if raises:
                raise raises
            return response or MagicMock(status_code=201)
        return ChasterHttpTransport(token_manager=tokens or FakeTokens(), keyholder_user_id="acct", clock=FrozenClock(NOW), send=send), sent

    def test_posts_the_complete_body_to_the_documented_endpoint(self) -> None:
        transport, sent = self._transport()
        body = plan_for().body
        result = transport.set_extensions(lock_id=LOCK, body=body)
        method, url, kw = sent[0]
        assert method == "POST" and url == f"https://api.chaster.app/locks/{LOCK}/extensions" and kw["json"] is body
        assert kw["headers"]["Authorization"] == "Bearer tok" and result == TransportResult(outcome=TransportOutcome.OK, http_status=201)

    @pytest.mark.parametrize("status, outcome", [(200, TransportOutcome.OK), (204, TransportOutcome.OK), (400, TransportOutcome.NOT_ELIGIBLE),
                                                 (403, TransportOutcome.FORBIDDEN), (404, TransportOutcome.NOT_FOUND), (500, TransportOutcome.UNKNOWN),
                                                 (202, TransportOutcome.UNKNOWN)])
    def test_status_mapping(self, status, outcome) -> None:
        transport, _ = self._transport(response=MagicMock(status_code=status))
        assert transport.set_extensions(lock_id=LOCK, body=plan_for().body).outcome is outcome

    def test_the_freeze_mapping_is_unchanged(self) -> None:
        transport, _ = self._transport(response=MagicMock(status_code=201))
        assert transport.set_frozen(lock_id=LOCK, frozen=True).outcome is TransportOutcome.UNKNOWN      # freeze succeeds only on 204

    @pytest.mark.parametrize("lock_id, body", [("../x", {"extensions": [{}]}), (LOCK, {"extensions": []}), (LOCK, {"extensions": "x"}),
                                               (LOCK, {"extensions": [{}], "extra": 1}), (LOCK, None), (LOCK, [])])
    def test_bad_input_is_never_sent(self, lock_id, body) -> None:
        transport, sent = self._transport()
        assert transport.set_extensions(lock_id=lock_id, body=body).outcome is TransportOutcome.NOT_SENT and sent == []

    def test_a_read_timeout_is_ambiguous_and_a_connect_timeout_is_not_sent(self) -> None:
        transport, _ = self._transport(raises=requests.ReadTimeout("slow"))
        assert transport.set_extensions(lock_id=LOCK, body=plan_for().body).outcome is TransportOutcome.UNKNOWN
        transport, _ = self._transport(raises=requests.ConnectTimeout("down"))
        assert transport.set_extensions(lock_id=LOCK, body=plan_for().body).outcome is TransportOutcome.NOT_SENT


# ---------------------------------------------------------------------------------------------------------------- the service

class FakeReader:
    def __init__(self, *locks, problem=None, raises=False) -> None:
        self.locks, self.problem, self.raises, self.calls = list(locks), problem, raises, []

    def read(self, *, user_id, lock_ids, now):
        self.calls.append((user_id, list(lock_ids)))
        if self.raises:
            raise RuntimeError("boom")
        report = ConfigurationReport()
        if self.problem:
            report.problems.append(f"{lock_ids[0]}: {self.problem}")
            return report
        raw = self.locks.pop(0) if len(self.locks) > 1 else self.locks[0]
        report.configurations.append(parse_lock_configuration(raw))
        return report


class FakeTransport:
    def __init__(self, result=None, raises=None, probe=None) -> None:
        self.result = result or TransportResult(outcome=TransportOutcome.OK, http_status=201)
        self.raises, self.probe, self.calls = raises, probe, []

    def set_extensions(self, *, lock_id, body):
        if self.probe:
            self.probe()
        self.calls.append((lock_id, body))
        if self.raises:
            raise self.raises
        return self.result


class World:
    def __init__(self, tmp_path: Path, *, reader=None, transport=None, owner=OWNER, account="acct", halted=False, consent=True) -> None:
        self.core = CoreDatabase(tmp_path / "t.db")
        with self.core.raw_connection() as conn:
            for path in sorted((ROOT / "database" / "migrations").glob("*.sql")):
                conn.executescript(path.read_text(encoding="utf-8"))
        self.control = ExecutionControl(self.core.db_path, core=self.core)
        self.control.set_halted(halted, reason="test", now=NOW)
        self.reader = reader or FakeReader(raw_lock(), on_target(raw_lock()))
        self.transport = transport or FakeTransport()
        self.service = ExtensionReviewService(self.core.db_path, core=self.core, control=self.control, reader=self.reader,
                                              transport=self.transport, owner_user_id=owner, account_user_id=account, clock=FrozenClock(NOW))
        self.ledger = ReviewConsentLedger(self.core.db_path, core=self.core)
        if consent:
            self.ledger.record(lock_id=LOCK, decision=ConsentDecision.APPROVED, comment="test", now=NOW)

    def apply(self, *, actor=OWNER, lock_id=LOCK, plan_id=None, confirmed=True):
        return self.service.apply(actor=actor, lock_id=lock_id, plan_id=plan_for().plan_id if plan_id is None else plan_id, confirmed=confirmed)

    def audit(self):
        with self.core.transaction() as tx:
            return [dict(r) for r in tx.fetch_all("SELECT * FROM manual_extension_actions ORDER BY created_at, rowid")]


@pytest.fixture
def w(tmp_path):
    return World(tmp_path)


class TestPreview:
    def test_a_dry_run_sends_nothing_writes_nothing_and_works_while_halted(self, tmp_path) -> None:
        world = World(tmp_path, halted=True)
        result = world.service.preview(actor=OWNER, lock_id=LOCK)
        assert result.outcome is O.PLANNED and result.plan.plan_id == plan_for().plan_id and result.halted is True
        assert result.blockers == ('runtime halted (python -m scripts.execution_control clear "reason")',)
        assert world.transport.calls == [] and world.audit() == [] and world.reader.calls == [("acct", [LOCK])]

    def test_it_lists_every_blocker(self, tmp_path) -> None:
        raw = raw_lock(status="unlocked", role="wearer")
        raw["extensions"][0]["isPartner"] = True
        world = World(tmp_path, reader=FakeReader(raw), consent=False)
        world.ledger.record(lock_id=LOCK, decision=ConsentDecision.REVOKED, comment="t", now=NOW)
        result = world.service.preview(actor=OWNER, lock_id=LOCK)
        assert result.blockers == ("lock status is unlocked, not locked", "the wearer role may not edit extensions on this lock",
                                   "plan refused: partner_extension:pillory",
                                   f"the wearer's consent to community review is revoked (python -m cli review-consent {LOCK} ...)")

    def test_a_lock_on_target(self, tmp_path) -> None:
        result = World(tmp_path, reader=FakeReader(on_target(raw_lock()))).service.preview(actor=OWNER, lock_id=LOCK)
        assert result.outcome is O.ALREADY_ON_TARGET and result.blockers == ()

    def test_owner_only_and_input_checks(self, tmp_path, w) -> None:
        assert w.service.preview(actor="someone", lock_id=LOCK).outcome is O.NOT_OWNER
        assert w.service.preview(actor=OWNER, lock_id="../x").outcome is O.INVALID_INPUT
        (tmp_path / "o").mkdir()
        assert World(tmp_path / "o", owner=None).service.preview(actor=OWNER, lock_id=LOCK).outcome is O.OWNER_NOT_CONFIGURED
        assert w.reader.calls == []

    def test_a_read_failure_is_reported_with_its_code(self, tmp_path) -> None:
        result = World(tmp_path, reader=FakeReader(problem="api_error:403")).service.preview(actor=OWNER, lock_id=LOCK)
        assert result.outcome is O.READ_FAILED and result.detail == "api_error:403"


class TestApply:
    def test_the_reviewed_plan_is_sent_once_verified_and_audited(self, w) -> None:
        result = w.apply()
        assert result.outcome is O.APPLIED and result.http_status == 201 and result.remaining_gaps == ()
        assert w.transport.calls == [(LOCK, plan_for().body)]
        row, = w.audit()
        assert row["outcome"] == "applied" and row["http_status"] == 201 and row["plan_id"] == plan_for().plan_id and row["actor"] == OWNER
        assert row["changes"] == ("tasks peerVerification.visibility keyholder -> all_members; tasks peerVerification.punishments "
                                  "[add_time(3600)] -> []; verification-picture peerVerification.enabled off -> on; "
                                  "verification-picture peerVerification.visibility unset -> all_members")

    def test_the_attempt_is_recorded_before_the_call(self, tmp_path) -> None:
        seen = []
        world = World(tmp_path, transport=FakeTransport(probe=lambda: seen.append(world.audit()[0]["outcome"])))
        world.apply()
        assert seen == ["attempting"]

    @pytest.mark.parametrize("kwargs, outcome", [
        ({"actor": "someone"}, O.NOT_OWNER), ({"actor": OWNER.upper()}, O.NOT_OWNER), ({"confirmed": False}, O.NOT_CONFIRMED),
        ({"lock_id": "../x"}, O.INVALID_INPUT), ({"plan_id": ""}, O.INVALID_INPUT), ({"plan_id": "B92932793208FE65"}, O.INVALID_INPUT),
        ({"plan_id": "0" * 16}, O.PLAN_MISMATCH),
    ])
    def test_refusals_send_nothing_and_are_audited(self, w, kwargs, outcome) -> None:
        result = w.apply(**kwargs)
        assert result.outcome is outcome and result.refused and w.transport.calls == []
        assert [r["outcome"] for r in w.audit()] == [outcome.value]

    def test_the_runtime_halt_refuses_before_any_read(self, tmp_path) -> None:
        world = World(tmp_path, halted=True)
        assert world.apply().outcome is O.HALTED and world.reader.calls == [] and world.transport.calls == []

    def test_an_unreadable_halt_control_counts_as_halted(self, w) -> None:
        w.control.is_halted = lambda: True
        assert w.apply().outcome is O.HALTED

    @pytest.mark.parametrize("world_kw, outcome", [
        ({"owner": None}, O.OWNER_NOT_CONFIGURED), ({"account": None}, O.ACCOUNT_NOT_CONFIGURED),
        ({"reader": FakeReader(problem="reauthorization_required")}, O.READ_FAILED), ({"reader": FakeReader(raises=True)}, O.READ_FAILED),
        ({"reader": FakeReader(raw_lock(status="unlocked"))}, O.NOT_LOCKED),
        ({"reader": FakeReader(raw_lock(role="wearer"))}, O.NOT_PERMITTED),
        ({"reader": FakeReader(on_target(raw_lock()))}, O.ALREADY_ON_TARGET),
    ])
    def test_lock_state_refusals(self, tmp_path, world_kw, outcome) -> None:
        world = World(tmp_path, **world_kw)
        assert world.apply().outcome is outcome and world.transport.calls == []

    def test_a_partner_extension_refuses_the_plan(self, tmp_path) -> None:
        raw = raw_lock()
        raw["extensions"][0]["isPartner"] = True
        world = World(tmp_path, reader=FakeReader(raw))
        result = world.apply(plan_id=plan_for(raw).plan_id)
        assert result.outcome is O.PLAN_REFUSED and result.detail == "partner_extension:pillory" and world.transport.calls == []

    def test_a_lock_that_changed_since_the_dry_run_is_refused(self, tmp_path) -> None:
        changed = raw_lock()
        changed["extensions"][0]["config"]["timeToAdd"] = 60
        world = World(tmp_path, reader=FakeReader(changed))
        assert world.apply().outcome is O.PLAN_MISMATCH and world.transport.calls == []

    def test_the_wearer_role_works_when_the_contract_allows_it(self, tmp_path) -> None:
        raw = raw_lock(role="wearer")
        raw["permissions"]["grants"][0]["subjects"]["wearer"] = ["edit"]
        world = World(tmp_path, reader=FakeReader(raw, on_target(raw)))
        assert world.apply(plan_id=plan_for(raw).plan_id).outcome is O.APPLIED

    @pytest.mark.parametrize("transport_outcome, expected, status", [
        (TransportOutcome.NOT_SENT, O.NOT_SENT, None), (TransportOutcome.UNAUTHORIZED, O.UNAUTHORIZED, 401),
        (TransportOutcome.FORBIDDEN, O.FORBIDDEN, 403), (TransportOutcome.NOT_ELIGIBLE, O.NOT_ELIGIBLE, 400),
        (TransportOutcome.NOT_FOUND, O.NOT_FOUND, 404), (TransportOutcome.UNKNOWN, O.UNKNOWN, 500),
    ])
    def test_every_transport_failure_is_mapped_audited_and_not_verified(self, tmp_path, transport_outcome, expected, status) -> None:
        world = World(tmp_path, transport=FakeTransport(TransportResult(outcome=transport_outcome, http_status=status)))
        assert world.apply().outcome is expected and len(world.reader.calls) == 1
        row, = world.audit()
        assert row["outcome"] == expected.value and row["http_status"] == status

    def test_an_exception_during_the_call_is_unknown_and_never_retried(self, tmp_path) -> None:
        world = World(tmp_path, transport=FakeTransport(raises=RuntimeError("boom")))
        assert world.apply().outcome is O.UNKNOWN and len(world.transport.calls) == 1 and world.audit()[0]["outcome"] == "unknown"

    def test_a_re_read_that_does_not_confirm_is_applied_unverified(self, tmp_path) -> None:
        world = World(tmp_path, reader=FakeReader(raw_lock(), raw_lock()))
        result = world.apply()
        assert result.outcome is O.APPLIED_UNVERIFIED and "tasks: visibility keyholder (target all_members)" in result.remaining_gaps
        shrunk = on_target(raw_lock())
        shrunk["extensions"].pop(0)
        (tmp_path / "b").mkdir()
        world = World(tmp_path / "b", reader=FakeReader(raw_lock(), shrunk))
        result = world.apply()
        assert result.outcome is O.APPLIED_UNVERIFIED and result.remaining_gaps == ("the set of extensions changed",)

    def test_no_secret_reaches_the_audit(self, w) -> None:
        w.apply()
        assert "SECRET" not in json.dumps(w.audit()) and "tok" not in json.dumps(w.audit())


# ---------------------------------------------------------------------------------------------------------------- the CLI

class TestCli:
    def _run(self, world, *args):
        out = io.StringIO()
        code = main(["extension-review", LOCK, "--owner-id", OWNER, *args], out=out, review_factory=lambda: world.service, now=lambda: NOW)
        return code, out.getvalue()

    def test_the_default_is_a_dry_run_with_the_exact_diff_and_the_apply_command(self, tmp_path) -> None:
        world = World(tmp_path, halted=True)
        code, text = self._run(world)
        plan_id = plan_for().plan_id
        assert code == 0 and text.startswith("DRY RUN: nothing was sent.") and world.transport.calls == [] and world.audit() == []
        assert "tasks peerVerification.punishments" in text and "[add_time(3600)]  ->  []" in text
        assert "ALL 3 extensions (pillory, tasks, verification-picture); none added, none removed." in text
        assert "pillory: mode unlimited, regularity 3600, config keys limitToLoggedUsers, timeToAdd (sent back exactly as read)" in text
        assert f"Plan id: {plan_id}" in text and "Runtime halt: HALTED" in text and "runtime halted" in text
        assert f"--apply --plan {plan_id} --yes" in text
        assert "SECRET" not in text

    def test_apply_with_the_plan_writes(self, w) -> None:
        code, text = self._run(w, "--apply", "--plan", plan_for().plan_id, "--yes")
        assert code == 0 and text.startswith("Done:") and "outcome=applied" in text and len(w.transport.calls) == 1

    def test_apply_without_yes_or_with_the_halt_is_refused(self, tmp_path, w) -> None:
        code, text = self._run(w, "--apply", "--plan", plan_for().plan_id)
        assert code == 3 and "add --yes" in text and w.transport.calls == []
        (tmp_path / "h").mkdir()
        world = World(tmp_path / "h", halted=True)
        code, text = self._run(world, "--apply", "--plan", plan_for().plan_id, "--yes")
        assert code == 3 and "execution runtime is halted" in text and world.transport.calls == []

    def test_an_ambiguous_result_exits_4(self, tmp_path) -> None:
        world = World(tmp_path, transport=FakeTransport(TransportResult(outcome=TransportOutcome.UNKNOWN)))
        code, text = self._run(world, "--apply", "--plan", plan_for().plan_id, "--yes")
        assert code == 4 and "Check the lock on Chaster" in text

    def test_every_outcome_has_a_description(self) -> None:
        from cli.main import _REVIEW_TEXT
        assert set(_REVIEW_TEXT) == {o.value for o in O}

    def test_owner_id_is_required(self, w) -> None:
        with pytest.raises(SystemExit):
            main(["extension-review", LOCK], out=io.StringIO(), review_factory=lambda: w.service)

    def test_render_without_a_plan(self) -> None:
        from chaster_execution.extension_review import ExtensionReviewResult
        text = render_review(ExtensionReviewResult(outcome=O.NOT_OWNER, audit_id="a1"), lock_id=LOCK, owner_id=OWNER)
        assert text == "Refused: that identity is not the owner.  [outcome=not_owner audit=a1]"


# ---------------------------------------------------------------------------------------------------------------- isolation

class TestIsolation:
    def test_only_the_cli_and_tests_import_the_review_service_or_the_plan(self) -> None:
        offenders = []
        for path in ROOT.rglob("*.py"):
            rel = path.relative_to(ROOT)
            if rel.parts[0] in {"cli", "tests"} or rel.parts[0].startswith(".") or "__pycache__" in rel.parts:
                continue
            if rel.as_posix() in ("chaster_execution/extension_review.py", "system/review_consent_dm.py"):
                continue                       # the service itself, and the owner's DM consent facade (guarded below)
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                    [node.module] if isinstance(node, ast.ImportFrom) and node.module else []
                offenders += [f"{rel}: {n}" for n in names if n.endswith(("extension_review", "extension_plan"))]
        assert offenders == []

    def test_the_dm_consent_facade_can_never_reach_the_forward_write(self) -> None:
        """Phase J1: the bot's only door to the service may show consent and record decisions (whose revocation reverts); it never
        calls `apply` or `preview` itself."""
        text = (ROOT / "system" / "review_consent_dm.py").read_text(encoding="utf-8")
        calls = {n.func.attr for n in ast.walk(ast.parse(text)) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        assert "decide_consent" in calls and not calls & {"apply", "preview", "set_extensions"}

    def test_the_composition_never_wires_extension_writes(self) -> None:
        for name in ("system/composition.py", "system/tenant_factory.py", "bot/discord_bot.py"):
            text = (ROOT / name).read_text(encoding="utf-8")
            assert "set_extensions" not in text and "ExtensionReviewService" not in text

    def test_migration_042(self, w) -> None:
        with w.core.transaction() as tx:
            assert tx.fetch_one("SELECT version FROM schema_version WHERE version = 42")
