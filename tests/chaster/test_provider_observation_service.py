"""
tests/chaster/test_provider_observation_service.py
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from cryptography.fernet import Fernet

from chaster.lock_client import ChasterLockApiError
from chaster.models import ChasterProviderStatus
from chaster.oauth_client import ChasterTokenExchangeError, ChasterTokenResponse
from chaster.provider_observation_repository import (
    ChasterProviderObservationRecorder,
    ChasterProviderObservations,
)
from chaster.provider_observation_service import (
    ChasterProviderObservationService,
    FetchObservationOutcome,
)
from chaster.repository import ChasterConnectionRepository
from chaster.token_encryptor import TokenEncryptor
from infrastructure.database import Database as CoreDatabase

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
    with c.raw_connection() as conn:
        conn.execute(
            "INSERT INTO user_accounts (id, created_at, last_seen_at) VALUES (?, ?, ?)",
            ("u1", FIXED_TIME.isoformat(), FIXED_TIME.isoformat()),
        )
        conn.commit()
    return c


@pytest.fixture
def encryptor() -> TokenEncryptor:
    return TokenEncryptor(Fernet.generate_key())


@pytest.fixture
def connections(core: CoreDatabase, tmp_path: Path, encryptor: TokenEncryptor) -> ChasterConnectionRepository:
    return ChasterConnectionRepository(tmp_path / "test.db", core=core, encryptor=encryptor)


@pytest.fixture
def observations(core: CoreDatabase, tmp_path: Path) -> ChasterProviderObservations:
    return ChasterProviderObservations(tmp_path / "test.db", core=core)


@pytest.fixture
def recorder(core: CoreDatabase, tmp_path: Path) -> ChasterProviderObservationRecorder:
    return ChasterProviderObservationRecorder(tmp_path / "test.db", core=core)


@pytest.fixture
def oauth_client() -> MagicMock:
    return MagicMock()


@pytest.fixture
def lock_client() -> MagicMock:
    return MagicMock()


@pytest.fixture
def service(connections, oauth_client, lock_client, observations, recorder) -> ChasterProviderObservationService:
    return ChasterProviderObservationService(
        connections=connections, oauth_client=oauth_client, lock_client=lock_client,
        observations=observations, recorder=recorder,
    )


def _create_connection(connections: ChasterConnectionRepository, *, access_token_expires_at=None) -> None:
    connections.create_or_replace(
        user_id="u1", chaster_account_id="acc1", chaster_username="wearer1",
        access_token="real-access-token", refresh_token="real-refresh-token",
        access_token_expires_at=access_token_expires_at or (FIXED_TIME + timedelta(minutes=5)),
        refresh_token_expires_at=FIXED_TIME + timedelta(days=30),
        granted_scopes=("locks",), now=FIXED_TIME,
    )


class TestNoConnection:
    def test_missing_connection_returns_no_connection_outcome(self, service: ChasterProviderObservationService) -> None:
        result = service.fetch_and_record(connection_id="nonexistent", now=FIXED_TIME)
        assert result.outcome is FetchObservationOutcome.NO_CONNECTION
        assert result.observation is None

    def test_missing_connection_never_calls_the_lock_client(self, service: ChasterProviderObservationService, lock_client: MagicMock) -> None:
        service.fetch_and_record(connection_id="nonexistent", now=FIXED_TIME)
        lock_client.list_active_locks.assert_not_called()


class TestSuccessfulFetch:
    def test_records_a_fresh_observation(self, connections, service, lock_client: MagicMock) -> None:
        _create_connection(connections)
        lock_client.list_active_locks.return_value = [{"_id": "lock123", "status": "locked", "lockType": "chastity"}]
        result = service.fetch_and_record(connection_id="u1", now=FIXED_TIME)
        assert result.outcome is FetchObservationOutcome.RECORDED
        assert result.observation.chaster_lock_id == "lock123"
        assert result.observation.status is ChasterProviderStatus.LOCKED

    def test_fetched_at_matches_the_now_the_fetch_actually_happened_at(self, connections, service, lock_client: MagicMock) -> None:
        fetch_time = FIXED_TIME + timedelta(minutes=2)
        _create_connection(connections, access_token_expires_at=fetch_time + timedelta(hours=1))
        lock_client.list_active_locks.return_value = [{"_id": "lock1", "status": "unlocked"}]
        result = service.fetch_and_record(connection_id="u1", now=fetch_time)
        assert result.observation.fetched_at == fetch_time

    def test_uses_the_confirmed_decrypted_access_token(self, connections, service, lock_client: MagicMock) -> None:
        _create_connection(connections)
        lock_client.list_active_locks.return_value = [{"_id": "lock1", "status": "locked"}]
        service.fetch_and_record(connection_id="u1", now=FIXED_TIME)
        lock_client.list_active_locks.assert_called_once_with(access_token="real-access-token")

    def test_unlocked_status_parses_correctly(self, connections, service, lock_client: MagicMock) -> None:
        _create_connection(connections)
        lock_client.list_active_locks.return_value = [{"_id": "lock1", "status": "unlocked"}]
        result = service.fetch_and_record(connection_id="u1", now=FIXED_TIME)
        assert result.observation.status is ChasterProviderStatus.UNLOCKED

    def test_deserted_status_parses_correctly(self, connections, service, lock_client: MagicMock) -> None:
        _create_connection(connections)
        lock_client.list_active_locks.return_value = [{"_id": "lock1", "status": "deserted"}]
        result = service.fetch_and_record(connection_id="u1", now=FIXED_TIME)
        assert result.observation.status is ChasterProviderStatus.DESERTED

    def test_recorded_observation_is_actually_persisted(self, connections, service, lock_client: MagicMock, observations) -> None:
        _create_connection(connections)
        lock_client.list_active_locks.return_value = [{"_id": "lock1", "status": "locked"}]
        result = service.fetch_and_record(connection_id="u1", now=FIXED_TIME)
        assert observations.get_latest("u1") == result.observation


class TestZeroActiveLocks:
    def test_empty_list_records_unlocked_with_no_lock_id(self, connections, service, lock_client: MagicMock) -> None:
        _create_connection(connections)
        lock_client.list_active_locks.return_value = []
        result = service.fetch_and_record(connection_id="u1", now=FIXED_TIME)
        assert result.outcome is FetchObservationOutcome.ZERO_ACTIVE_LOCKS
        assert result.observation.status is ChasterProviderStatus.UNLOCKED
        assert result.observation.chaster_lock_id is None

    def test_zero_locks_is_still_persisted_not_just_returned(self, connections, service, lock_client: MagicMock, observations) -> None:
        _create_connection(connections)
        lock_client.list_active_locks.return_value = []
        service.fetch_and_record(connection_id="u1", now=FIXED_TIME)
        assert observations.get_latest("u1") is not None


class TestAmbiguousMultipleLocks:
    def test_multiple_locks_does_not_record_anything(self, connections, service, lock_client: MagicMock, observations) -> None:
        _create_connection(connections)
        lock_client.list_active_locks.return_value = [
            {"_id": "lock1", "status": "locked"}, {"_id": "lock2", "status": "unlocked"},
        ]
        result = service.fetch_and_record(connection_id="u1", now=FIXED_TIME)
        assert result.outcome is FetchObservationOutcome.AMBIGUOUS_MULTIPLE_LOCKS
        assert observations.get_latest("u1") is None

    def test_multiple_locks_returns_the_last_known_observation_if_one_exists(self, connections, service, lock_client: MagicMock) -> None:
        _create_connection(connections, access_token_expires_at=FIXED_TIME + timedelta(hours=2))
        lock_client.list_active_locks.return_value = [{"_id": "lock1", "status": "locked"}]
        first = service.fetch_and_record(connection_id="u1", now=FIXED_TIME)

        lock_client.list_active_locks.return_value = [
            {"_id": "lock1", "status": "locked"}, {"_id": "lock2", "status": "unlocked"},
        ]
        second = service.fetch_and_record(connection_id="u1", now=FIXED_TIME + timedelta(minutes=5))
        assert second.outcome is FetchObservationOutcome.AMBIGUOUS_MULTIPLE_LOCKS
        assert second.observation == first.observation  # the OLD one, unchanged, with its own real fetched_at


class TestUnparseableStatus:
    def test_unexpected_status_value_does_not_record_or_crash(self, connections, service, lock_client: MagicMock, observations) -> None:
        _create_connection(connections)
        lock_client.list_active_locks.return_value = [{"_id": "lock1", "status": "some-future-unknown-value"}]
        result = service.fetch_and_record(connection_id="u1", now=FIXED_TIME)
        assert result.outcome is FetchObservationOutcome.UNPARSEABLE_STATUS
        assert observations.get_latest("u1") is None

    def test_missing_status_field_entirely_is_also_unparseable(self, connections, service, lock_client: MagicMock) -> None:
        _create_connection(connections)
        lock_client.list_active_locks.return_value = [{"_id": "lock1"}]  # no status key at all
        result = service.fetch_and_record(connection_id="u1", now=FIXED_TIME)
        assert result.outcome is FetchObservationOutcome.UNPARSEABLE_STATUS


class TestApiFailure:
    def test_network_error_returns_api_unavailable_not_unlocked(self, connections, service, lock_client: MagicMock) -> None:
        _create_connection(connections)
        lock_client.list_active_locks.side_effect = ChasterLockApiError("Network error contacting Chaster's locks endpoint.")
        result = service.fetch_and_record(connection_id="u1", now=FIXED_TIME)
        assert result.outcome is FetchObservationOutcome.API_UNAVAILABLE
        assert result.observation is None  # no prior observation existed

    def test_api_failure_after_a_prior_success_returns_the_last_known_observation(self, connections, service, lock_client: MagicMock) -> None:
        _create_connection(connections, access_token_expires_at=FIXED_TIME + timedelta(hours=2))
        lock_client.list_active_locks.return_value = [{"_id": "lock1", "status": "locked"}]
        first = service.fetch_and_record(connection_id="u1", now=FIXED_TIME)

        lock_client.list_active_locks.side_effect = ChasterLockApiError("Chaster's locks endpoint returned HTTP 500.")
        second = service.fetch_and_record(connection_id="u1", now=FIXED_TIME + timedelta(minutes=30))
        assert second.outcome is FetchObservationOutcome.API_UNAVAILABLE
        assert second.observation == first.observation
        # The stale observation's own fetched_at is never rewritten to look fresh.
        assert second.observation.fetched_at == FIXED_TIME

    def test_http_error_status_also_returns_api_unavailable(self, connections, service, lock_client: MagicMock) -> None:
        _create_connection(connections)
        lock_client.list_active_locks.side_effect = ChasterLockApiError("Chaster's locks endpoint returned HTTP 429.")
        result = service.fetch_and_record(connection_id="u1", now=FIXED_TIME)
        assert result.outcome is FetchObservationOutcome.API_UNAVAILABLE


class TestTokenRefresh:
    def test_expired_token_triggers_a_refresh(self, connections, service, oauth_client: MagicMock, lock_client: MagicMock) -> None:
        _create_connection(connections, access_token_expires_at=FIXED_TIME - timedelta(minutes=1))
        oauth_client.refresh_tokens.return_value = ChasterTokenResponse(
            access_token="new-at", refresh_token="new-rt", expires_in=300, refresh_expires_in=2592000, granted_scopes=("locks",),
        )
        lock_client.list_active_locks.return_value = [{"_id": "lock1", "status": "locked"}]
        result = service.fetch_and_record(connection_id="u1", now=FIXED_TIME)
        oauth_client.refresh_tokens.assert_called_once_with(refresh_token="real-refresh-token")
        assert result.outcome is FetchObservationOutcome.RECORDED

    def test_refreshed_token_is_actually_used_for_the_locks_call(self, connections, service, oauth_client: MagicMock, lock_client: MagicMock) -> None:
        _create_connection(connections, access_token_expires_at=FIXED_TIME - timedelta(minutes=1))
        oauth_client.refresh_tokens.return_value = ChasterTokenResponse(
            access_token="brand-new-access-token", refresh_token="new-rt", expires_in=300, refresh_expires_in=2592000, granted_scopes=("locks",),
        )
        lock_client.list_active_locks.return_value = [{"_id": "lock1", "status": "locked"}]
        service.fetch_and_record(connection_id="u1", now=FIXED_TIME)
        lock_client.list_active_locks.assert_called_once_with(access_token="brand-new-access-token")

    def test_valid_token_does_not_trigger_a_refresh(self, connections, service, oauth_client: MagicMock, lock_client: MagicMock) -> None:
        _create_connection(connections, access_token_expires_at=FIXED_TIME + timedelta(minutes=30))
        lock_client.list_active_locks.return_value = [{"_id": "lock1", "status": "locked"}]
        service.fetch_and_record(connection_id="u1", now=FIXED_TIME)
        oauth_client.refresh_tokens.assert_not_called()

    def test_revoked_refresh_token_returns_needs_reauthorization(self, connections, service, oauth_client: MagicMock) -> None:
        _create_connection(connections, access_token_expires_at=FIXED_TIME - timedelta(minutes=1))
        oauth_client.refresh_tokens.side_effect = ChasterTokenExchangeError("Chaster's token endpoint returned HTTP 400.")
        result = service.fetch_and_record(connection_id="u1", now=FIXED_TIME)
        assert result.outcome is FetchObservationOutcome.NEEDS_REAUTHORIZATION

    def test_revoked_refresh_token_marks_the_connection_needing_reauthorization(self, connections, service, oauth_client: MagicMock) -> None:
        from chaster.models import ConnectionStatus
        _create_connection(connections, access_token_expires_at=FIXED_TIME - timedelta(minutes=1))
        oauth_client.refresh_tokens.side_effect = ChasterTokenExchangeError("HTTP 400.")
        service.fetch_and_record(connection_id="u1", now=FIXED_TIME)
        assert connections.get("u1").connection_status is ConnectionStatus.NEEDS_REAUTHORIZATION

    def test_failed_refresh_never_calls_the_lock_client(self, connections, service, oauth_client: MagicMock, lock_client: MagicMock) -> None:
        _create_connection(connections, access_token_expires_at=FIXED_TIME - timedelta(minutes=1))
        oauth_client.refresh_tokens.side_effect = ChasterTokenExchangeError("HTTP 400.")
        service.fetch_and_record(connection_id="u1", now=FIXED_TIME)
        lock_client.list_active_locks.assert_not_called()

    def test_failed_refresh_returns_the_last_known_observation(self, connections, service, oauth_client: MagicMock, lock_client: MagicMock) -> None:
        _create_connection(connections)
        lock_client.list_active_locks.return_value = [{"_id": "lock1", "status": "locked"}]
        first = service.fetch_and_record(connection_id="u1", now=FIXED_TIME)

        oauth_client.refresh_tokens.side_effect = ChasterTokenExchangeError("HTTP 400.")
        connections.create_or_replace(
            user_id="u1", chaster_account_id="acc1", chaster_username="wearer1",
            access_token="real-access-token", refresh_token="real-refresh-token",
            access_token_expires_at=FIXED_TIME - timedelta(minutes=1), refresh_token_expires_at=FIXED_TIME + timedelta(days=30),
            granted_scopes=("locks",), now=FIXED_TIME,
        )
        second = service.fetch_and_record(connection_id="u1", now=FIXED_TIME + timedelta(hours=1))
        assert second.outcome is FetchObservationOutcome.NEEDS_REAUTHORIZATION
        assert second.observation == first.observation


class TestSelfHealing:
    """The service delegates to the shared ChasterTokenManager."""

    def test_a_401_triggers_one_refresh_and_a_successful_retry(self, connections, service, oauth_client, lock_client) -> None:
        _create_connection(connections)
        oauth_client.refresh_tokens.return_value = ChasterTokenResponse(
            access_token="new-access", refresh_token="new-refresh", expires_in=300,
            refresh_expires_in=1800, granted_scopes=("locks",),
        )
        lock_client.list_active_locks.side_effect = [
            ChasterLockApiError("401", status_code=401),
            [{"_id": "lock1", "status": "locked"}],
        ]
        result = service.fetch_and_record(connection_id="u1", now=FIXED_TIME)
        assert result.outcome == FetchObservationOutcome.RECORDED
        assert lock_client.list_active_locks.call_args_list[1].kwargs["access_token"] == "new-access"
        oauth_client.refresh_tokens.assert_called_once()

    def test_a_401_with_a_revoked_refresh_token_returns_needs_reauthorization(self, connections, service, oauth_client, lock_client) -> None:
        _create_connection(connections)
        oauth_client.refresh_tokens.side_effect = ChasterTokenExchangeError("rejected", status_code=400)
        lock_client.list_active_locks.side_effect = ChasterLockApiError("401", status_code=401)
        result = service.fetch_and_record(connection_id="u1", now=FIXED_TIME)
        assert result.outcome == FetchObservationOutcome.NEEDS_REAUTHORIZATION

    def test_a_transient_refresh_failure_is_api_unavailable_and_does_not_mark_the_connection(self, connections, service, oauth_client, lock_client) -> None:
        _create_connection(connections, access_token_expires_at=FIXED_TIME - timedelta(minutes=1))
        oauth_client.refresh_tokens.side_effect = ChasterTokenExchangeError("network", transient=True)
        result = service.fetch_and_record(connection_id="u1", now=FIXED_TIME)
        assert result.outcome == FetchObservationOutcome.API_UNAVAILABLE
        assert connections.get("u1").connection_status.value == "active"
        lock_client.list_active_locks.assert_not_called()

    def test_a_connection_already_marked_needs_reauthorization_makes_no_network_call(self, connections, service, oauth_client, lock_client) -> None:
        _create_connection(connections)
        connections.mark_needs_reauthorization("u1", now=FIXED_TIME)
        result = service.fetch_and_record(connection_id="u1", now=FIXED_TIME)
        assert result.outcome == FetchObservationOutcome.NEEDS_REAUTHORIZATION
        lock_client.list_active_locks.assert_not_called()
        oauth_client.refresh_tokens.assert_not_called()


class TestNeverImportsRestrictedModules:
    def test_does_not_import_task_runtime_eligibility(self) -> None:
        import inspect

        import chaster.provider_observation_service as module
        import_lines = "\n".join(ln for ln in inspect.getsource(module).splitlines() if ln.strip().startswith(("import ", "from ")))
        assert "task_runtime" not in import_lines

    def test_does_not_import_lock_state(self) -> None:
        import inspect

        import chaster.provider_observation_service as module
        import_lines = "\n".join(ln for ln in inspect.getsource(module).splitlines() if ln.strip().startswith(("import ", "from ")))
        assert "lock_state" not in import_lines

    def test_does_not_import_conversation_engine(self) -> None:
        import inspect

        import chaster.provider_observation_service as module
        import_lines = "\n".join(ln for ln in inspect.getsource(module).splitlines() if ln.strip().startswith(("import ", "from ")))
        assert "conversation_engine" not in import_lines

    def test_no_threading_asyncio_or_timer_usage(self) -> None:
        """Checks actual import statements only -- this module's own
        docstring legitimately explains what it deliberately does NOT
        use, which is not itself a usage."""
        import inspect

        import chaster.provider_observation_service as module
        import_lines = "\n".join(ln for ln in inspect.getsource(module).splitlines() if ln.strip().startswith(("import ", "from ")))
        for forbidden in ("threading", "asyncio"):
            assert forbidden not in import_lines
