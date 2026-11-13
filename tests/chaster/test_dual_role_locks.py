"""v0.39 dual-role Phase A: role model, keyholder search client, lock directory and the read-only CLI. No network."""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest

from chaster.lock_client import KEYHOLDER_SEARCH_URL, ChasterLockApiError, ChasterLockClient
from chaster.lock_directory import LockDirectory
from chaster.managed_locks import LockRole, ManagedLock, parse_lock
from chaster.oauth_client import ChasterOAuthClient
from chaster.token_manager import ChasterNotConnectedError, ChasterReauthorizationRequiredError
from cli.main import main, render_listing

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)

KEYHOLDER_LOCK = {
    "_id": "kh-1", "title": "Weekend lock", "status": "locked", "isFrozen": True, "isTestLock": True,
    "user": {"_id": "u-wearer", "username": "wearer_name", "avatarUrl": "x"},
    "keyholder": {"_id": "u-bot", "username": "the_bot"},
    "combination": "SECRET-COMBINATION", "offerToken": "SECRET-OFFER", "sharedLinkToken": "SECRET-LINK",
}


class TestParse:
    def test_keyholder_lock_fields(self) -> None:
        lock = parse_lock(KEYHOLDER_LOCK, LockRole.KEYHOLDER)
        assert lock == ManagedLock(
            role=LockRole.KEYHOLDER, lock_id="kh-1", title="Weekend lock", status="locked", is_frozen=True, is_test_lock=True,
            wearer_chaster_id="u-wearer", wearer_username="wearer_name", keyholder_chaster_id="u-bot", keyholder_username="the_bot")

    def test_secrets_never_reach_the_value(self) -> None:
        lock = parse_lock(KEYHOLDER_LOCK, LockRole.KEYHOLDER)
        text = repr(lock) + json.dumps(lock.__dict__, default=str)
        assert "SECRET" not in text and not any("combination" in f or "token" in f.lower() for f in lock.__dict__)

    @pytest.mark.parametrize("raw", [None, [], "x", {}, {"_id": ""}, {"_id": 5}, {"title": "no id"}])
    def test_things_that_are_not_locks_are_dropped(self, raw) -> None:
        assert parse_lock(raw, LockRole.WEARER) is None

    def test_ill_typed_fields_become_none(self) -> None:
        lock = parse_lock({"_id": "a", "title": 3, "isFrozen": "yes", "user": "bad", "status": None}, LockRole.WEARER)
        assert lock.title is None and lock.is_frozen is None and lock.wearer_chaster_id is None and lock.status is None
        assert lock.role is LockRole.WEARER


def _resp(body, status=201):
    r = MagicMock(status_code=status)
    r.json.return_value = body
    return r


class TestSearchKeyholderLocks:
    def test_posts_the_documented_search_with_scope_free_body(self) -> None:
        with patch("requests.Session.send", return_value=_resp({"pages": 1, "total": 1, "locks": [KEYHOLDER_LOCK]})) as send:
            out = ChasterLockClient().search_keyholder_locks(access_token="tok")
        prepared = send.call_args.args[0]
        assert prepared.method == "POST" and prepared.url == KEYHOLDER_SEARCH_URL == "https://api.chaster.app/keyholder/locks/search"
        assert json.loads(prepared.body) == {"status": "locked", "page": 0, "limit": 50}
        assert prepared.headers["Authorization"] == "Bearer tok" and out == [KEYHOLDER_LOCK]

    def test_reads_every_page(self) -> None:
        pages = [_resp({"pages": 3, "total": 3, "locks": [{"_id": f"l{i}"}]}) for i in range(3)]
        with patch("requests.Session.send", side_effect=pages) as send:
            out = ChasterLockClient().search_keyholder_locks(access_token="tok")
        assert [x["_id"] for x in out] == ["l0", "l1", "l2"] and send.call_count == 3
        assert [json.loads(c.args[0].body)["page"] for c in send.call_args_list] == [0, 1, 2]

    def test_accepts_200_as_well_as_201(self) -> None:
        with patch("requests.Session.send", return_value=_resp({"pages": 1, "total": 0, "locks": []}, status=200)):
            assert ChasterLockClient().search_keyholder_locks(access_token="tok") == []

    @pytest.mark.parametrize("status", [400, 401, 403, 500])
    def test_error_statuses_raise_with_the_status(self, status) -> None:
        with patch("requests.Session.send", return_value=_resp({}, status=status)):
            with pytest.raises(ChasterLockApiError) as e:
                ChasterLockClient().search_keyholder_locks(access_token="tok")
        assert e.value.status_code == status and "tok" not in str(e.value)

    @pytest.mark.parametrize("body", [[], {"locks": "x", "pages": 1}, {"locks": [], "pages": "1"}, {"pages": 1}])
    def test_unexpected_shapes_raise(self, body) -> None:
        with patch("requests.Session.send", return_value=_resp(body)):
            with pytest.raises(ChasterLockApiError):
                ChasterLockClient().search_keyholder_locks(access_token="tok")

    def test_a_runaway_page_count_is_bounded(self) -> None:
        with patch("requests.Session.send", return_value=_resp({"pages": 9999, "total": 1, "locks": [{"_id": "x"}]})) as send:
            with pytest.raises(ChasterLockApiError):
                ChasterLockClient().search_keyholder_locks(access_token="tok")
        assert send.call_count == 20

    def test_input_validation(self) -> None:
        with pytest.raises(ValueError):
            ChasterLockClient().search_keyholder_locks(access_token=" ")
        with pytest.raises(ValueError):
            ChasterLockClient().search_keyholder_locks(access_token="t", status="everything")

    def test_a_network_error_is_wrapped(self) -> None:
        import requests
        with patch("requests.Session.send", side_effect=requests.ConnectionError("boom")):
            with pytest.raises(ChasterLockApiError):
                ChasterLockClient().search_keyholder_locks(access_token="tok")


class FakeTokens:
    def __init__(self, raises=None) -> None:
        self.raises, self.calls = raises, []

    def call_with_token(self, user_id, *, now, operation):
        self.calls.append(user_id)
        if self.raises and user_id in self.raises:
            raise self.raises[user_id]
        return operation(f"token-{user_id}")


class FakeConnections:
    def __init__(self, scopes) -> None:
        self.scopes = scopes

    def get(self, user_id):
        s = self.scopes.get(user_id)
        return None if s is None else SimpleNamespace(granted_scopes=s)


class FakeClient:
    def __init__(self) -> None:
        self.wearer_tokens, self.keyholder_tokens = [], []

    def list_active_locks(self, *, access_token):
        self.wearer_tokens.append(access_token)
        return [{"_id": "w-1", "title": "My lock", "status": "locked", "isFrozen": False,
                 "user": {"_id": "me", "username": "me_name"}, "combination": "SECRET"}]

    def search_keyholder_locks(self, *, access_token, status):
        self.keyholder_tokens.append(access_token)
        return [KEYHOLDER_LOCK, "garbage"]


def directory(scopes=None, raises=None):
    scopes = scopes if scopes is not None else {"w": ("locks",), "k": ("locks", "keyholder")}
    client = FakeClient()
    return LockDirectory(token_manager=FakeTokens(raises), lock_client=client, connections=FakeConnections(scopes)), client


class TestDirectory:
    def test_both_roles_are_listed_and_categorised(self) -> None:
        d, client = directory()
        listing = d.list_locks(wearer_user_id="w", keyholder_user_id="k", now=NOW)
        assert [l.lock_id for l in listing.wearer_locks] == ["w-1"] and listing.wearer_locks[0].role is LockRole.WEARER
        assert [l.lock_id for l in listing.keyholder_locks] == ["kh-1"] and listing.keyholder_locks[0].role is LockRole.KEYHOLDER
        assert listing.problems == [] and client.wearer_tokens == ["token-w"] and client.keyholder_tokens == ["token-k"]

    def test_each_side_uses_its_own_connection(self) -> None:
        d, client = directory()
        d.list_locks(wearer_user_id="w", keyholder_user_id="k", now=NOW)
        assert "token-k" not in client.wearer_tokens and "token-w" not in client.keyholder_tokens

    def test_a_missing_keyholder_scope_keeps_the_self_locks(self) -> None:
        d, client = directory(scopes={"w": ("locks",), "k": ("locks",)})
        listing = d.list_locks(wearer_user_id="w", keyholder_user_id="k", now=NOW)
        assert len(listing.wearer_locks) == 1 and listing.keyholder_locks == ()
        assert listing.problems == ["keyholder: missing_scope:keyholder"] and client.keyholder_tokens == []

    def test_unconfigured_and_unconnected_sides(self) -> None:
        d, _ = directory(scopes={"w": ("locks",)})
        listing = d.list_locks(wearer_user_id="w", keyholder_user_id=None, now=NOW)
        assert listing.problems == ["keyholder: not_configured"]
        listing = d.list_locks(wearer_user_id=None, keyholder_user_id="k", now=NOW)
        assert listing.problems == ["wearer: not_configured", "keyholder: not_connected"]

    def test_provider_failures_become_short_codes_and_do_not_hide_the_other_side(self) -> None:
        d, _ = directory(raises={"k": ChasterLockApiError("x", status_code=403)})
        listing = d.list_locks(wearer_user_id="w", keyholder_user_id="k", now=NOW)
        assert listing.problems == ["keyholder: api_error:403"] and len(listing.wearer_locks) == 1
        d, _ = directory(raises={"w": ChasterReauthorizationRequiredError("x"), "k": ChasterNotConnectedError("x")})
        listing = d.list_locks(wearer_user_id="w", keyholder_user_id="k", now=NOW)
        assert listing.problems == ["wearer: reauthorization_required", "keyholder: not_connected"]
        d, _ = directory(raises={"w": RuntimeError("secret detail")})
        listing = d.list_locks(wearer_user_id="w", keyholder_user_id="k", now=NOW)
        assert listing.problems == ["wearer: failed"] and "secret" not in " ".join(listing.problems)


class TestCli:
    def _run(self, **kw):
        d, _ = directory(**kw)
        out = io.StringIO()
        code = main(["list-locks"], out=out, directory_factory=lambda: (d, "w", "k"), now=lambda: NOW)
        return code, out.getvalue()

    def test_two_categorised_tables_with_all_columns(self) -> None:
        code, text = self._run()
        assert code == 0
        assert text.index("SELF-LOCKS (wearer)") < text.index("w-1") < text.index("MANAGED LOCKS (keyholder)") < text.index("kh-1")
        kh_line = next(l for l in text.splitlines() if l.startswith("kh-1"))
        for part in ("Weekend lock", "wearer_name", "u-wearer", "locked", "yes"):
            assert part in kh_line
        assert "isFrozen" in text and "PROBLEMS" not in text

    def test_secrets_never_appear_in_the_output(self) -> None:
        _, text = self._run()
        assert "SECRET" not in text and "token-" not in text

    def test_problems_give_exit_code_2_and_a_scope_hint(self) -> None:
        code, text = self._run(scopes={"w": ("locks",), "k": ("locks",)})
        assert code == 2 and "PROBLEMS" in text and "missing_scope:keyholder" in text and "CHASTER_OAUTH_SCOPES" in text
        assert "w-1" in text                                  # the self-locks are still shown

    def test_empty_tables_render(self) -> None:
        from chaster.lock_directory import LockListing
        text = render_listing(LockListing())
        assert "SELF-LOCKS (wearer)" in text and "MANAGED LOCKS (keyholder)" in text

    def test_there_is_no_write_command(self) -> None:
        with pytest.raises(SystemExit):
            main(["freeze-lock", "x"], out=io.StringIO(), directory_factory=lambda: (None, None, None))


class TestOauthScopes:
    def _client(self, **kw):
        return ChasterOAuthClient(client_id="c", client_secret="s", redirect_uri="http://localhost/cb", **kw)

    def _scope(self, url):
        return parse_qs(urlparse(url).query)["scope"][0]

    # Live, 2026-11-13: a new Chaster app accepts only the split scopes; the plain names are split when requested.
    def test_default_is_locks_only(self) -> None:
        assert self._scope(self._client().build_authorization_url(state="s")) == "locks:read locks:write"

    def test_configured_scopes_are_requested(self) -> None:
        c = self._client(scopes=("locks", "keyholder", "messaging", "profile", "offline_access"))
        assert self._scope(c.build_authorization_url(state="s")) == ("locks:read locks:write keyholder:read keyholder:write messaging:read "
                                                                      "messaging:write profile offline_access")
        assert self._scope(c.build_authorization_url(state="s", scopes=("locks",))) == "locks:read locks:write"
        assert self._scope(c.build_authorization_url(state="s", scopes=("locks:read",))) == "locks:read"

    def test_a_plain_name_comes_back_once_both_halves_are_granted(self) -> None:
        from chaster.oauth_client import effective_scopes
        granted = effective_scopes(["profile", "locks:read", "locks:write", "keyholder:read", "keyholder:write", "messaging:read", "offline_access"])
        assert "locks" in granted and "keyholder" in granted and "messaging" not in granted and "messaging:read" in granted
        assert effective_scopes(["locks", "keyholder"]) == ("locks", "keyholder")          # an older app's plain grant stays as it was

    def test_invalid_scopes_are_refused(self) -> None:
        for bad in ((), ("",)):
            with pytest.raises(ValueError):
                self._client(scopes=bad)

    def test_config_default(self) -> None:
        from core.config import Config
        assert Config.__dataclass_fields__["chaster_oauth_scopes"].default == "locks"
