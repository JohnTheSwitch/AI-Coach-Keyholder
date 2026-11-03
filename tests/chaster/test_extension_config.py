"""v0.39 Phase I1: role-agnostic lock discovery, the read-only lock configuration fetcher and the `extension-config` CLI. No network."""

from __future__ import annotations

import copy
import io
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from chaster.extension_config import (
    TARGET_VISIBILITY, VISIBILITY_VALUES, LockConfigurationReader, Punishment, describe_review, parse_lock_configuration, review_gaps,
)
from chaster.lock_client import ChasterLockApiError, ChasterLockClient
from chaster.lock_directory import LockDirectory
from chaster.managed_locks import LockRole
from chaster.token_manager import ChasterNotConnectedError, ChasterReauthorizationRequiredError
from cli.main import discovery_user_ids, main, render_configuration

NOW = datetime(2026, 11, 3, 14, 0, tzinfo=timezone.utc)


def _grant(resource, wearer, keyholder, category="extensions"):
    return {"resource": resource, "category": category, "labelKey": "x", "subjects": {"wearer": wearer, "keyholder": keyholder}}


def _ext(slug, ext_id, config, **extra):
    return {"_id": ext_id, "slug": slug, "mode": "non_cumulative", "regularity": 3600, "config": config, "displayName": slug,
            "userData": {"currentVerificationCode": "SECRET-CODE", "history": []}, **extra}


# Shapes as seen live on 2026-11-03 (values are invented).
WEARER_LOCK = {
    "_id": "6ae98ca410556a82f4b0d1aa", "role": "wearer", "status": "locked", "isTestLock": True,
    "combination": "SECRET-COMBINATION", "offerToken": "SECRET-OFFER", "sharedLinkToken": "SECRET-LINK",
    "permissions": {"preset": "full_trust", "grants": [
        _grant("lock.time.add", ["edit"], ["edit"], "time"),
        _grant("lock.extensions.edit", ["edit"], ["edit"])]},
    "extensions": [
        _ext("pillory", "e-pil", {"timeToAdd": 3600, "limitToLoggedUsers": True}),
        _ext("tasks", "e-tasks", {"tasks": [{"task": "SECRET TASK TEXT", "points": 0, "verificationRequired": True, "codeRequired": True}],
                                  "peerVerification": {"enabled": True, "punishments": [], "visibility": "keyholder"}}),
        _ext("verification-picture", "e-vp", {"visibility": "all", "peerVerification": {"enabled": False, "punishments": []}}),
    ],
}
KEYHOLDER_LOCK = {
    "_id": "6aca95cdc5ee0f6d4989cd25", "role": "keyholder", "status": "locked", "isTestLock": True, "combination": "SECRET-COMBINATION",
    "permissions": {"preset": "trusted_keyholder", "grants": [_grant("lock.extensions.edit", [], ["edit"])]},
    "extensions": [
        _ext("tasks", "k-tasks", {"tasks": [], "peerVerification": {"enabled": True, "visibility": "keyholder",
                                                                    "punishments": [{"name": "add_time", "params": 3600}]}}),
        _ext("verification-picture", "k-vp", {"visibility": "keyholder",
                                              "peerVerification": {"enabled": True, "punishments": [], "visibility": "keyholder"}}),
    ],
}


def _target(lock):
    lock = copy.deepcopy(lock)
    for e in lock["extensions"]:
        if e["slug"] in ("tasks", "verification-picture"):
            e["config"]["peerVerification"] = {"enabled": True, "punishments": [], "visibility": "all_members"}
    return lock


class TestGetLock:
    def _resp(self, body, status=200):
        r = MagicMock(status_code=status)
        r.json.return_value = body
        return r

    def test_gets_the_documented_endpoint(self) -> None:
        with patch("requests.Session.send", return_value=self._resp(WEARER_LOCK)) as send:
            body = ChasterLockClient().get_lock(access_token="tok", lock_id="6ae98ca410556a82f4b0d1aa")
        prepared = send.call_args.args[0]
        assert prepared.method == "GET" and prepared.url == "https://api.chaster.app/locks/6ae98ca410556a82f4b0d1aa"
        assert prepared.body is None and prepared.headers["Authorization"] == "Bearer tok" and body == WEARER_LOCK

    @pytest.mark.parametrize("status", [401, 403, 404, 500])
    def test_error_statuses_raise_with_the_status(self, status) -> None:
        with patch("requests.Session.send", return_value=self._resp({}, status)):
            with pytest.raises(ChasterLockApiError) as e:
                ChasterLockClient().get_lock(access_token="tok", lock_id="abc")
        assert e.value.status_code == status and "tok" not in str(e.value)

    @pytest.mark.parametrize("body", [[], "x", None])
    def test_a_non_object_body_raises(self, body) -> None:
        with patch("requests.Session.send", return_value=self._resp(body)):
            with pytest.raises(ChasterLockApiError):
                ChasterLockClient().get_lock(access_token="tok", lock_id="abc")

    def test_an_unparseable_body_and_a_network_error_raise(self) -> None:
        import requests
        bad = MagicMock(status_code=200)
        bad.json.side_effect = ValueError("no json")
        for kwargs in ({"return_value": bad}, {"side_effect": requests.ConnectionError("boom")}):
            with patch("requests.Session.send", **kwargs):
                with pytest.raises(ChasterLockApiError):
                    ChasterLockClient().get_lock(access_token="tok", lock_id="abc")

    @pytest.mark.parametrize("lock_id", ["", "../locks", "a/b", "a b", "x" * 65, None, 5])
    def test_lock_ids_that_could_change_the_path_are_refused_before_any_request(self, lock_id) -> None:
        with patch("requests.Session.send") as send:
            with pytest.raises(ValueError):
                ChasterLockClient().get_lock(access_token="tok", lock_id=lock_id)
        send.assert_not_called()


class TestParse:
    def test_wearer_lock(self) -> None:
        c = parse_lock_configuration(WEARER_LOCK)
        assert c.lock_id == "6ae98ca410556a82f4b0d1aa" and c.role is LockRole.WEARER and c.status == "locked" and c.is_test_lock is True
        assert c.edit_subjects == {"wearer", "keyholder"} and c.can_edit_extensions
        assert [e.slug for e in c.extensions] == ["pillory", "tasks", "verification-picture"]
        tasks = c.extension("tasks")
        assert tasks.extension_id == "e-tasks" and tasks.mode == "non_cumulative" and tasks.regularity == 3600
        assert tasks.peer_verification.enabled is True and tasks.peer_verification.visibility == "keyholder"
        assert tasks.peer_verification.punishments == ()
        vp = c.extension("verification-picture").peer_verification
        assert vp.enabled is False and vp.visibility is None                     # the picture's own `visibility` is a different field
        assert c.extension("pillory").peer_verification is None

    def test_keyholder_lock(self) -> None:
        c = parse_lock_configuration(KEYHOLDER_LOCK)
        assert c.role is LockRole.KEYHOLDER and c.edit_subjects == {"keyholder"} and c.can_edit_extensions
        assert c.extension("tasks").peer_verification.punishments == (Punishment(name="add_time", seconds=3600),)

    def test_edit_permission_is_per_role(self) -> None:
        lock = copy.deepcopy(KEYHOLDER_LOCK)
        lock["role"] = "wearer"                                            # the wearer of a trusted_keyholder lock may not edit
        assert not parse_lock_configuration(lock).can_edit_extensions
        lock["role"] = "visitor"
        c = parse_lock_configuration(lock)
        assert c.role is None and not c.can_edit_extensions

    @pytest.mark.parametrize("permissions", [None, "x", {}, {"grants": "x"}, {"grants": [{"resource": "lock.extensions.edit", "subjects": "x"}]},
                                             {"grants": [{"resource": "lock.extensions.edit", "subjects": {"keyholder": "edit"}}]}])
    def test_missing_or_odd_permissions_mean_nobody_may_edit(self, permissions) -> None:
        lock = dict(KEYHOLDER_LOCK, permissions=permissions)
        assert parse_lock_configuration(lock).edit_subjects == frozenset()

    def test_the_full_config_is_kept_as_an_independent_copy_for_a_later_write(self) -> None:
        raw = copy.deepcopy(WEARER_LOCK)
        c = parse_lock_configuration(raw)
        raw["extensions"][1]["config"]["tasks"].clear()
        assert c.extension("tasks").config["tasks"][0]["task"] == "SECRET TASK TEXT"

    def test_secrets_user_data_and_task_text_never_reach_repr(self) -> None:
        text = repr(parse_lock_configuration(WEARER_LOCK)) + repr(parse_lock_configuration(KEYHOLDER_LOCK))
        assert "SECRET" not in text and "userData" not in text

    @pytest.mark.parametrize("raw", [None, [], "x", {}, {"_id": ""}, {"_id": 5}])
    def test_things_that_are_not_locks_are_dropped(self, raw) -> None:
        assert parse_lock_configuration(raw) is None

    def test_ill_typed_extension_data_never_raises(self) -> None:
        lock = {"_id": "a", "role": "wearer", "status": 3, "extensions": [
            "garbage", {"slug": ""}, {"slug": "tasks", "config": "x", "regularity": True},
            {"slug": "verification-picture", "config": {"peerVerification": {"enabled": "yes", "visibility": 1,
                                                                             "punishments": ["x", {"name": "freeze"}, {"name": "add_time", "params": "1h"}]}}}]}
        c = parse_lock_configuration(lock)
        assert c.status is None and [e.slug for e in c.extensions] == ["tasks", "verification-picture"]
        assert c.extension("tasks").config == {} and c.extension("tasks").regularity is None
        pv = c.extension("verification-picture").peer_verification
        assert pv.enabled is None and pv.visibility is None
        assert pv.punishments == (Punishment(name="freeze", seconds=None), Punishment(name="add_time", seconds=None))
        assert parse_lock_configuration({"_id": "a", "extensions": "x"}).extensions == ()


class TestReviewGaps:
    def test_the_flawed_configurations_are_listed(self) -> None:
        assert review_gaps(parse_lock_configuration(WEARER_LOCK)) == (
            "tasks: visibility keyholder (target all_members)",
            "verification-picture: peer verification off",
            "verification-picture: visibility unset (target all_members)")
        assert review_gaps(parse_lock_configuration(KEYHOLDER_LOCK)) == (
            "tasks: visibility keyholder (target all_members)",
            "tasks: punishments add_time(3600) (target none)",
            "verification-picture: visibility keyholder (target all_members)")

    def test_a_lock_on_target_has_no_gaps(self) -> None:
        assert review_gaps(parse_lock_configuration(_target(WEARER_LOCK))) == ()
        assert review_gaps(parse_lock_configuration(_target(KEYHOLDER_LOCK))) == ()

    def test_missing_extensions_and_settings(self) -> None:
        lock = {"_id": "a", "role": "keyholder", "extensions": [_ext("tasks", "t", {})]}
        assert review_gaps(parse_lock_configuration(lock)) == (
            "tasks: peer verification off", "tasks: visibility unset (target all_members)", "verification-picture: extension missing")

    def test_the_target_is_a_documented_visibility_value(self) -> None:
        assert TARGET_VISIBILITY in VISIBILITY_VALUES and "community" not in VISIBILITY_VALUES

    def test_describe_review(self) -> None:
        c = parse_lock_configuration(KEYHOLDER_LOCK)
        assert describe_review(c.extension("tasks")) == "peer verification on, visibility keyholder, punishments add_time(3600)"
        assert describe_review(None) == "not on the lock"
        assert describe_review(parse_lock_configuration(WEARER_LOCK).extension("pillory")) == "no peerVerification settings"


class FakeTokens:
    def __init__(self, raises=None) -> None:
        self.raises, self.calls = raises or {}, []

    def call_with_token(self, user_id, *, now, operation):
        self.calls.append(user_id)
        if user_id in self.raises:
            raise self.raises[user_id]
        return operation(f"token-{user_id}")


class FakeClient:
    """Only reads exist here: a write method would show up as an AttributeError in these tests."""

    def __init__(self, locks=None, errors=None) -> None:
        self.locks = {l["_id"]: l for l in (locks or [WEARER_LOCK, KEYHOLDER_LOCK])}
        self.errors, self.calls = errors or {}, []

    def list_active_locks(self, *, access_token):
        self.calls.append(("wearer-list", access_token))
        return [l for l in self.locks.values() if l.get("role") == "wearer"]

    def search_keyholder_locks(self, *, access_token, status):
        self.calls.append(("keyholder-search", access_token))
        return [l for l in self.locks.values() if l.get("role") == "keyholder"]

    def get_lock(self, *, access_token, lock_id):
        self.calls.append(("get", lock_id))
        if lock_id in self.errors:
            raise self.errors[lock_id]
        return self.locks[lock_id]


class FakeConnections:
    def __init__(self, scopes) -> None:
        self.scopes = scopes

    def get(self, user_id):
        s = self.scopes.get(user_id)
        return None if s is None else SimpleNamespace(granted_scopes=s)


class TestDualRoleDiscovery:
    def _directory(self, scopes=None, client=None):
        client = client or FakeClient()
        return LockDirectory(token_manager=FakeTokens(), lock_client=client,
                             connections=FakeConnections(scopes or {"me": ("locks", "keyholder")})), client

    def test_one_account_finds_the_lock_it_wears_and_the_lock_it_holds(self) -> None:
        d, client = self._directory()
        listing = d.list_account_locks(user_id="me", now=NOW)
        assert [l.lock_id for l in listing.wearer_locks] == ["6ae98ca410556a82f4b0d1aa"]
        assert [l.lock_id for l in listing.keyholder_locks] == ["6aca95cdc5ee0f6d4989cd25"]
        assert listing.wearer_locks[0].role is LockRole.WEARER and listing.keyholder_locks[0].role is LockRole.KEYHOLDER
        assert listing.problems == [] and client.calls == [("wearer-list", "token-me"), ("keyholder-search", "token-me")]

    def test_without_the_keyholder_scope_the_worn_lock_is_still_found(self) -> None:
        d, client = self._directory(scopes={"me": ("locks",)})
        listing = d.list_account_locks(user_id="me", now=NOW)
        assert len(listing.wearer_locks) == 1 and listing.keyholder_locks == ()
        assert listing.problems == ["keyholder: missing_scope:keyholder"]

    def test_no_account(self) -> None:
        d, _ = self._directory()
        assert d.list_account_locks(user_id=None, now=NOW).problems == ["wearer: not_configured", "keyholder: not_configured"]

    def test_cli_default_uses_the_connected_account_for_both_roles(self) -> None:
        assert discovery_user_ids(None, "kh") == ("kh", "kh")
        assert discovery_user_ids("w", "kh") == ("w", "kh")                # an explicit wearer connection still wins
        assert discovery_user_ids(None, None) == (None, None)


class TestReader:
    def _reader(self, client=None, raises=None):
        client = client or FakeClient()
        return LockConfigurationReader(token_manager=FakeTokens(raises), lock_client=client), client

    def test_reads_each_lock_once_through_the_account(self) -> None:
        reader, client = self._reader()
        ids = ["6ae98ca410556a82f4b0d1aa", "6aca95cdc5ee0f6d4989cd25", "6ae98ca410556a82f4b0d1aa"]
        report = reader.read(user_id="me", lock_ids=ids, now=NOW)
        assert [c.lock_id for c in report.configurations] == ids[:2] and report.problems == []
        assert client.calls == [("get", ids[0]), ("get", ids[1])]

    def test_failures_are_short_codes_per_lock_and_do_not_hide_the_others(self) -> None:
        client = FakeClient(errors={"6ae98ca410556a82f4b0d1aa": ChasterLockApiError("x", status_code=403)})
        reader, _ = self._reader(client)
        report = reader.read(user_id="me", lock_ids=["6ae98ca410556a82f4b0d1aa", "6aca95cdc5ee0f6d4989cd25"], now=NOW)
        assert report.problems == ["6ae98ca410556a82f4b0d1aa: api_error:403"] and len(report.configurations) == 1
        for exc, code in ((ChasterNotConnectedError("x"), "not_connected"), (ChasterReauthorizationRequiredError("x"), "reauthorization_required"),
                          (RuntimeError("secret detail"), "failed")):
            reader, _ = self._reader(raises={"me": exc})
            assert reader.read(user_id="me", lock_ids=["a"], now=NOW).problems == [f"a: {code}"]

    def test_invalid_ids_and_unparseable_bodies(self) -> None:
        client = FakeClient(locks=[{"_id": "odd", "role": "wearer"}])
        client.locks["odd"] = ["not", "a", "lock"]
        reader = LockConfigurationReader(token_manager=FakeTokens(), lock_client=ChasterLockClient())
        with patch("requests.Session.send") as send:
            assert reader.read(user_id="me", lock_ids=["../x"], now=NOW).problems == ["../x: invalid_lock_id"]
        send.assert_not_called()
        reader, _ = self._reader(client)
        assert reader.read(user_id="me", lock_ids=["odd"], now=NOW).problems == ["odd: unparseable"]

    def test_logs_the_review_settings_and_nothing_private(self, caplog) -> None:
        reader, _ = self._reader()
        with caplog.at_level(logging.INFO, logger="ai_coach_keyholder.chaster.extension_config"):
            reader.read(user_id="me", lock_ids=["6ae98ca410556a82f4b0d1aa", "6aca95cdc5ee0f6d4989cd25"], now=NOW)
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "Lock 6aca95cdc5ee0f6d4989cd25 tasks: peer verification on, visibility keyholder, punishments add_time(3600)." in text
        assert "Lock 6ae98ca410556a82f4b0d1aa verification-picture: peer verification off, visibility unset, punishments none." in text
        assert "role wearer, may edit extensions: yes" in text
        assert "SECRET" not in text and "token-" not in text


class TestCli:
    def _run(self, client=None, scopes=None, user_id="me"):
        client = client or FakeClient()
        tokens = FakeTokens()
        directory = LockDirectory(token_manager=tokens, lock_client=client,
                                  connections=FakeConnections(scopes or {"me": ("locks", "keyholder")}))
        reader = LockConfigurationReader(token_manager=tokens, lock_client=client)
        out = io.StringIO()
        code = main(["extension-config"], out=out, config_factory=lambda: (directory, reader, user_id), now=lambda: NOW)
        return code, out.getvalue(), client

    def test_both_roles_and_their_flawed_review_settings(self) -> None:
        code, text, client = self._run()
        assert code == 0 and "ACCOUNT LOCKS: 1 worn, 1 held as keyholder" in text
        wearer, keyholder = text.split("LOCK 6aca95cdc5ee0f6d4989cd25")
        assert "LOCK 6ae98ca410556a82f4b0d1aa  role wearer" in wearer and "role keyholder" in keyholder
        assert "may edit extensions: yes (lock.extensions.edit granted to: keyholder, wearer)" in wearer
        assert "may edit extensions: yes (lock.extensions.edit granted to: keyholder)" in keyholder
        assert "- tasks: punishments add_time(3600) (target none)" in keyholder and "add_time" not in wearer
        assert "- verification-picture: peer verification off" in wearer
        assert {kind for kind, _ in client.calls} == {"wearer-list", "keyholder-search", "get"}       # reads only

    def test_a_lock_on_target_says_so(self) -> None:
        code, text, _ = self._run(client=FakeClient(locks=[_target(WEARER_LOCK)]))
        assert code == 0 and "matches the target" in text and "to change" not in text

    def test_a_lock_that_is_not_locked_is_out_of_scope(self) -> None:
        unlocked = {"_id": "6aba61d1efcf4bef4e0566df", "role": "wearer", "status": "unlocked", "extensions": [_ext("pillory", "p", {})]}
        code, text, _ = self._run(client=FakeClient(locks=[unlocked, KEYHOLDER_LOCK]))
        block = text.split("LOCK 6aca95cdc5ee0f6d4989cd25")[0]
        assert code == 0 and "status unlocked: out of scope, no changes planned" in block and "to change" not in block

    def test_secrets_never_appear_in_the_output(self) -> None:
        _, text, _ = self._run()
        assert "SECRET" not in text and "token-" not in text

    def test_problems_give_exit_code_2(self) -> None:
        client = FakeClient(errors={"6aca95cdc5ee0f6d4989cd25": ChasterLockApiError("x", status_code=500)})
        code, text, _ = self._run(client=client)
        assert code == 2 and "PROBLEMS" in text and "6aca95cdc5ee0f6d4989cd25: api_error:500" in text
        assert "LOCK 6ae98ca410556a82f4b0d1aa" in text
        code, text, _ = self._run(scopes={"me": ("locks",)})
        assert code == 2 and "keyholder: missing_scope:keyholder" in text and "LOCK 6ae98ca410556a82f4b0d1aa" in text

    def test_no_connected_account(self) -> None:
        code, text, client = self._run(user_id=None)
        assert code == 2 and "CHASTER_EXECUTION_KEYHOLDER_USER_ID" in text and client.calls == []

    def test_render_without_locks(self) -> None:
        from chaster.extension_config import ConfigurationReport
        from chaster.lock_directory import LockListing
        assert render_configuration(LockListing(), ConfigurationReport()) == "ACCOUNT LOCKS: 0 worn, 0 held as keyholder"


def test_nothing_in_phase_i1_can_write_to_chaster() -> None:
    """Static guard: the new module and the CLI command only read (no POST/PUT/PATCH/DELETE, no extension edit endpoint)."""
    import inspect

    import chaster.extension_config as module
    from chaster.lock_client import ChasterLockClient as Client
    source = inspect.getsource(module) + inspect.getsource(Client.get_lock)
    for verb in ('"POST"', '"PUT"', '"PATCH"', '"DELETE"', "requests.post", "requests.put", "requests.patch", "requests.delete"):
        assert verb not in source
    assert not any(name.startswith(("set_", "update_", "edit_", "post_")) for name in dir(Client))
