"""
tests/application/test_chaster_status_command.py

`chaster status` wired into ApplicationService (CHASTER-01B, read-only
display). Uses ApplicationService.handle_message() end-to-end through
the real CommandRouter -- exactly the same pattern
tests/application/test_chaster_connect_command.py already
established. All Chaster HTTP traffic mocked via
`patch.object(ChasterLockClient, "list_active_locks", ...)` -- no
real network calls.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from cryptography.fernet import Fernet

from application.models import IncomingMessage
from application.service import ApplicationService
from chaster.lock_client import ChasterLockApiError, ChasterLockClient
from chaster.oauth_client import ChasterOAuthClient
from chaster.repository import ChasterConnectionRepository
from chaster.token_encryptor import TokenDecryptionError, TokenEncryptor
from conversation_engine.engine import ConversationEngine
from conversation_engine.model_types import ModelGenerationRequest
from conversation_engine.subject_queue import SubjectConversationQueue
from infrastructure.database import Database as CoreDatabase
from memory_system.working_memory import InMemoryWorkingMemory

FIXED_TIME = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _apply_migrations(core: CoreDatabase) -> None:
    migrations_dir = Path(__file__).parent.parent.parent / "database" / "migrations"
    with core.raw_connection() as conn:
        for path in sorted(migrations_dir.glob("*.sql")):
            conn.executescript(path.read_text(encoding="utf-8"))


@pytest.fixture
def core(tmp_path: Path) -> CoreDatabase:
    c = CoreDatabase(tmp_path / "test.db")
    _apply_migrations(c)
    return c


class _RecordingModel:
    def __init__(self, response: str = "a plain conversational reply") -> None:
        self._response = response
        self.calls: list[ModelGenerationRequest] = []

    def generate(self, *, request: ModelGenerationRequest) -> str:
        self.calls.append(request)
        return self._response


@pytest.fixture
def model() -> _RecordingModel:
    return _RecordingModel()


def _real_oauth_client() -> ChasterOAuthClient:
    return ChasterOAuthClient(client_id="test-client-id", client_secret="test-secret", redirect_uri="https://example.com/oauth/chaster/callback")


def _real_connections(core: CoreDatabase, tmp_path: Path) -> ChasterConnectionRepository:
    return ChasterConnectionRepository(tmp_path / "test.db", core=core, encryptor=TokenEncryptor(Fernet.generate_key()))


@pytest.fixture
def configured_service(core: CoreDatabase, tmp_path: Path, model: _RecordingModel) -> ApplicationService:
    buffer = InMemoryWorkingMemory(max_exchanges_per_subject=10, max_characters_per_subject=8000)
    engine = ConversationEngine(model=model, working_memory_reader=buffer, working_memory_writer=buffer, queue=SubjectConversationQueue())
    return ApplicationService(
        core.db_path, core=core, conversation_engine=engine,
        chaster_connections=_real_connections(core, tmp_path), chaster_oauth_client=_real_oauth_client(),
    )


@pytest.fixture
def unconfigured_service(core: CoreDatabase) -> ApplicationService:
    return ApplicationService(core.db_path, core=core)  # no chaster_connections/oauth_client -- "not configured"


def _incoming(text: str, *, external_user_id: str = "42", now: datetime = FIXED_TIME, external_message_id: str | None = None) -> IncomingMessage:
    return IncomingMessage(channel="discord", external_user_id=external_user_id, text=text, received_at=now, external_message_id=external_message_id)


def _complete_onboarding(service: ApplicationService, *, external_user_id: str = "42") -> None:
    service.handle_message(_incoming("anything", external_user_id=external_user_id, external_message_id="ob0"))
    service.handle_message(_incoming("english", external_user_id=external_user_id, external_message_id="ob1"))
    service.handle_message(_incoming("neutral", external_user_id=external_user_id, external_message_id="ob2"))
    service.handle_message(_incoming("alex", external_user_id=external_user_id, external_message_id="ob3"))


def _connect_user(service: ApplicationService, *, external_user_id: str = "42", access_token_expires_at: datetime | None = None) -> None:
    user = service.user_service.get_or_create_user("discord", external_user_id, now=FIXED_TIME)
    service.chaster_connections.create_or_replace(
        user_id=user.id, chaster_account_id="chaster-acc-1", chaster_username="wearer1",
        access_token="real-access-token", refresh_token="real-refresh-token",
        access_token_expires_at=access_token_expires_at or (FIXED_TIME + timedelta(hours=1)),
        refresh_token_expires_at=FIXED_TIME + timedelta(days=30),
        granted_scopes=("locks",), now=FIXED_TIME,
    )


class TestChasterStatusDeterministicRouting:
    def test_chaster_status_never_invokes_the_model(self, configured_service: ApplicationService, model: _RecordingModel) -> None:
        _complete_onboarding(configured_service)
        with patch.object(ChasterLockClient, "list_active_locks", return_value=[]):
            configured_service.handle_message(_incoming("chaster status", external_message_id="m1"))
        assert model.calls == []

    def test_chaster_status_is_registered_alongside_chaster_connect(self, configured_service: ApplicationService) -> None:
        _complete_onboarding(configured_service)
        _connect_user(configured_service)
        with patch.object(ChasterLockClient, "list_active_locks", return_value=[]):
            result = configured_service.handle_message(_incoming("chaster status", external_message_id="m1"))
        assert "no active locks" in result.text.lower()


class TestNotConfigured:
    def test_chaster_status_gives_a_safe_reply_when_bot_not_configured(self, unconfigured_service: ApplicationService) -> None:
        _complete_onboarding(unconfigured_service)
        result = unconfigured_service.handle_message(_incoming("chaster status", external_message_id="m1"))
        assert "not configured" in result.text.lower()
        assert result.embed is not None and result.embed.title == "Chaster Not Configured" and result.embed.color == "red"


class TestNotConnected:
    """Covers requirement 1: unconnected user -> red "not connected" embed."""

    def test_user_with_no_connection_gets_the_not_connected_embed(self, configured_service: ApplicationService) -> None:
        _complete_onboarding(configured_service)
        result = configured_service.handle_message(_incoming("chaster status", external_message_id="m1"))
        assert result.embed is not None
        assert result.embed.title == "Chaster Account Not Connected"
        assert result.embed.color == "red"
        assert "chaster connect" in result.embed.description.lower()

    def test_decryption_failure_gets_the_reconnect_embed(self, configured_service: ApplicationService) -> None:
        """A connection row exists but can't be decrypted (e.g. wrong
        encryption key). INTENTIONALLY CHANGED: this is now a red
        "Reconnection Required" embed (the connection is marked
        needs_reauthorization and `chaster connect` fixes it) rather
        than the generic "Not Connected" one. Never crashes."""
        _complete_onboarding(configured_service)
        _connect_user(configured_service)
        with patch.object(configured_service.chaster_connections, "get_decrypted_access_token", side_effect=TokenDecryptionError("bad key")):
            result = configured_service.handle_message(_incoming("chaster status", external_message_id="m1"))
        assert result.embed.title == "Chaster Reconnection Required"
        assert result.embed.color == "red"
        assert "chaster connect" in result.embed.description.lower()


class TestZeroActiveLocks:
    """Covers requirement 2: connected user, zero locks -> blue embed."""

    def test_zero_locks_gives_the_blue_status_embed(self, configured_service: ApplicationService) -> None:
        _complete_onboarding(configured_service)
        _connect_user(configured_service)
        with patch.object(ChasterLockClient, "list_active_locks", return_value=[]):
            result = configured_service.handle_message(_incoming("chaster status", external_message_id="m1"))
        assert result.embed.title == "\U0001f512 Chaster Status"
        assert result.embed.color == "blue"
        assert result.embed.description == "You currently have no active locks."
        assert result.embed.fields == ()


class TestActiveLocks:
    """Covers requirement 3: connected user, active locks -> green
    embed with per-lock fields and time-remaining calculation."""

    def test_single_lock_with_a_future_end_date(self, configured_service: ApplicationService) -> None:
        _complete_onboarding(configured_service)
        _connect_user(configured_service)
        end_date = (FIXED_TIME + timedelta(days=2, hours=3, minutes=15)).isoformat().replace("+00:00", "Z")
        lock = {"_id": "lock1", "title": "My Chastity Lock", "status": "locked", "endDate": end_date}
        with patch.object(ChasterLockClient, "list_active_locks", return_value=[lock]):
            result = configured_service.handle_message(_incoming("chaster status", external_message_id="m1"))
        assert result.embed.title == "\U0001f512 Active Locks (1)"
        assert result.embed.color == "green"
        assert len(result.embed.fields) == 1
        field = result.embed.fields[0]
        assert field.name == "\U0001f512 My Chastity Lock"
        assert "**Status:** locked" in field.value
        assert "**Time Remaining:** 2d 3h 15m" in field.value

    def test_multiple_locks_produce_one_field_each_in_order(self, configured_service: ApplicationService) -> None:
        _complete_onboarding(configured_service)
        _connect_user(configured_service)
        locks = [
            {"_id": "lock1", "title": "First", "status": "locked"},
            {"_id": "lock2", "title": "Second", "status": "unlocked"},
        ]
        with patch.object(ChasterLockClient, "list_active_locks", return_value=locks):
            result = configured_service.handle_message(_incoming("chaster status", external_message_id="m1"))
        assert result.embed.title == "\U0001f512 Active Locks (2)"
        assert [f.name for f in result.embed.fields] == ["\U0001f512 First", "\U0001f512 Second"]

    def test_no_end_date_shows_unlimited_permanent(self, configured_service: ApplicationService) -> None:
        _complete_onboarding(configured_service)
        _connect_user(configured_service)
        lock = {"_id": "lock1", "title": "Permanent Lock", "status": "locked"}  # no endDate key at all
        with patch.object(ChasterLockClient, "list_active_locks", return_value=[lock]):
            result = configured_service.handle_message(_incoming("chaster status", external_message_id="m1"))
        assert "**Time Remaining:** Unlimited / Permanent" in result.embed.fields[0].value

    def test_past_end_date_shows_expired(self, configured_service: ApplicationService) -> None:
        _complete_onboarding(configured_service)
        _connect_user(configured_service)
        end_date = (FIXED_TIME - timedelta(hours=5)).isoformat().replace("+00:00", "Z")
        lock = {"_id": "lock1", "title": "Expired Lock", "status": "unlocked", "endDate": end_date}
        with patch.object(ChasterLockClient, "list_active_locks", return_value=[lock]):
            result = configured_service.handle_message(_incoming("chaster status", external_message_id="m1"))
        assert "**Time Remaining:** Expired" in result.embed.fields[0].value

    def test_unparseable_end_date_falls_back_to_unlimited_permanent(self, configured_service: ApplicationService) -> None:
        _complete_onboarding(configured_service)
        _connect_user(configured_service)
        lock = {"_id": "lock1", "title": "Weird Date Lock", "status": "locked", "endDate": "not-a-real-date"}
        with patch.object(ChasterLockClient, "list_active_locks", return_value=[lock]):
            result = configured_service.handle_message(_incoming("chaster status", external_message_id="m1"))
        assert "**Time Remaining:** Unlimited / Permanent" in result.embed.fields[0].value

    def test_missing_title_falls_back_to_untitled(self, configured_service: ApplicationService) -> None:
        _complete_onboarding(configured_service)
        _connect_user(configured_service)
        lock = {"_id": "lock1", "status": "locked"}  # no title key
        with patch.object(ChasterLockClient, "list_active_locks", return_value=[lock]):
            result = configured_service.handle_message(_incoming("chaster status", external_message_id="m1"))
        assert result.embed.fields[0].name == "\U0001f512 (untitled)"

    def test_missing_status_falls_back_to_unknown(self, configured_service: ApplicationService) -> None:
        _complete_onboarding(configured_service)
        _connect_user(configured_service)
        lock = {"_id": "lock1", "title": "No Status Lock"}  # no status key
        with patch.object(ChasterLockClient, "list_active_locks", return_value=[lock]):
            result = configured_service.handle_message(_incoming("chaster status", external_message_id="m1"))
        assert "**Status:** unknown" in result.embed.fields[0].value

    def test_raw_status_value_shown_as_is_never_reinterpreted(self, configured_service: ApplicationService) -> None:
        """Displays whatever string Chaster returns -- never maps it
        to an enum or makes a decision from it."""
        _complete_onboarding(configured_service)
        _connect_user(configured_service)
        lock = {"_id": "lock1", "title": "Odd Status Lock", "status": "some-unexpected-value"}
        with patch.object(ChasterLockClient, "list_active_locks", return_value=[lock]):
            result = configured_service.handle_message(_incoming("chaster status", external_message_id="m1"))
        assert "**Status:** some-unexpected-value" in result.embed.fields[0].value


class TestApiFailure:
    """Covers requirement 4: API failure -> red error embed."""

    def test_api_error_gives_a_red_error_embed(self, configured_service: ApplicationService) -> None:
        _complete_onboarding(configured_service)
        _connect_user(configured_service)
        with patch.object(ChasterLockClient, "list_active_locks", side_effect=ChasterLockApiError("Chaster's locks endpoint returned HTTP 500.")):
            result = configured_service.handle_message(_incoming("chaster status", external_message_id="m1"))
        assert result.embed.title == "Chaster API Error"
        assert result.embed.color == "red"
        assert "500" in result.embed.description

    def test_network_error_also_gives_a_red_error_embed(self, configured_service: ApplicationService) -> None:
        _complete_onboarding(configured_service)
        _connect_user(configured_service)
        with patch.object(ChasterLockClient, "list_active_locks", side_effect=ChasterLockApiError("Network error contacting Chaster's locks endpoint.")):
            result = configured_service.handle_message(_incoming("chaster status", external_message_id="m1"))
        assert result.embed.title == "Chaster API Error"
        assert result.embed.color == "red"

    def test_api_failure_never_crashes_the_handler(self, configured_service: ApplicationService) -> None:
        _complete_onboarding(configured_service)
        _connect_user(configured_service)
        with patch.object(ChasterLockClient, "list_active_locks", side_effect=ChasterLockApiError("boom")):
            result = configured_service.handle_message(_incoming("chaster status", external_message_id="m1"))  # must not raise
        assert result is not None


class TestNoWriteOperations:
    def test_chaster_status_never_calls_emergency_unlock(self, configured_service: ApplicationService) -> None:
        _complete_onboarding(configured_service)
        _connect_user(configured_service)
        with patch.object(ChasterLockClient, "list_active_locks", return_value=[]) as mock_list, \
             patch.object(ChasterLockClient, "emergency_unlock") as mock_unlock:
            configured_service.handle_message(_incoming("chaster status", external_message_id="m1"))
        mock_list.assert_called_once()
        mock_unlock.assert_not_called()

    def test_chaster_status_never_writes_to_working_memory(self, configured_service: ApplicationService) -> None:
        _complete_onboarding(configured_service)
        _connect_user(configured_service)
        with patch.object(ChasterLockClient, "list_active_locks", return_value=[]):
            configured_service.handle_message(_incoming("chaster status", external_message_id="m1"))
        user = configured_service.user_service.get_or_create_user("discord", "42", now=FIXED_TIME)
        snapshot = configured_service._conversation_engine._working_memory_reader.read(subject_key=user.id)
        assert snapshot.turns == ()


class TestNoSecretLeakage:
    def test_access_token_never_appears_in_any_reply(self, configured_service: ApplicationService) -> None:
        _complete_onboarding(configured_service)
        _connect_user(configured_service)
        with patch.object(ChasterLockClient, "list_active_locks", return_value=[]):
            result = configured_service.handle_message(_incoming("chaster status", external_message_id="m1"))
        assert "real-access-token" not in result.text
        assert result.embed is None or "real-access-token" not in (result.embed.description or "")


class TestSelfHealingTokenRefresh:
    """`chaster status` never needs manual database intervention: an
    expired token is refreshed before the call, a 401 is refreshed and
    retried once, and only a definitively rejected refresh token ends in
    a reconnect prompt."""

    @staticmethod
    def _new_tokens():
        from chaster.oauth_client import ChasterTokenResponse
        return ChasterTokenResponse(
            access_token="fresh-access-token", refresh_token="fresh-refresh-token",
            expires_in=300, refresh_expires_in=1800, granted_scopes=("locks",),
        )

    def test_expired_access_token_is_refreshed_transparently(self, configured_service: ApplicationService) -> None:
        _complete_onboarding(configured_service)
        _connect_user(configured_service, access_token_expires_at=FIXED_TIME - timedelta(minutes=1))
        with patch.object(ChasterOAuthClient, "refresh_tokens", return_value=self._new_tokens()) as refresh, \
             patch.object(ChasterLockClient, "list_active_locks", return_value=[]) as lock_call:
            result = configured_service.handle_message(_incoming("chaster status", external_message_id="m1"))
        refresh.assert_called_once_with(refresh_token="real-refresh-token")
        assert lock_call.call_args.kwargs["access_token"] == "fresh-access-token"
        assert result.embed.color == "blue"

    def test_refreshed_tokens_are_persisted_so_the_next_call_needs_no_refresh(self, configured_service: ApplicationService) -> None:
        _complete_onboarding(configured_service)
        _connect_user(configured_service, access_token_expires_at=FIXED_TIME - timedelta(minutes=1))
        with patch.object(ChasterOAuthClient, "refresh_tokens", return_value=self._new_tokens()) as refresh, \
             patch.object(ChasterLockClient, "list_active_locks", return_value=[]):
            configured_service.handle_message(_incoming("chaster status", external_message_id="m1"))
            configured_service.handle_message(_incoming("chaster status", external_message_id="m2"))
        assert refresh.call_count == 1

    def test_a_401_refreshes_and_retries_once_then_succeeds(self, configured_service: ApplicationService) -> None:
        _complete_onboarding(configured_service)
        _connect_user(configured_service)
        with patch.object(ChasterOAuthClient, "refresh_tokens", return_value=self._new_tokens()) as refresh, \
             patch.object(ChasterLockClient, "list_active_locks",
                          side_effect=[ChasterLockApiError("401", status_code=401), []]) as lock_call:
            result = configured_service.handle_message(_incoming("chaster status", external_message_id="m1"))
        assert lock_call.call_count == 2
        assert lock_call.call_args_list[1].kwargs["access_token"] == "fresh-access-token"
        refresh.assert_called_once()
        assert result.embed.color == "blue"

    def test_a_revoked_refresh_token_gives_the_red_reconnect_embed(self, configured_service: ApplicationService) -> None:
        from chaster.oauth_client import ChasterTokenExchangeError
        _complete_onboarding(configured_service)
        _connect_user(configured_service, access_token_expires_at=FIXED_TIME - timedelta(minutes=1))
        with patch.object(ChasterOAuthClient, "refresh_tokens", side_effect=ChasterTokenExchangeError("rejected", status_code=400)):
            result = configured_service.handle_message(_incoming("chaster status", external_message_id="m1"))
        assert result.embed.title == "Chaster Reconnection Required"
        assert result.embed.color == "red"
        assert "chaster connect" in result.embed.description.lower()
        assert "chaster connect" in result.text.lower()

    def test_a_revoked_refresh_token_marks_the_connection_needs_reauthorization(self, configured_service: ApplicationService) -> None:
        from chaster.oauth_client import ChasterTokenExchangeError
        _complete_onboarding(configured_service)
        _connect_user(configured_service, access_token_expires_at=FIXED_TIME - timedelta(minutes=1))
        with patch.object(ChasterOAuthClient, "refresh_tokens", side_effect=ChasterTokenExchangeError("rejected", status_code=400)):
            configured_service.handle_message(_incoming("chaster status", external_message_id="m1"))
        user = configured_service.user_service.get_or_create_user("discord", "42", now=FIXED_TIME)
        assert configured_service.chaster_connections.get(user.id).connection_status.value == "needs_reauthorization"

    def test_a_marked_connection_does_not_hit_the_network_again(self, configured_service: ApplicationService) -> None:
        from chaster.oauth_client import ChasterTokenExchangeError
        _complete_onboarding(configured_service)
        _connect_user(configured_service, access_token_expires_at=FIXED_TIME - timedelta(minutes=1))
        with patch.object(ChasterOAuthClient, "refresh_tokens", side_effect=ChasterTokenExchangeError("rejected", status_code=400)) as refresh:
            configured_service.handle_message(_incoming("chaster status", external_message_id="m1"))
            configured_service.handle_message(_incoming("chaster status", external_message_id="m2"))
        assert refresh.call_count == 1

    def test_a_transient_outage_gives_a_red_retry_embed_and_keeps_the_connection(self, configured_service: ApplicationService) -> None:
        from chaster.oauth_client import ChasterTokenExchangeError
        _complete_onboarding(configured_service)
        _connect_user(configured_service, access_token_expires_at=FIXED_TIME - timedelta(minutes=1))
        with patch.object(ChasterOAuthClient, "refresh_tokens", side_effect=ChasterTokenExchangeError("down", transient=True)):
            result = configured_service.handle_message(_incoming("chaster status", external_message_id="m1"))
        assert result.embed.title == "Chaster Temporarily Unavailable"
        assert result.embed.color == "red"
        user = configured_service.user_service.get_or_create_user("discord", "42", now=FIXED_TIME)
        assert configured_service.chaster_connections.get(user.id).connection_status.value == "active"

    def test_reconnect_embed_never_leaks_token_values(self, configured_service: ApplicationService) -> None:
        from chaster.oauth_client import ChasterTokenExchangeError
        _complete_onboarding(configured_service)
        _connect_user(configured_service, access_token_expires_at=FIXED_TIME - timedelta(minutes=1))
        with patch.object(ChasterOAuthClient, "refresh_tokens", side_effect=ChasterTokenExchangeError("rejected", status_code=400)):
            result = configured_service.handle_message(_incoming("chaster status", external_message_id="m1"))
        assert "real-refresh-token" not in result.text and "real-access-token" not in result.text

    def test_status_after_a_reconnect_works_again(self, configured_service: ApplicationService) -> None:
        """End-to-end self-heal: revoked -> reconnect prompt -> the
        callback re-links (simulated) -> status works, no manual DB step."""
        from chaster.oauth_client import ChasterTokenExchangeError
        _complete_onboarding(configured_service)
        _connect_user(configured_service, access_token_expires_at=FIXED_TIME - timedelta(minutes=1))
        with patch.object(ChasterOAuthClient, "refresh_tokens", side_effect=ChasterTokenExchangeError("rejected", status_code=400)):
            first = configured_service.handle_message(_incoming("chaster status", external_message_id="m1"))
        assert first.embed.title == "Chaster Reconnection Required"
        _connect_user(configured_service)   # what a successful callback does (create_or_replace)
        with patch.object(ChasterLockClient, "list_active_locks", return_value=[]):
            second = configured_service.handle_message(_incoming("chaster status", external_message_id="m2"))
        assert second.embed.color == "blue"
