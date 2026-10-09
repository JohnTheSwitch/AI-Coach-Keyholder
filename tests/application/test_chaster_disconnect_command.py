"""
tests/application/test_chaster_disconnect_command.py

`chaster disconnect` -- deterministic local removal of the user's stored
Chaster connection, answered with a colour-coded embed. Goes through
ApplicationService.handle_message() and the real CommandRouter.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from application.models import IncomingMessage
from application.service import ApplicationService
from chaster.models import ChasterProviderStatus
from chaster.oauth_client import ChasterOAuthClient
from chaster.provider_observation_repository import ChasterProviderObservationRecorder
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
    def __init__(self) -> None:
        self.calls: list[ModelGenerationRequest] = []

    def generate(self, *, request: ModelGenerationRequest) -> str:
        self.calls.append(request)
        return "a plain conversational reply"


@pytest.fixture
def model() -> _RecordingModel:
    return _RecordingModel()


@pytest.fixture
def configured_service(core: CoreDatabase, tmp_path: Path, model: _RecordingModel) -> ApplicationService:
    buffer = InMemoryWorkingMemory(max_exchanges_per_subject=10, max_characters_per_subject=8000)
    engine = ConversationEngine(model=model, working_memory_reader=buffer, working_memory_writer=buffer, queue=SubjectConversationQueue())
    return ApplicationService(
        core.db_path, core=core, conversation_engine=engine,
        chaster_connections=ChasterConnectionRepository(tmp_path / "test.db", core=core, encryptor=TokenEncryptor(Fernet.generate_key())),
        chaster_oauth_client=ChasterOAuthClient(client_id="cid", client_secret="secret", redirect_uri="https://example.com/cb"),
    )


@pytest.fixture
def unconfigured_service(core: CoreDatabase) -> ApplicationService:
    return ApplicationService(core.db_path, core=core)


def _incoming(text: str, *, external_user_id: str = "42", external_message_id: str | None = None) -> IncomingMessage:
    return IncomingMessage(channel="discord", external_user_id=external_user_id, text=text, received_at=FIXED_TIME, external_message_id=external_message_id)


def _complete_onboarding(service: ApplicationService, *, external_user_id: str = "42") -> None:
    service.handle_message(_incoming("anything", external_user_id=external_user_id, external_message_id="ob0"))
    service.handle_message(_incoming("english", external_user_id=external_user_id, external_message_id="ob1"))
    service.handle_message(_incoming("neutral", external_user_id=external_user_id, external_message_id="ob2"))
    service.handle_message(_incoming("alex", external_user_id=external_user_id, external_message_id="ob3"))


def _connect(service: ApplicationService, *, external_user_id: str = "42") -> str:
    user = service.user_service.get_or_create_user("discord", external_user_id, now=FIXED_TIME)
    service.chaster_connections.create_or_replace(
        user_id=user.id, chaster_account_id=f"acc-{external_user_id}", chaster_username="wearer1",
        access_token="real-access-token", refresh_token="real-refresh-token",
        access_token_expires_at=FIXED_TIME + timedelta(hours=1),
        refresh_token_expires_at=FIXED_TIME + timedelta(days=30),
        granted_scopes=("locks",), now=FIXED_TIME,
    )
    return user.id


class TestDisconnect:
    def test_never_invokes_the_model(self, configured_service, model) -> None:
        _complete_onboarding(configured_service)
        configured_service.handle_message(_incoming("chaster disconnect", external_message_id="m1"))
        assert model.calls == []

    def test_connected_user_gets_a_green_embed(self, configured_service) -> None:
        _complete_onboarding(configured_service)
        _connect(configured_service)
        result = configured_service.handle_message(_incoming("chaster disconnect", external_message_id="m1"))
        assert result.embed is not None
        assert result.embed.title == "Chaster Disconnected"
        assert result.embed.color == "green"
        assert "disconnected" in result.text.lower()

    def test_the_connection_is_actually_removed(self, configured_service) -> None:
        _complete_onboarding(configured_service)
        user_id = _connect(configured_service)
        configured_service.handle_message(_incoming("chaster disconnect", external_message_id="m1"))
        assert configured_service.chaster_connections.get(user_id) is None

    def test_user_with_provider_observations_can_disconnect(self, configured_service, core, tmp_path) -> None:
        """Regression for the foreign key: observations reference the connection."""
        _complete_onboarding(configured_service)
        user_id = _connect(configured_service)
        ChasterProviderObservationRecorder(tmp_path / "test.db", core=core).record(
            connection_id=user_id, provider="chaster", chaster_lock_id="lock1",
            status=ChasterProviderStatus.LOCKED, fetched_at=FIXED_TIME, now=FIXED_TIME,
        )
        result = configured_service.handle_message(_incoming("chaster disconnect", external_message_id="m1"))
        assert result.embed.color == "green"
        assert configured_service.chaster_connections.get(user_id) is None

    def test_user_without_a_connection_gets_a_blue_embed(self, configured_service) -> None:
        _complete_onboarding(configured_service)
        result = configured_service.handle_message(_incoming("chaster disconnect", external_message_id="m1"))
        assert result.embed.title == "Nothing to Disconnect"
        assert result.embed.color == "blue"

    def test_disconnect_is_idempotent(self, configured_service) -> None:
        _complete_onboarding(configured_service)
        _connect(configured_service)
        first = configured_service.handle_message(_incoming("chaster disconnect", external_message_id="m1"))
        second = configured_service.handle_message(_incoming("chaster disconnect", external_message_id="m2"))
        assert first.embed.color == "green"
        assert second.embed.color == "blue"

    def test_only_the_requesting_users_connection_is_removed(self, configured_service) -> None:
        _complete_onboarding(configured_service, external_user_id="42")
        _complete_onboarding(configured_service, external_user_id="99")
        _connect(configured_service, external_user_id="42")
        other_id = _connect(configured_service, external_user_id="99")
        configured_service.handle_message(_incoming("chaster disconnect", external_user_id="42", external_message_id="m1"))
        assert configured_service.chaster_connections.get(other_id) is not None

    def test_status_after_disconnect_says_not_connected(self, configured_service) -> None:
        _complete_onboarding(configured_service)
        _connect(configured_service)
        configured_service.handle_message(_incoming("chaster disconnect", external_message_id="m1"))
        result = configured_service.handle_message(_incoming("chaster status", external_message_id="m2"))
        assert result.embed.title == "Chaster Account Not Connected"

    def test_connect_after_disconnect_issues_a_plain_link(self, configured_service) -> None:
        _complete_onboarding(configured_service)
        _connect(configured_service)
        configured_service.handle_message(_incoming("chaster disconnect", external_message_id="m1"))
        result = configured_service.handle_message(_incoming("chaster connect", external_message_id="m2"))
        assert "state=" in result.text
        assert "already have" not in result.text.lower()

    def test_disconnect_kills_an_outstanding_authorization_link(self, configured_service, core) -> None:
        _complete_onboarding(configured_service)
        _connect(configured_service)
        link = configured_service.handle_message(_incoming("chaster connect", external_message_id="m1"))
        state = link.text.split("state=")[1].split()[0]
        configured_service.handle_message(_incoming("chaster disconnect", external_message_id="m2"))
        assert configured_service.chaster_states.consume(state=state, now=FIXED_TIME).matched is False

    def test_reply_never_leaks_tokens(self, configured_service) -> None:
        _complete_onboarding(configured_service)
        _connect(configured_service)
        result = configured_service.handle_message(_incoming("chaster disconnect", external_message_id="m1"))
        blob = result.text + repr(result.embed)
        assert "real-access-token" not in blob and "real-refresh-token" not in blob

    def test_reply_is_honest_that_chaster_is_not_notified(self, configured_service) -> None:
        _complete_onboarding(configured_service)
        _connect(configured_service)
        result = configured_service.handle_message(_incoming("chaster disconnect", external_message_id="m1"))
        assert "does not notify chaster" in result.embed.description.lower()

    def test_makes_no_network_calls(self, configured_service) -> None:
        from unittest.mock import patch
        _complete_onboarding(configured_service)
        _connect(configured_service)
        with patch("requests.Session.send", side_effect=AssertionError("network call attempted")):
            configured_service.handle_message(_incoming("chaster disconnect", external_message_id="m1"))

    def test_not_configured_gives_a_safe_reply(self, unconfigured_service) -> None:
        _complete_onboarding(unconfigured_service)
        result = unconfigured_service.handle_message(_incoming("chaster disconnect", external_message_id="m1"))
        assert "not configured" in result.text.lower()

    def test_disconnect_before_onboarding_never_reaches_the_command(self, configured_service) -> None:
        result = configured_service.handle_message(_incoming("chaster disconnect", external_message_id="m1"))
        assert result.embed is None


class TestInvalidChasterCommandHelp:
    def test_the_family_fallback_lists_all_three_commands(self, configured_service) -> None:
        _complete_onboarding(configured_service)
        result = configured_service.handle_message(_incoming("chaster", external_message_id="m1"))
        for command in ("chaster connect", "chaster status", "chaster disconnect"):
            assert command in result.text
