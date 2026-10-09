"""
tests/application/test_chaster_connect_command.py

`chaster connect` wired into ApplicationService (CHASTER-01A). Uses
ApplicationService.handle_message() end-to-end through the real
CommandRouter -- exactly the same pattern already established for
lock/task/mode commands.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from cryptography.fernet import Fernet

from application.models import IncomingMessage
from application.service import ApplicationService
from chaster.oauth_client import ChasterOAuthClient
from chaster.repository import ChasterConnectionRepository
from chaster.token_encryptor import TokenEncryptor
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


class TestChasterConnectDeterministicRouting:
    def test_chaster_connect_never_invokes_the_model(self, configured_service: ApplicationService, model: _RecordingModel) -> None:
        _complete_onboarding(configured_service)
        configured_service.handle_message(_incoming("chaster connect", external_message_id="m1"))
        assert model.calls == []

    def test_chaster_connect_returns_a_real_authorization_url(self, configured_service: ApplicationService) -> None:
        _complete_onboarding(configured_service)
        result = configured_service.handle_message(_incoming("chaster connect", external_message_id="m1"))
        assert "https://sso.chaster.app/auth/realms/app/protocol/openid-connect/auth" in result.text
        assert "state=" in result.text

    def test_bare_chaster_word_triggers_the_family_fallback(self, configured_service: ApplicationService) -> None:
        _complete_onboarding(configured_service)
        result = configured_service.handle_message(_incoming("chaster", external_message_id="m1"))
        assert "not a recognized `chaster` command" in result.text.lower()

    def test_near_miss_chaster_input_reaches_conversation_not_the_family_fallback(
        self, configured_service: ApplicationService, model: _RecordingModel,
    ) -> None:
        """Command Family Fallback Precision applies here too --
        confirms the chaster family wasn't wired with the old,
        already-fixed prefix-matching bug."""
        _complete_onboarding(configured_service)
        configured_service.handle_message(_incoming("chaster is confusing to set up", external_message_id="m1"))
        assert len(model.calls) == 1


class TestChasterConnectIdentityBinding:
    def test_two_different_discord_users_get_different_states(self, configured_service: ApplicationService) -> None:
        _complete_onboarding(configured_service, external_user_id="42")
        _complete_onboarding(configured_service, external_user_id="99")
        result1 = configured_service.handle_message(_incoming("chaster connect", external_user_id="42", external_message_id="m1"))
        result2 = configured_service.handle_message(_incoming("chaster connect", external_user_id="99", external_message_id="m1"))
        # Extract state= query values crudely -- just confirm they differ.
        state1 = result1.text.split("state=")[1].split("&")[0].split()[0]
        state2 = result2.text.split("state=")[1].split("&")[0].split()[0]
        assert state1 != state2

    def test_a_second_chaster_connect_replaces_the_pending_state(self, configured_service: ApplicationService) -> None:
        _complete_onboarding(configured_service)
        first = configured_service.handle_message(_incoming("chaster connect", external_message_id="m1"))
        second = configured_service.handle_message(_incoming("chaster connect", external_message_id="m2"))
        state1 = first.text.split("state=")[1].split("&")[0].split()[0]
        state2 = second.text.split("state=")[1].split("&")[0].split()[0]
        assert state1 != state2


class TestChasterConnectAlreadyConnected:
    """INTENTIONALLY CHANGED from the CHASTER-01A slice, which refused to
    issue a link to an already-connected user. A user whose connection
    expired or was revoked must be able to re-link without any manual
    database step, so `chaster connect` now always issues a fresh link."""

    @staticmethod
    def _connect(service: ApplicationService, *, status_needs_reauth: bool = False) -> str:
        from datetime import timedelta
        user = service.user_service.get_or_create_user("discord", "42", now=FIXED_TIME)
        service.chaster_connections.create_or_replace(
            user_id=user.id, chaster_account_id="chaster-acc-1", chaster_username="wearer1",
            access_token="at", refresh_token="rt",
            access_token_expires_at=FIXED_TIME + timedelta(minutes=5),
            refresh_token_expires_at=FIXED_TIME + timedelta(minutes=30),
            granted_scopes=("locks",), now=FIXED_TIME,
        )
        if status_needs_reauth:
            service.chaster_connections.mark_needs_reauthorization(user.id, now=FIXED_TIME)
        return user.id

    def test_already_connected_user_gets_a_fresh_authorization_link(self, configured_service: ApplicationService) -> None:
        _complete_onboarding(configured_service)
        self._connect(configured_service)
        result = configured_service.handle_message(_incoming("chaster connect", external_message_id="m1"))
        assert "already have a chaster connection" in result.text.lower()
        assert "https://sso.chaster.app/auth/realms/app/protocol/openid-connect/auth" in result.text
        assert "state=" in result.text

    def test_expired_connection_user_gets_a_fresh_link_mentioning_the_status(self, configured_service: ApplicationService) -> None:
        _complete_onboarding(configured_service)
        self._connect(configured_service, status_needs_reauth=True)
        result = configured_service.handle_message(_incoming("chaster connect", external_message_id="m1"))
        assert "needs_reauthorization" in result.text
        assert "state=" in result.text

    def test_relink_does_not_delete_the_existing_connection_before_the_callback(self, configured_service: ApplicationService) -> None:
        _complete_onboarding(configured_service)
        user_id = self._connect(configured_service)
        configured_service.handle_message(_incoming("chaster connect", external_message_id="m1"))
        assert configured_service.chaster_connections.get(user_id) is not None

    def test_relink_issues_a_new_state_each_time_for_a_connected_user(self, configured_service: ApplicationService) -> None:
        _complete_onboarding(configured_service)
        self._connect(configured_service)
        first = configured_service.handle_message(_incoming("chaster connect", external_message_id="m1"))
        second = configured_service.handle_message(_incoming("chaster connect", external_message_id="m2"))
        assert first.text.split("state=")[1].split()[0] != second.text.split("state=")[1].split()[0]


class TestChasterNotConfigured:
    def test_chaster_connect_gives_a_safe_reply_when_not_configured(self, unconfigured_service: ApplicationService) -> None:
        _complete_onboarding(unconfigured_service)
        result = unconfigured_service.handle_message(_incoming("chaster connect", external_message_id="m1"))
        assert "not configured" in result.text.lower()


class TestIncompleteOnboarding:
    def test_chaster_connect_before_onboarding_is_complete_never_reaches_the_command(
        self, configured_service: ApplicationService,
    ) -> None:
        """Onboarding gates the router entirely -- chaster connect
        cannot be reached before it, exactly like every other command."""
        result = configured_service.handle_message(_incoming("chaster connect", external_message_id="m1"))
        assert "chaster" not in result.text.lower()  # this is an onboarding prompt, not the command's own reply


class TestNoSecretLeakage:
    def test_client_secret_never_appears_in_the_chaster_connect_reply(self, configured_service: ApplicationService) -> None:
        _complete_onboarding(configured_service)
        result = configured_service.handle_message(_incoming("chaster connect", external_message_id="m1"))
        assert "test-secret" not in result.text

    def test_chaster_connect_never_writes_to_working_memory(self, configured_service: ApplicationService) -> None:
        """Deterministic commands never touch Working Memory at all --
        it is written only by a successful Conversation Engine
        exchange, which chaster connect never reaches (confirmed
        separately: it never invokes the model)."""
        _complete_onboarding(configured_service)
        configured_service.handle_message(_incoming("chaster connect", external_message_id="m1"))
        user = configured_service.user_service.get_or_create_user("discord", "42", now=FIXED_TIME)
        snapshot = configured_service._conversation_engine._working_memory_reader.read(subject_key=user.id)
        assert snapshot.turns == ()
