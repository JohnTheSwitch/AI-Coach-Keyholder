"""
tests/chaster/test_oauth_client.py

All Chaster HTTP traffic is mocked -- no real network calls, no real
Chaster credentials required. Since every real request now goes
through `chaster._http.send_with_ordered_headers()` (a
`requests.Session()` + `session.send(prepared, ...)` call, not the
module-level `requests.get()`/`requests.post()` shortcuts), these
tests patch `requests.Session.send` directly and inspect the actual
`PreparedRequest` object it receives -- exactly the level needed to
prove the real header order, not just the input dict, per the
project's own diagnostic history (see
docs/architecture/chaster_integration_technical_design.md).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs

import pytest

from chaster import CHASTER_USER_AGENT
from chaster.oauth_client import AUTHORIZATION_URL, TOKEN_URL, ChasterOAuthClient, ChasterTokenExchangeError


def _client() -> ChasterOAuthClient:
    return ChasterOAuthClient(client_id="test-client-id", client_secret="test-client-secret", redirect_uri="https://example.com/oauth/chaster/callback")


def _body_as_dict(prepared_request) -> dict:
    """Parses a PreparedRequest's URL-encoded form body back into a
    plain dict of single values, for assertions."""
    parsed = parse_qs(prepared_request.body)
    return {k: v[0] for k, v in parsed.items()}


class TestConstruction:
    def test_empty_client_id_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            ChasterOAuthClient(client_id="", client_secret="s", redirect_uri="https://example.com/cb")

    def test_empty_client_secret_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            ChasterOAuthClient(client_id="c", client_secret="", redirect_uri="https://example.com/cb")

    def test_empty_redirect_uri_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            ChasterOAuthClient(client_id="c", client_secret="s", redirect_uri="")


class TestAuthorizationUrl:
    def test_url_targets_the_confirmed_authorization_endpoint(self) -> None:
        url = _client().build_authorization_url(state="abc123")
        assert url.startswith(AUTHORIZATION_URL + "?")

    def test_url_includes_client_id_redirect_uri_and_state(self) -> None:
        url = _client().build_authorization_url(state="abc123")
        assert "client_id=test-client-id" in url
        assert "state=abc123" in url
        assert "response_type=code" in url

    def test_default_scope_is_locks_only(self) -> None:
        url = _client().build_authorization_url(state="abc123")
        assert "scope=locks" in url

    def test_empty_state_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            _client().build_authorization_url(state="")


class TestCodeExchange:
    def test_successful_exchange_returns_parsed_token_response(self) -> None:
        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = {
            "access_token": "real-access-token", "refresh_token": "real-refresh-token",
            "expires_in": 300, "refresh_expires_in": 1800, "scope": "locks",
        }
        with patch("requests.Session.send", return_value=mock_response) as mock_send:
            tokens = _client().exchange_code_for_tokens(code="real-auth-code")
        assert tokens.access_token == "real-access-token"
        assert tokens.refresh_token == "real-refresh-token"
        assert tokens.expires_in == 300
        assert tokens.refresh_expires_in == 1800
        assert tokens.granted_scopes == ("locks",)
        # Confirms it actually hit the confirmed Token URL, not a guessed one.
        assert mock_send.call_args.args[0].url == TOKEN_URL

    def test_exchange_sends_client_secret_and_code_in_the_request_body_not_the_url(self) -> None:
        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = {
            "access_token": "at", "refresh_token": "rt", "expires_in": 300, "refresh_expires_in": 1800, "scope": "locks",
        }
        with patch("requests.Session.send", return_value=mock_response) as mock_send:
            _client().exchange_code_for_tokens(code="real-auth-code")
        sent_data = _body_as_dict(mock_send.call_args.args[0])
        assert sent_data["client_secret"] == "test-client-secret"
        assert sent_data["code"] == "real-auth-code"
        assert sent_data["grant_type"] == "authorization_code"
        assert "client_secret" not in mock_send.call_args.args[0].url
        assert "real-auth-code" not in mock_send.call_args.args[0].url

    def test_non_200_response_raises_token_exchange_error(self) -> None:
        mock_response = MagicMock(status_code=400)
        with patch("requests.Session.send", return_value=mock_response):
            with pytest.raises(ChasterTokenExchangeError):
                _client().exchange_code_for_tokens(code="bad-code")

    def test_malformed_json_response_raises_token_exchange_error(self) -> None:
        mock_response = MagicMock(status_code=200)
        mock_response.json.side_effect = ValueError("not json")
        with patch("requests.Session.send", return_value=mock_response):
            with pytest.raises(ChasterTokenExchangeError):
                _client().exchange_code_for_tokens(code="real-code")

    def test_missing_expected_field_raises_token_exchange_error(self) -> None:
        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = {"access_token": "at"}  # missing refresh_token etc.
        with patch("requests.Session.send", return_value=mock_response):
            with pytest.raises(ChasterTokenExchangeError):
                _client().exchange_code_for_tokens(code="real-code")

    def test_network_error_raises_token_exchange_error(self) -> None:
        import requests
        with patch("requests.Session.send", side_effect=requests.ConnectionError("boom")):
            with pytest.raises(ChasterTokenExchangeError):
                _client().exchange_code_for_tokens(code="real-code")

    def test_empty_code_is_rejected_before_any_network_call(self) -> None:
        with patch("requests.Session.send") as mock_send:
            with pytest.raises(ValueError):
                _client().exchange_code_for_tokens(code="")
            mock_send.assert_not_called()

    def test_exchange_error_message_never_contains_the_code_or_secrets(self) -> None:
        mock_response = MagicMock(status_code=400)
        try:
            with patch("requests.Session.send", return_value=mock_response):
                _client().exchange_code_for_tokens(code="UNIQUE-SECRET-CODE-VALUE")
            pytest.fail("expected ChasterTokenExchangeError")
        except ChasterTokenExchangeError as exc:
            assert "UNIQUE-SECRET-CODE-VALUE" not in str(exc)
            assert "test-client-secret" not in str(exc)


class TestRefresh:
    def test_successful_refresh_returns_new_tokens(self) -> None:
        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = {
            "access_token": "new-access", "refresh_token": "new-refresh",
            "expires_in": 300, "refresh_expires_in": 1800, "scope": "locks",
        }
        with patch("requests.Session.send", return_value=mock_response):
            tokens = _client().refresh_tokens(refresh_token="old-refresh-token")
        assert tokens.access_token == "new-access"

    def test_failed_refresh_raises_token_exchange_error_without_leaking_the_refresh_token(self) -> None:
        mock_response = MagicMock(status_code=400)
        try:
            with patch("requests.Session.send", return_value=mock_response):
                _client().refresh_tokens(refresh_token="UNIQUE-REFRESH-TOKEN-VALUE")
            pytest.fail("expected ChasterTokenExchangeError")
        except ChasterTokenExchangeError as exc:
            assert "UNIQUE-REFRESH-TOKEN-VALUE" not in str(exc)


class TestTokenResponseReprRedaction:
    def test_repr_never_includes_raw_token_values(self) -> None:
        from chaster.oauth_client import ChasterTokenResponse
        tokens = ChasterTokenResponse(
            access_token="UNIQUE-SECRET-ACCESS", refresh_token="UNIQUE-SECRET-REFRESH",
            expires_in=300, refresh_expires_in=1800, granted_scopes=("locks",),
        )
        text = repr(tokens)
        assert "UNIQUE-SECRET-ACCESS" not in text
        assert "UNIQUE-SECRET-REFRESH" not in text
        assert "<redacted>" in text


class TestFetchRawProfileDevelopmentDiagnostic:
    """`fetch_raw_profile` is a development/manual-test-only diagnostic
    -- never called from any production code path. See its own
    docstring."""

    def test_calls_the_confirmed_profile_endpoint(self) -> None:
        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = {"_id": "u1"}
        with patch("requests.Session.send", return_value=mock_response) as mock_send:
            _client().fetch_raw_profile(access_token="at")
        assert mock_send.call_args.args[0].url == "https://api.chaster.app/auth/profile"

    def test_sends_the_access_token_as_a_bearer_header(self) -> None:
        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = {"_id": "u1"}
        with patch("requests.Session.send", return_value=mock_response) as mock_send:
            _client().fetch_raw_profile(access_token="real-access-token")
        assert mock_send.call_args.args[0].headers["Authorization"] == "Bearer real-access-token"

    def test_returns_the_raw_dict_unparsed(self) -> None:
        raw_body = {"_id": "u1", "username": "wearer1", "email": "someone@example.com"}
        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = raw_body
        with patch("requests.Session.send", return_value=mock_response):
            result = _client().fetch_raw_profile(access_token="at")
        assert result == raw_body

    def test_non_200_response_raises(self) -> None:
        mock_response = MagicMock(status_code=401)
        with patch("requests.Session.send", return_value=mock_response):
            with pytest.raises(ChasterTokenExchangeError):
                _client().fetch_raw_profile(access_token="at")

    def test_non_dict_response_body_raises(self) -> None:
        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = ["unexpected", "list"]
        with patch("requests.Session.send", return_value=mock_response):
            with pytest.raises(ChasterTokenExchangeError):
                _client().fetch_raw_profile(access_token="at")

    def test_empty_access_token_is_rejected_before_any_network_call(self) -> None:
        with patch("requests.Session.send") as mock_send:
            with pytest.raises(ValueError):
                _client().fetch_raw_profile(access_token="")
            mock_send.assert_not_called()

    def test_error_message_never_contains_the_access_token(self) -> None:
        mock_response = MagicMock(status_code=401)
        try:
            with patch("requests.Session.send", return_value=mock_response):
                _client().fetch_raw_profile(access_token="UNIQUE-SECRET-ACCESS-TOKEN")
            pytest.fail("expected ChasterTokenExchangeError")
        except ChasterTokenExchangeError as exc:
            assert "UNIQUE-SECRET-ACCESS-TOKEN" not in str(exc)

    def test_is_now_the_basis_of_the_real_production_identity_resolver(self) -> None:
        """This invariant intentionally changed: fetch_raw_profile()
        is no longer dev-only -- it is now the confirmed HTTP call
        underlying chaster/callback_service.py::build_real_identity_resolver(),
        the production identity resolver, evidence-backed by a real,
        complete GET /auth/profile response. This replaces the old
        'never wired into production' assertion, which protected an
        invariant that no longer holds by design."""
        import inspect

        import chaster.callback_service as module
        assert "fetch_raw_profile" in inspect.getsource(module)
        assert "build_real_identity_resolver" in module.__all__


class TestUserAgent:
    """Chaster's own developer support confirmed their API/Cloudflare
    edge rejects requests carrying Python's default client User-Agent
    -- every real request from this client must send the explicit,
    stable CHASTER_USER_AGENT value instead. Regression coverage for
    all three send_with_ordered_headers() call sites in this module."""

    def test_code_exchange_sends_the_explicit_user_agent(self) -> None:
        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = {
            "access_token": "at", "refresh_token": "rt", "expires_in": 300, "refresh_expires_in": 1800, "scope": "locks",
        }
        with patch("requests.Session.send", return_value=mock_response) as mock_send:
            _client().exchange_code_for_tokens(code="real-auth-code")
        assert mock_send.call_args.args[0].headers["User-Agent"] == CHASTER_USER_AGENT

    def test_refresh_sends_the_explicit_user_agent(self) -> None:
        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = {
            "access_token": "at", "refresh_token": "rt", "expires_in": 300, "refresh_expires_in": 1800, "scope": "locks",
        }
        with patch("requests.Session.send", return_value=mock_response) as mock_send:
            _client().refresh_tokens(refresh_token="real-refresh-token")
        assert mock_send.call_args.args[0].headers["User-Agent"] == CHASTER_USER_AGENT

    def test_fetch_raw_profile_sends_the_explicit_user_agent(self) -> None:
        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = {"_id": "u1"}
        with patch("requests.Session.send", return_value=mock_response) as mock_send:
            _client().fetch_raw_profile(access_token="at")
        assert mock_send.call_args.args[0].headers["User-Agent"] == CHASTER_USER_AGENT

    def test_user_agent_is_not_a_default_python_client_string(self) -> None:
        """Directly guards against regressing back to the rejected
        default -- must never look like a bare Python HTTP client's
        own default User-Agent."""
        assert "python" not in CHASTER_USER_AGENT.lower()
        assert "urllib" not in CHASTER_USER_AGENT.lower()
        assert CHASTER_USER_AGENT != "my-code"  # the placeholder value confirmed to work, never the production one

    def test_code_exchange_still_sends_client_secret_and_code_unchanged(self) -> None:
        """Confirms adding User-Agent did not disturb the existing,
        already-tested request body construction."""
        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = {
            "access_token": "at", "refresh_token": "rt", "expires_in": 300, "refresh_expires_in": 1800, "scope": "locks",
        }
        with patch("requests.Session.send", return_value=mock_response) as mock_send:
            _client().exchange_code_for_tokens(code="real-auth-code")
        sent_data = _body_as_dict(mock_send.call_args.args[0])
        assert sent_data["client_secret"] == "test-client-secret"
        assert sent_data["code"] == "real-auth-code"
        assert mock_send.call_args.args[0].url == TOKEN_URL


class TestHeaderOrder:
    """Regression coverage for the confirmed root cause: Chaster's own
    API/Cloudflare edge rejects requests.'s default header ordering
    (Authorization appended last). Authorization must come first,
    User-Agent second, on every real, authenticated request this
    client makes."""

    def test_fetch_raw_profile_sends_authorization_first_then_user_agent(self) -> None:
        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = {"_id": "u1"}
        with patch("requests.Session.send", return_value=mock_response) as mock_send:
            _client().fetch_raw_profile(access_token="at")
        header_keys = list(mock_send.call_args.args[0].headers.keys())
        assert header_keys[0] == "Authorization"
        assert header_keys[1] == "User-Agent"

    def test_code_exchange_sends_user_agent_first_when_no_authorization_present(self) -> None:
        """Token exchange never sends Authorization (it authenticates
        via client_id/client_secret in the body) -- User-Agent alone
        must still come first among the headers this client controls."""
        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = {
            "access_token": "at", "refresh_token": "rt", "expires_in": 300, "refresh_expires_in": 1800, "scope": "locks",
        }
        with patch("requests.Session.send", return_value=mock_response) as mock_send:
            _client().exchange_code_for_tokens(code="real-auth-code")
        header_keys = list(mock_send.call_args.args[0].headers.keys())
        assert "Authorization" not in header_keys
        assert header_keys[0] == "User-Agent"

    def test_content_type_and_content_length_are_preserved_after_reordering(self) -> None:
        """Confirms the reordering helper never drops requests' own
        form-body headers -- only moves them after Authorization/
        User-Agent."""
        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = {
            "access_token": "at", "refresh_token": "rt", "expires_in": 300, "refresh_expires_in": 1800, "scope": "locks",
        }
        with patch("requests.Session.send", return_value=mock_response) as mock_send:
            _client().exchange_code_for_tokens(code="real-auth-code")
        prepared = mock_send.call_args.args[0]
        assert prepared.headers["Content-Type"] == "application/x-www-form-urlencoded"
        assert int(prepared.headers["Content-Length"]) > 0

    def test_timeout_is_passed_through_to_session_send(self) -> None:
        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = {"_id": "u1"}
        with patch("requests.Session.send", return_value=mock_response) as mock_send:
            _client().fetch_raw_profile(access_token="at")
        assert mock_send.call_args.kwargs["timeout"] is not None


class TestRefreshFailureClassification:
    """`transient` separates "Chaster could not be reached" from "Chaster
    rejected this refresh token" -- only the latter may ever cause a
    connection to be marked needs_reauthorization."""

    @pytest.mark.parametrize("status", [400, 401, 403])
    def test_client_error_rejections_are_definitive(self, status: int) -> None:
        with patch("requests.Session.send", return_value=MagicMock(status_code=status)):
            with pytest.raises(ChasterTokenExchangeError) as excinfo:
                _client().refresh_tokens(refresh_token="rt")
        assert excinfo.value.status_code == status
        assert excinfo.value.transient is False

    @pytest.mark.parametrize("status", [429, 500, 502, 503])
    def test_rate_limit_and_server_errors_are_transient(self, status: int) -> None:
        with patch("requests.Session.send", return_value=MagicMock(status_code=status)):
            with pytest.raises(ChasterTokenExchangeError) as excinfo:
                _client().refresh_tokens(refresh_token="rt")
        assert excinfo.value.transient is True

    def test_network_error_is_transient(self) -> None:
        import requests
        with patch("requests.Session.send", side_effect=requests.ConnectionError("down")):
            with pytest.raises(ChasterTokenExchangeError) as excinfo:
                _client().refresh_tokens(refresh_token="rt")
        assert excinfo.value.transient is True
        assert excinfo.value.status_code is None

    def test_unparseable_success_body_is_not_transient(self) -> None:
        response = MagicMock(status_code=200)
        response.json.return_value = {"access_token": "at"}
        with patch("requests.Session.send", return_value=response):
            with pytest.raises(ChasterTokenExchangeError) as excinfo:
                _client().refresh_tokens(refresh_token="rt")
        assert excinfo.value.transient is False

    def test_refresh_request_uses_the_refresh_token_grant_at_the_token_url(self) -> None:
        from chaster.oauth_client import TOKEN_URL
        response = MagicMock(status_code=200)
        response.json.return_value = {
            "access_token": "a", "refresh_token": "r", "expires_in": 300, "refresh_expires_in": 1800, "scope": "locks",
        }
        with patch("requests.Session.send", return_value=response) as mock_send:
            _client().refresh_tokens(refresh_token="old-rt")
        prepared = mock_send.call_args.args[0]
        assert prepared.url == TOKEN_URL
        assert "grant_type=refresh_token" in prepared.body
        assert "refresh_token=old-rt" in prepared.body

    def test_plain_construction_defaults_to_definitive(self) -> None:
        error = ChasterTokenExchangeError("x")
        assert error.transient is False and error.status_code is None
