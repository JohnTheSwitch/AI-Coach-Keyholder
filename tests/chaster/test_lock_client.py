"""
tests/chaster/test_lock_client.py

All Chaster HTTP traffic mocked -- no real network calls, no real
Chaster credentials required. Since every real request now goes
through `chaster._http.send_with_ordered_headers()` (a
`requests.Session()` + `session.send(prepared, ...)` call, not the
module-level `requests.get()`/`requests.post()` shortcuts), these
tests patch `requests.Session.send` directly and inspect the actual
`PreparedRequest` object it receives.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from chaster import CHASTER_USER_AGENT
from chaster.lock_client import (
    LOCKS_URL,
    ChasterLockApiError,
    ChasterLockClient,
    EmergencyUnlockHttpOutcome,
)


def _client() -> ChasterLockClient:
    return ChasterLockClient()


class TestListActiveLocks:
    def test_calls_the_confirmed_locks_endpoint_with_status_active(self) -> None:
        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = []
        with patch("requests.Session.send", return_value=mock_response) as mock_send:
            _client().list_active_locks(access_token="real-access-token")
        prepared = mock_send.call_args.args[0]
        assert prepared.url == f"{LOCKS_URL}?status=active"

    def test_sends_the_access_token_as_a_bearer_header(self) -> None:
        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = []
        with patch("requests.Session.send", return_value=mock_response) as mock_send:
            _client().list_active_locks(access_token="real-access-token")
        assert mock_send.call_args.args[0].headers["Authorization"] == "Bearer real-access-token"

    def test_returns_the_raw_list_unparsed(self) -> None:
        raw_locks = [{"_id": "lock1"}, {"_id": "lock2"}]
        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = raw_locks
        with patch("requests.Session.send", return_value=mock_response):
            result = _client().list_active_locks(access_token="at")
        assert result == raw_locks

    def test_empty_result_list_is_returned_as_is(self) -> None:
        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = []
        with patch("requests.Session.send", return_value=mock_response):
            result = _client().list_active_locks(access_token="at")
        assert result == []

    def test_non_200_response_raises(self) -> None:
        mock_response = MagicMock(status_code=401)
        with patch("requests.Session.send", return_value=mock_response):
            with pytest.raises(ChasterLockApiError):
                _client().list_active_locks(access_token="at")

    def test_non_list_response_body_raises(self) -> None:
        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = {"unexpected": "shape"}
        with patch("requests.Session.send", return_value=mock_response):
            with pytest.raises(ChasterLockApiError):
                _client().list_active_locks(access_token="at")

    def test_network_error_raises(self) -> None:
        import requests
        with patch("requests.Session.send", side_effect=requests.ConnectionError("boom")):
            with pytest.raises(ChasterLockApiError):
                _client().list_active_locks(access_token="at")

    def test_empty_access_token_is_rejected_before_any_network_call(self) -> None:
        with patch("requests.Session.send") as mock_send:
            with pytest.raises(ValueError):
                _client().list_active_locks(access_token="")
            mock_send.assert_not_called()

    def test_error_message_never_contains_the_access_token(self) -> None:
        mock_response = MagicMock(status_code=401)
        try:
            with patch("requests.Session.send", return_value=mock_response):
                _client().list_active_locks(access_token="UNIQUE-SECRET-ACCESS-TOKEN")
            pytest.fail("expected ChasterLockApiError")
        except ChasterLockApiError as exc:
            assert "UNIQUE-SECRET-ACCESS-TOKEN" not in str(exc)


class TestEmergencyUnlock:
    def test_calls_the_confirmed_emergency_unlock_endpoint(self) -> None:
        mock_response = MagicMock(status_code=204)
        with patch("requests.Session.send", return_value=mock_response) as mock_send:
            _client().emergency_unlock(access_token="at", lock_id="real-lock-id")
        assert mock_send.call_args.args[0].url == f"{LOCKS_URL}/real-lock-id/emergency-unlock"

    def test_sends_the_access_token_as_a_bearer_header(self) -> None:
        mock_response = MagicMock(status_code=204)
        with patch("requests.Session.send", return_value=mock_response) as mock_send:
            _client().emergency_unlock(access_token="real-access-token", lock_id="lock1")
        assert mock_send.call_args.args[0].headers["Authorization"] == "Bearer real-access-token"

    def test_204_maps_to_succeeded(self) -> None:
        mock_response = MagicMock(status_code=204)
        with patch("requests.Session.send", return_value=mock_response):
            result = _client().emergency_unlock(access_token="at", lock_id="lock1")
        assert result.outcome is EmergencyUnlockHttpOutcome.SUCCEEDED

    def test_400_maps_to_not_eligible(self) -> None:
        mock_response = MagicMock(status_code=400)
        with patch("requests.Session.send", return_value=mock_response):
            result = _client().emergency_unlock(access_token="at", lock_id="lock1")
        assert result.outcome is EmergencyUnlockHttpOutcome.NOT_ELIGIBLE

    def test_401_maps_to_unauthorized(self) -> None:
        mock_response = MagicMock(status_code=401)
        with patch("requests.Session.send", return_value=mock_response):
            result = _client().emergency_unlock(access_token="at", lock_id="lock1")
        assert result.outcome is EmergencyUnlockHttpOutcome.UNAUTHORIZED

    def test_403_maps_to_forbidden(self) -> None:
        mock_response = MagicMock(status_code=403)
        with patch("requests.Session.send", return_value=mock_response):
            result = _client().emergency_unlock(access_token="at", lock_id="lock1")
        assert result.outcome is EmergencyUnlockHttpOutcome.FORBIDDEN

    def test_404_maps_to_not_found(self) -> None:
        mock_response = MagicMock(status_code=404)
        with patch("requests.Session.send", return_value=mock_response):
            result = _client().emergency_unlock(access_token="at", lock_id="lock1")
        assert result.outcome is EmergencyUnlockHttpOutcome.NOT_FOUND

    def test_unexpected_status_maps_to_unexpected_never_succeeded(self) -> None:
        mock_response = MagicMock(status_code=500)
        with patch("requests.Session.send", return_value=mock_response):
            result = _client().emergency_unlock(access_token="at", lock_id="lock1")
        assert result.outcome is EmergencyUnlockHttpOutcome.UNEXPECTED
        assert result.outcome is not EmergencyUnlockHttpOutcome.SUCCEEDED

    def test_network_error_raises_never_silently_reports_success(self) -> None:
        import requests
        with patch("requests.Session.send", side_effect=requests.Timeout("timed out")):
            with pytest.raises(ChasterLockApiError):
                _client().emergency_unlock(access_token="at", lock_id="lock1")

    def test_empty_lock_id_is_rejected_before_any_network_call(self) -> None:
        with patch("requests.Session.send") as mock_send:
            with pytest.raises(ValueError):
                _client().emergency_unlock(access_token="at", lock_id="")
            mock_send.assert_not_called()

    def test_error_message_never_contains_the_access_token(self) -> None:
        import requests
        try:
            with patch("requests.Session.send", side_effect=requests.ConnectionError("boom")):
                _client().emergency_unlock(access_token="UNIQUE-SECRET-ACCESS-TOKEN", lock_id="lock1")
            pytest.fail("expected ChasterLockApiError")
        except ChasterLockApiError as exc:
            assert "UNIQUE-SECRET-ACCESS-TOKEN" not in str(exc)


class TestUserAgent:
    """Chaster's own developer support confirmed their API/Cloudflare
    edge rejects requests carrying Python's default client User-Agent
    -- every real request from this client must send the explicit,
    stable CHASTER_USER_AGENT value instead."""

    def test_list_active_locks_sends_the_explicit_user_agent(self) -> None:
        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = []
        with patch("requests.Session.send", return_value=mock_response) as mock_send:
            _client().list_active_locks(access_token="at")
        assert mock_send.call_args.args[0].headers["User-Agent"] == CHASTER_USER_AGENT

    def test_emergency_unlock_sends_the_explicit_user_agent(self) -> None:
        mock_response = MagicMock(status_code=204)
        with patch("requests.Session.send", return_value=mock_response) as mock_send:
            _client().emergency_unlock(access_token="at", lock_id="lock1")
        assert mock_send.call_args.args[0].headers["User-Agent"] == CHASTER_USER_AGENT

    def test_authorization_header_still_sent_alongside_user_agent(self) -> None:
        """Confirms adding User-Agent did not disturb the existing,
        already-tested Authorization header."""
        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = []
        with patch("requests.Session.send", return_value=mock_response) as mock_send:
            _client().list_active_locks(access_token="real-token-value")
        prepared = mock_send.call_args.args[0]
        assert prepared.headers["Authorization"] == "Bearer real-token-value"
        assert prepared.headers["User-Agent"] == CHASTER_USER_AGENT


class TestHeaderOrder:
    """Regression coverage for the confirmed root cause: Chaster's own
    API/Cloudflare edge rejects requests.'s default header ordering
    (Authorization appended last). Authorization must come first,
    User-Agent second, on every real request this client makes --
    inspecting the actual PreparedRequest passed to Session.send, not
    merely the arguments this client's own methods were called with."""

    def test_list_active_locks_sends_authorization_first_then_user_agent(self) -> None:
        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = []
        with patch("requests.Session.send", return_value=mock_response) as mock_send:
            _client().list_active_locks(access_token="at")
        header_keys = list(mock_send.call_args.args[0].headers.keys())
        assert header_keys[0] == "Authorization"
        assert header_keys[1] == "User-Agent"

    def test_emergency_unlock_sends_authorization_first_then_user_agent(self) -> None:
        mock_response = MagicMock(status_code=204)
        with patch("requests.Session.send", return_value=mock_response) as mock_send:
            _client().emergency_unlock(access_token="at", lock_id="lock1")
        header_keys = list(mock_send.call_args.args[0].headers.keys())
        assert header_keys[0] == "Authorization"
        assert header_keys[1] == "User-Agent"

    def test_remaining_requests_default_headers_are_preserved_after_reordering(self) -> None:
        """Confirms the helper reorders rather than strips -- Accept/
        Accept-Encoding/Connection (requests' own defaults) must still
        be present, just after Authorization/User-Agent."""
        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = []
        with patch("requests.Session.send", return_value=mock_response) as mock_send:
            _client().list_active_locks(access_token="at")
        header_keys = set(mock_send.call_args.args[0].headers.keys())
        assert {"Accept", "Accept-Encoding", "Connection"}.issubset(header_keys)

    def test_query_params_are_preserved_in_the_prepared_url(self) -> None:
        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = []
        with patch("requests.Session.send", return_value=mock_response) as mock_send:
            _client().list_active_locks(access_token="at")
        assert "status=active" in mock_send.call_args.args[0].url

    def test_timeout_is_passed_through_to_session_send(self) -> None:
        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = []
        with patch("requests.Session.send", return_value=mock_response) as mock_send:
            _client().list_active_locks(access_token="at")
        assert mock_send.call_args.kwargs["timeout"] is not None


class TestLockApiErrorStatusCode:
    """`status_code` lets chaster/token_manager.py recognise an expired
    token (HTTP 401) without parsing message text."""

    def test_non_200_response_carries_its_status_code(self) -> None:
        mock_response = MagicMock(status_code=401)
        with patch("requests.Session.send", return_value=mock_response):
            with pytest.raises(ChasterLockApiError) as excinfo:
                _client().list_active_locks(access_token="at")
        assert excinfo.value.status_code == 401

    def test_network_error_has_no_status_code(self) -> None:
        import requests
        with patch("requests.Session.send", side_effect=requests.ConnectionError("down")):
            with pytest.raises(ChasterLockApiError) as excinfo:
                _client().list_active_locks(access_token="at")
        assert excinfo.value.status_code is None

    def test_error_can_still_be_constructed_with_only_a_message(self) -> None:
        assert ChasterLockApiError("x").status_code is None
