"""
ChasterHttpTransport tests. Every request goes to a fake `send` or a patched
`requests.Session.send`: no test ever touches the network. Token strings here
are obviously fake test values.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests
from cryptography.fernet import Fernet

from chaster.oauth_client import ChasterTokenExchangeError, ChasterTokenResponse
from chaster.repository import ChasterConnectionRepository
from chaster.token_encryptor import TokenEncryptor
from chaster.token_manager import ChasterTokenManager
from chaster_execution.models import TransportOutcome as O
from chaster_execution.transport import (
    ChasterHttpTransport,
    build_keyholder_transport,
)
from infrastructure.clock import FrozenClock
from infrastructure.database import Database as CoreDatabase

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).parent.parent.parent
FAKE_TOKEN = "fake-test-access-token-123"


class Resp:
    def __init__(self, status: int) -> None:
        self.status_code = status


class FakeSend:
    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, method, url, **kw):
        self.calls.append({"method": method, "url": url, **kw})
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return Resp(item) if isinstance(item, int) else item


@pytest.fixture
def parts(tmp_path: Path):
    core = CoreDatabase(tmp_path / "test.db")
    with core.raw_connection() as conn:
        for path in sorted((ROOT / "database" / "migrations").glob("*.sql")):
            conn.executescript(path.read_text(encoding="utf-8"))
        conn.execute("INSERT INTO user_accounts (id, created_at, last_seen_at) VALUES ('k', ?, ?)",
                     (NOW.isoformat(), NOW.isoformat()))
        conn.commit()
    conns = ChasterConnectionRepository(core.db_path, core=core, encryptor=TokenEncryptor(Fernet.generate_key()))
    conns.create_or_replace(
        user_id="k", chaster_account_id="ka", chaster_username="kh", access_token=FAKE_TOKEN, refresh_token="fake-refresh",
        access_token_expires_at=NOW + timedelta(hours=3), refresh_token_expires_at=NOW + timedelta(days=30),
        granted_scopes=("locks",), now=NOW,
    )
    oauth = MagicMock()
    return conns, oauth, ChasterTokenManager(connections=conns, oauth_client=oauth)


def transport(parts, send) -> ChasterHttpTransport:
    return ChasterHttpTransport(token_manager=parts[2], keyholder_user_id="k", clock=FrozenClock(NOW), send=send)


class TestRequestShape:
    def test_timeouts_are_set(self, parts) -> None:
        send = FakeSend(204)
        transport(parts, send).set_frozen(lock_id="abc", frozen=True)
        assert isinstance(send.calls[0]["timeout"], tuple)


class TestInputValidation:
    @pytest.mark.parametrize("lock_id", ["", " ", "a/b", "a?x=1", "../x", "a b", "a#b", None, 5, "x" * 65])
    def test_bad_lock_ids_never_send(self, parts, lock_id) -> None:
        send = FakeSend(204)
        r = transport(parts, send).set_frozen(lock_id=lock_id, frozen=True)
        assert r.outcome == O.NOT_SENT and not send.calls


class TestOutcomeMapping:
    @pytest.mark.parametrize("status,outcome", [
        (204, O.OK), (400, O.NOT_ELIGIBLE), (403, O.FORBIDDEN), (404, O.NOT_FOUND),
        (200, O.UNKNOWN), (201, O.UNKNOWN), (202, O.UNKNOWN), (429, O.UNKNOWN),
        (500, O.UNKNOWN), (502, O.UNKNOWN), (503, O.UNKNOWN), (418, O.UNKNOWN),
    ])
    def test_status_mapping(self, parts, status, outcome) -> None:
        r = transport(parts, FakeSend(status)).set_frozen(lock_id="abc", frozen=True)
        assert r.outcome == outcome and r.http_status == status

    def test_connect_timeout_is_not_sent(self, parts) -> None:
        assert transport(parts, FakeSend(requests.ConnectTimeout())).set_frozen(lock_id="abc", frozen=True).outcome == O.NOT_SENT

    @pytest.mark.parametrize("exc", [requests.ReadTimeout(), requests.ConnectionError(), requests.RequestException()])
    def test_other_network_errors_are_ambiguous(self, parts, exc) -> None:
        assert transport(parts, FakeSend(exc)).set_frozen(lock_id="abc", frozen=True).outcome == O.UNKNOWN

    def test_a_rejected_request_is_never_resent_for_ambiguous_errors(self, parts) -> None:
        send = FakeSend(500)
        transport(parts, send).set_frozen(lock_id="abc", frozen=True)
        assert len(send.calls) == 1


class TestAuthHandling:
    def test_no_keyholder_connection_is_not_sent(self, parts) -> None:
        send = FakeSend(204)
        t = ChasterHttpTransport(token_manager=parts[2], keyholder_user_id="nobody", clock=FrozenClock(NOW), send=send)
        assert t.set_frozen(lock_id="abc", frozen=True).outcome == O.NOT_SENT and not send.calls

    def test_a_401_refreshes_once_and_retries_with_the_new_token(self, parts) -> None:
        conns, oauth, _ = parts
        oauth.refresh_tokens.return_value = ChasterTokenResponse(
            access_token="fake-new-access-token", refresh_token="fake-new-refresh", expires_in=3600,
            refresh_expires_in=86400, granted_scopes=("locks",),
        )
        send = FakeSend(401, 204)
        r = transport(parts, send).set_frozen(lock_id="abc", frozen=True)
        assert r.outcome == O.OK and len(send.calls) == 2
        assert send.calls[0]["headers"]["Authorization"] == f"Bearer {FAKE_TOKEN}"
        assert send.calls[1]["headers"]["Authorization"] == "Bearer fake-new-access-token"

    def test_a_repeated_401_requires_reauthorisation_and_does_not_loop(self, parts) -> None:
        oauth = parts[1]
        oauth.refresh_tokens.return_value = ChasterTokenResponse(
            access_token="fake-new-access-token", refresh_token="fake-new-refresh", expires_in=3600,
            refresh_expires_in=86400, granted_scopes=("locks",),
        )
        send = FakeSend(401)
        r = transport(parts, send).set_frozen(lock_id="abc", frozen=True)
        assert r.outcome == O.UNAUTHORIZED and len(send.calls) == 2

    def test_refresh_endpoint_down_is_not_sent(self, parts) -> None:
        oauth = parts[1]
        oauth.refresh_tokens.side_effect = ChasterTokenExchangeError("down", status_code=503, transient=True)
        send = FakeSend(401)
        r = transport(parts, send).set_frozen(lock_id="abc", frozen=True)
        assert r.outcome == O.NOT_SENT and len(send.calls) == 1

    def test_expired_token_is_refreshed_before_sending(self, parts) -> None:
        conns, oauth, mgr = parts
        late = ChasterHttpTransport(token_manager=mgr, keyholder_user_id="k",
                                    clock=FrozenClock(NOW + timedelta(hours=5)), send=FakeSend(204))
        oauth.refresh_tokens.return_value = ChasterTokenResponse(
            access_token="fake-new-access-token", refresh_token="fake-new-refresh", expires_in=3600,
            refresh_expires_in=86400, granted_scopes=("locks",),
        )
        assert late.set_frozen(lock_id="abc", frozen=True).outcome == O.OK
        assert late._send.calls[0]["headers"]["Authorization"] == "Bearer fake-new-access-token"


class TestSecrets:
    def test_token_is_never_logged_returned_or_in_the_result(self, parts, caplog) -> None:
        caplog.set_level(logging.DEBUG)
        for status in (204, 403, 500):
            r = transport(parts, FakeSend(status)).set_frozen(lock_id="abc", frozen=True)
            assert FAKE_TOKEN not in repr(r)
        transport(parts, FakeSend(RuntimeError("boom"))).set_frozen(lock_id="abc", frozen=True)
        assert FAKE_TOKEN not in caplog.text

    def test_unexpected_error_is_ambiguous_not_a_crash(self, parts) -> None:
        r = transport(parts, FakeSend(ValueError("weird"))).set_frozen(lock_id="abc", frozen=True)
        assert r.outcome == O.UNKNOWN


class TestFactoryAndInterface:
    def test_factory_builds_the_real_transport(self, parts) -> None:
        t = build_keyholder_transport(token_manager=parts[2], keyholder_user_id="k", clock=FrozenClock(NOW))
        assert isinstance(t, ChasterHttpTransport)

    def test_constructor_requires_a_keyholder_id(self, parts) -> None:
        with pytest.raises(ValueError):
            ChasterHttpTransport(token_manager=parts[2], keyholder_user_id=" ", clock=FrozenClock(NOW))

    def test_public_surface_is_only_barrier_and_extension_methods(self, parts) -> None:
        public = {n for n in dir(ChasterHttpTransport) if not n.startswith("_")}
        assert public == {"set_frozen", "set_extensions"}       # set_extensions: Phase I2, CLI only

    def test_module_never_disables_tls_verification(self) -> None:
        text = (ROOT / "chaster_execution" / "transport.py").read_text(encoding="utf-8")
        assert "verify=False" not in text and "verify =" not in text


class TestSetFrozen:
    def test_unfreeze_posts_the_documented_body(self, parts) -> None:
        send = FakeSend(204)
        r = transport(parts, send).set_frozen(lock_id="64a1b2c3d4e5f60718293a4b", frozen=False)
        (c,) = send.calls
        assert r.outcome == O.OK and r.http_status == 204
        assert c["method"] == "POST" and c["url"] == "https://api.chaster.app/locks/64a1b2c3d4e5f60718293a4b/freeze"
        assert c["json"] == {"isFrozen": False} and c["headers"]["Authorization"] == f"Bearer {FAKE_TOKEN}"

    def test_freeze_posts_true(self, parts) -> None:
        send = FakeSend(204)
        transport(parts, send).set_frozen(lock_id="abc", frozen=True)
        assert send.calls[0]["json"] == {"isFrozen": True}

    @pytest.mark.parametrize("status,outcome", [
        (204, O.OK), (400, O.NOT_ELIGIBLE), (403, O.FORBIDDEN), (404, O.NOT_FOUND), (200, O.UNKNOWN), (500, O.UNKNOWN),
    ])
    def test_status_mapping(self, parts, status, outcome) -> None:
        assert transport(parts, FakeSend(status)).set_frozen(lock_id="abc", frozen=False).outcome == outcome

    @pytest.mark.parametrize("lock_id,frozen", [("", True), ("a/b", True), (None, True), ("abc", 1), ("abc", "true"), ("abc", None)])
    def test_bad_inputs_never_send(self, parts, lock_id, frozen) -> None:
        send = FakeSend(204)
        assert transport(parts, send).set_frozen(lock_id=lock_id, frozen=frozen).outcome == O.NOT_SENT
        assert not send.calls

    def test_ambiguous_network_error(self, parts) -> None:
        assert transport(parts, FakeSend(requests.ReadTimeout())).set_frozen(lock_id="abc", frozen=False).outcome == O.UNKNOWN

    def test_401_refreshes_once(self, parts) -> None:
        parts[1].refresh_tokens.return_value = ChasterTokenResponse(
            access_token="fake-new-access-token", refresh_token="fake-new-refresh", expires_in=3600,
            refresh_expires_in=86400, granted_scopes=("locks",),
        )
        send = FakeSend(401, 204)
        assert transport(parts, send).set_frozen(lock_id="abc", frozen=False).outcome == O.OK and len(send.calls) == 2

    def test_real_wire_format(self, parts, monkeypatch) -> None:
        captured = {}

        def fake_session_send(self, prepared, **kw):
            captured["p"] = prepared
            return Resp(204)

        monkeypatch.setattr(requests.Session, "send", fake_session_send)
        t = ChasterHttpTransport(token_manager=parts[2], keyholder_user_id="k", clock=FrozenClock(NOW))
        assert t.set_frozen(lock_id="abc123", frozen=False).outcome == O.OK
        p = captured["p"]
        assert p.url == "https://api.chaster.app/locks/abc123/freeze" and json.loads(p.body) == {"isFrozen": False}
        assert p.headers["Content-Type"] == "application/json" and list(p.headers.keys())[:2] == ["Authorization", "User-Agent"]

    def test_no_combination_or_unlock_endpoint_is_ever_referenced(self) -> None:
        text = (ROOT / "chaster_execution" / "transport.py").read_text(encoding="utf-8")
        code = text.split('"""', 2)[2]               # strip the module docstring
        for forbidden in ("/combination", "/unlock", "/emergency", "recover-combination"):
            assert forbidden not in code
