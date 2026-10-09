"""
tests/chaster/test_token_manager.py

ChasterTokenManager: pre-call refresh, single 401 refresh-and-retry,
honest failure classification, and concurrency. All Chaster traffic is
mocked -- no network.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from cryptography.fernet import Fernet

from chaster.lock_client import ChasterLockApiError
from chaster.models import ConnectionStatus
from chaster.oauth_client import ChasterTokenExchangeError, ChasterTokenResponse
from chaster.repository import ChasterConnectionRepository
from chaster.token_encryptor import TokenEncryptor
from chaster.token_manager import (
    ChasterNotConnectedError,
    ChasterReauthorizationRequiredError,
    ChasterRefreshUnavailableError,
    ChasterTokenManager,
)
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
def connections(core: CoreDatabase, tmp_path: Path) -> ChasterConnectionRepository:
    return ChasterConnectionRepository(tmp_path / "test.db", core=core, encryptor=TokenEncryptor(Fernet.generate_key()))


@pytest.fixture
def oauth_client() -> MagicMock:
    client = MagicMock()
    client.refresh_tokens.return_value = _tokens("new-access", "new-refresh")
    return client


@pytest.fixture
def manager(connections, oauth_client) -> ChasterTokenManager:
    return ChasterTokenManager(connections=connections, oauth_client=oauth_client)


def _tokens(access: str, refresh: str, *, expires_in: int = 300, refresh_expires_in: int = 1800) -> ChasterTokenResponse:
    return ChasterTokenResponse(
        access_token=access, refresh_token=refresh, expires_in=expires_in,
        refresh_expires_in=refresh_expires_in, granted_scopes=("locks",),
    )


def _connect(connections: ChasterConnectionRepository, *, expires_in: timedelta = timedelta(hours=1)) -> None:
    connections.create_or_replace(
        user_id="u1", chaster_account_id="acc1", chaster_username="wearer1",
        access_token="old-access", refresh_token="old-refresh",
        access_token_expires_at=FIXED_TIME + expires_in,
        refresh_token_expires_at=FIXED_TIME + timedelta(days=30),
        granted_scopes=("locks",), now=FIXED_TIME,
    )


class TestPreCallRefresh:
    def test_valid_token_is_returned_without_refreshing(self, connections, manager, oauth_client) -> None:
        _connect(connections)
        assert manager.get_valid_access_token("u1", now=FIXED_TIME) == "old-access"
        oauth_client.refresh_tokens.assert_not_called()

    def test_expired_token_is_refreshed_and_the_new_token_returned(self, connections, manager, oauth_client) -> None:
        _connect(connections, expires_in=timedelta(minutes=-1))
        assert manager.get_valid_access_token("u1", now=FIXED_TIME) == "new-access"
        oauth_client.refresh_tokens.assert_called_once_with(refresh_token="old-refresh")

    def test_token_within_the_expiry_skew_is_refreshed(self, connections, manager, oauth_client) -> None:
        _connect(connections, expires_in=timedelta(seconds=10))
        assert manager.get_valid_access_token("u1", now=FIXED_TIME) == "new-access"

    def test_refreshed_tokens_and_expiries_are_persisted(self, connections, manager) -> None:
        _connect(connections, expires_in=timedelta(minutes=-1))
        manager.get_valid_access_token("u1", now=FIXED_TIME)
        stored = connections.get("u1")
        assert stored.access_token_expires_at == FIXED_TIME + timedelta(seconds=300)
        assert stored.refresh_token_expires_at == FIXED_TIME + timedelta(seconds=1800)
        assert connections.get_decrypted_refresh_token("u1", now=FIXED_TIME) == "new-refresh"
        assert stored.connection_status == ConnectionStatus.ACTIVE

    def test_account_identity_is_preserved_across_a_refresh(self, connections, manager) -> None:
        _connect(connections, expires_in=timedelta(minutes=-1))
        manager.get_valid_access_token("u1", now=FIXED_TIME)
        stored = connections.get("u1")
        assert stored.chaster_account_id == "acc1"
        assert stored.chaster_username == "wearer1"

    def test_a_zero_refresh_expiry_from_the_server_is_not_treated_as_expired(self, connections, oauth_client) -> None:
        """Keycloak offline tokens report refresh_expires_in=0; the
        manager must never pre-judge the refresh token locally."""
        _connect(connections, expires_in=timedelta(minutes=-1))
        connections.create_or_replace(
            user_id="u1", chaster_account_id="acc1", chaster_username="wearer1",
            access_token="old-access", refresh_token="old-refresh",
            access_token_expires_at=FIXED_TIME - timedelta(minutes=1),
            refresh_token_expires_at=FIXED_TIME - timedelta(days=1),
            granted_scopes=("locks",), now=FIXED_TIME,
        )
        manager = ChasterTokenManager(connections=connections, oauth_client=oauth_client)
        assert manager.get_valid_access_token("u1", now=FIXED_TIME) == "new-access"
        oauth_client.refresh_tokens.assert_called_once()


class TestFailureClassification:
    def test_no_connection_raises_not_connected(self, manager) -> None:
        with pytest.raises(ChasterNotConnectedError):
            manager.get_valid_access_token("u1", now=FIXED_TIME)

    def test_not_connected_is_a_lookup_error(self) -> None:
        assert issubclass(ChasterNotConnectedError, LookupError)

    def test_rejected_refresh_token_marks_reauthorization_and_raises(self, connections, manager, oauth_client) -> None:
        _connect(connections, expires_in=timedelta(minutes=-1))
        oauth_client.refresh_tokens.side_effect = ChasterTokenExchangeError("rejected", status_code=400)
        with pytest.raises(ChasterReauthorizationRequiredError):
            manager.get_valid_access_token("u1", now=FIXED_TIME)
        assert connections.get("u1").connection_status == ConnectionStatus.NEEDS_REAUTHORIZATION

    def test_transient_refresh_failure_keeps_the_connection_active(self, connections, manager, oauth_client) -> None:
        _connect(connections, expires_in=timedelta(minutes=-1))
        oauth_client.refresh_tokens.side_effect = ChasterTokenExchangeError("network", transient=True)
        with pytest.raises(ChasterRefreshUnavailableError):
            manager.get_valid_access_token("u1", now=FIXED_TIME)
        assert connections.get("u1").connection_status == ConnectionStatus.ACTIVE
        assert connections.get_decrypted_refresh_token("u1", now=FIXED_TIME) == "old-refresh"

    def test_transient_failure_then_success_self_heals_on_the_next_call(self, connections, manager, oauth_client) -> None:
        _connect(connections, expires_in=timedelta(minutes=-1))
        oauth_client.refresh_tokens.side_effect = [
            ChasterTokenExchangeError("5xx", status_code=503, transient=True),
            _tokens("new-access", "new-refresh"),
        ]
        with pytest.raises(ChasterRefreshUnavailableError):
            manager.get_valid_access_token("u1", now=FIXED_TIME)
        assert manager.get_valid_access_token("u1", now=FIXED_TIME) == "new-access"

    def test_needs_reauthorization_status_short_circuits_without_any_network_call(self, connections, manager, oauth_client) -> None:
        _connect(connections, expires_in=timedelta(minutes=-1))
        connections.mark_needs_reauthorization("u1", now=FIXED_TIME)
        with pytest.raises(ChasterReauthorizationRequiredError):
            manager.get_valid_access_token("u1", now=FIXED_TIME)
        oauth_client.refresh_tokens.assert_not_called()

    def test_decryption_failure_raises_reauthorization_required(self, core, tmp_path, oauth_client) -> None:
        writer = ChasterConnectionRepository(tmp_path / "test.db", core=core, encryptor=TokenEncryptor(Fernet.generate_key()))
        _connect(writer)
        other_key_repo = ChasterConnectionRepository(tmp_path / "test.db", core=core, encryptor=TokenEncryptor(Fernet.generate_key()))
        manager = ChasterTokenManager(connections=other_key_repo, oauth_client=oauth_client)
        with pytest.raises(ChasterReauthorizationRequiredError):
            manager.get_valid_access_token("u1", now=FIXED_TIME)
        assert other_key_repo.get("u1").connection_status == ConnectionStatus.NEEDS_REAUTHORIZATION

    def test_error_messages_never_contain_token_values(self, connections, manager, oauth_client) -> None:
        _connect(connections, expires_in=timedelta(minutes=-1))
        oauth_client.refresh_tokens.side_effect = ChasterTokenExchangeError("rejected", status_code=400)
        with pytest.raises(ChasterReauthorizationRequiredError) as excinfo:
            manager.get_valid_access_token("u1", now=FIXED_TIME)
        assert "old-refresh" not in str(excinfo.value)
        assert "old-access" not in str(excinfo.value)


class TestCallWithToken:
    def test_success_passes_the_token_and_returns_the_result(self, connections, manager, oauth_client) -> None:
        _connect(connections)
        operation = MagicMock(return_value=["lock"])
        assert manager.call_with_token("u1", now=FIXED_TIME, operation=operation) == ["lock"]
        operation.assert_called_once_with("old-access")
        oauth_client.refresh_tokens.assert_not_called()

    def test_401_refreshes_once_and_retries_once_with_the_new_token(self, connections, manager, oauth_client) -> None:
        _connect(connections)
        operation = MagicMock(side_effect=[ChasterLockApiError("401", status_code=401), ["lock"]])
        assert manager.call_with_token("u1", now=FIXED_TIME, operation=operation) == ["lock"]
        assert [c.args[0] for c in operation.call_args_list] == ["old-access", "new-access"]
        oauth_client.refresh_tokens.assert_called_once()
        assert connections.get_decrypted_access_token("u1", now=FIXED_TIME) == "new-access"

    def test_a_second_401_is_terminal_and_marks_reauthorization(self, connections, manager, oauth_client) -> None:
        _connect(connections)
        operation = MagicMock(side_effect=ChasterLockApiError("401", status_code=401))
        with pytest.raises(ChasterReauthorizationRequiredError):
            manager.call_with_token("u1", now=FIXED_TIME, operation=operation)
        assert operation.call_count == 2          # never a loop
        assert oauth_client.refresh_tokens.call_count == 1
        assert connections.get("u1").connection_status == ConnectionStatus.NEEDS_REAUTHORIZATION

    def test_401_with_a_revoked_refresh_token_raises_reauthorization_required(self, connections, manager, oauth_client) -> None:
        _connect(connections)
        oauth_client.refresh_tokens.side_effect = ChasterTokenExchangeError("rejected", status_code=400)
        operation = MagicMock(side_effect=ChasterLockApiError("401", status_code=401))
        with pytest.raises(ChasterReauthorizationRequiredError):
            manager.call_with_token("u1", now=FIXED_TIME, operation=operation)
        assert operation.call_count == 1
        assert connections.get("u1").connection_status == ConnectionStatus.NEEDS_REAUTHORIZATION

    def test_401_with_a_transient_refresh_failure_keeps_the_connection_active(self, connections, manager, oauth_client) -> None:
        _connect(connections)
        oauth_client.refresh_tokens.side_effect = ChasterTokenExchangeError("network", transient=True)
        operation = MagicMock(side_effect=ChasterLockApiError("401", status_code=401))
        with pytest.raises(ChasterRefreshUnavailableError):
            manager.call_with_token("u1", now=FIXED_TIME, operation=operation)
        assert connections.get("u1").connection_status == ConnectionStatus.ACTIVE

    @pytest.mark.parametrize("status_code", [None, 400, 403, 404, 429, 500, 503])
    def test_non_401_errors_propagate_without_any_refresh(self, connections, manager, oauth_client, status_code) -> None:
        _connect(connections)
        error = ChasterLockApiError("boom", status_code=status_code)
        operation = MagicMock(side_effect=error)
        with pytest.raises(ChasterLockApiError) as excinfo:
            manager.call_with_token("u1", now=FIXED_TIME, operation=operation)
        assert excinfo.value is error
        oauth_client.refresh_tokens.assert_not_called()
        assert operation.call_count == 1

    def test_expired_token_is_refreshed_before_the_call_so_no_401_round_trip_is_needed(self, connections, manager, oauth_client) -> None:
        _connect(connections, expires_in=timedelta(minutes=-1))
        operation = MagicMock(return_value=[])
        manager.call_with_token("u1", now=FIXED_TIME, operation=operation)
        operation.assert_called_once_with("new-access")

    def test_not_connected_never_calls_the_operation(self, manager) -> None:
        operation = MagicMock()
        with pytest.raises(ChasterNotConnectedError):
            manager.call_with_token("u1", now=FIXED_TIME, operation=operation)
        operation.assert_not_called()


class TestConcurrency:
    def test_concurrent_callers_trigger_exactly_one_refresh(self, connections, oauth_client) -> None:
        _connect(connections, expires_in=timedelta(minutes=-1))
        calls = []

        def slow_refresh(*, refresh_token: str):
            calls.append(refresh_token)
            time.sleep(0.2)
            return _tokens("new-access", "new-refresh")

        oauth_client.refresh_tokens.side_effect = slow_refresh
        manager = ChasterTokenManager(connections=connections, oauth_client=oauth_client)
        results: list[str] = []

        def worker() -> None:
            results.append(manager.get_valid_access_token("u1", now=FIXED_TIME))

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(calls) == 1
        assert results == ["new-access"] * 4

    def test_a_stale_401_does_not_trigger_a_second_refresh_when_another_caller_already_refreshed(self, connections, manager, oauth_client) -> None:
        _connect(connections)
        # Another caller refreshes first, replacing the token this call used.
        connections.create_or_replace(
            user_id="u1", chaster_account_id="acc1", chaster_username="wearer1",
            access_token="already-refreshed", refresh_token="rotated-refresh",
            access_token_expires_at=FIXED_TIME + timedelta(hours=1),
            refresh_token_expires_at=FIXED_TIME + timedelta(days=30),
            granted_scopes=("locks",), now=FIXED_TIME,
        )
        manager._refresh("u1", now=FIXED_TIME, rejected_token="old-access")
        oauth_client.refresh_tokens.assert_not_called()


class TestStructure:
    def test_no_datetime_now_in_the_module(self) -> None:
        source = (Path(__file__).parent.parent.parent / "chaster" / "token_manager.py").read_text(encoding="utf-8")
        assert "datetime.now(" not in source
        assert "utcnow(" not in source

    def test_module_never_touches_the_emergency_unlock_plane(self) -> None:
        source = (Path(__file__).parent.parent.parent / "chaster" / "token_manager.py").read_text(encoding="utf-8")
        import_lines = [l for l in source.splitlines() if l.startswith(("import ", "from "))]
        assert not any("emergency" in l for l in import_lines)
        assert not any("lock_state" in l or "task_runtime" in l for l in import_lines)
