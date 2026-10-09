"""
tests/chaster/test_connection_repository.py
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from chaster.models import ConnectionStatus
from chaster.repository import ChasterConnectionRepository
from chaster.token_encryptor import TokenDecryptionError, TokenEncryptor
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
        for uid in ("u1", "u2"):
            conn.execute(
                "INSERT INTO user_accounts (id, created_at, last_seen_at) VALUES (?, ?, ?)",
                (uid, FIXED_TIME.isoformat(), FIXED_TIME.isoformat()),
            )
        conn.commit()
    return c


@pytest.fixture
def encryptor() -> TokenEncryptor:
    return TokenEncryptor(Fernet.generate_key())


@pytest.fixture
def repo(core: CoreDatabase, tmp_path: Path, encryptor: TokenEncryptor) -> ChasterConnectionRepository:
    return ChasterConnectionRepository(tmp_path / "test.db", core=core, encryptor=encryptor)


def _create(repo: ChasterConnectionRepository, *, user_id: str = "u1", chaster_account_id: str = "chaster-acc-1", **overrides):
    kwargs = dict(
        user_id=user_id, chaster_account_id=chaster_account_id, chaster_username="wearer1",
        access_token="real-access-token", refresh_token="real-refresh-token",
        access_token_expires_at=FIXED_TIME + timedelta(minutes=5),
        refresh_token_expires_at=FIXED_TIME + timedelta(minutes=30),
        granted_scopes=("locks",), now=FIXED_TIME,
    )
    kwargs.update(overrides)
    return repo.create_or_replace(**kwargs)


class TestCreateAndRead:
    def test_create_then_get_round_trips_all_fields(self, repo: ChasterConnectionRepository) -> None:
        created = _create(repo)
        fetched = repo.get("u1")
        assert fetched is not None
        assert fetched.user_id == "u1"
        assert fetched.chaster_account_id == "chaster-acc-1"
        assert fetched.chaster_username == "wearer1"
        assert fetched.connection_status == ConnectionStatus.ACTIVE
        assert fetched.granted_scopes == ("locks",)
        assert fetched.encryption_key_version is None

    def test_get_for_nonexistent_user_returns_none(self, repo: ChasterConnectionRepository) -> None:
        assert repo.get("u1") is None

    def test_encrypted_columns_are_not_the_plaintext(self, repo: ChasterConnectionRepository) -> None:
        _create(repo)
        connection = repo.get("u1")
        assert connection.encrypted_access_token != "real-access-token"
        assert connection.encrypted_refresh_token != "real-refresh-token"
        assert "real-access-token" not in connection.encrypted_access_token
        assert "real-refresh-token" not in connection.encrypted_refresh_token

    def test_plaintext_never_appears_in_the_raw_stored_row(
        self, repo: ChasterConnectionRepository, core: CoreDatabase,
    ) -> None:
        """Reads the RAW SQLite row directly, bypassing the repository
        entirely, to prove the plaintext genuinely never reached disk."""
        _create(repo, access_token="UNIQUE-MARKER-ACCESS-TOKEN", refresh_token="UNIQUE-MARKER-REFRESH-TOKEN")
        with core.raw_connection() as conn:
            row = conn.execute("SELECT * FROM chaster_connections WHERE user_id = ?", ("u1",)).fetchone()
        raw_text = " ".join(str(v) for v in dict(row).values())
        assert "UNIQUE-MARKER-ACCESS-TOKEN" not in raw_text
        assert "UNIQUE-MARKER-REFRESH-TOKEN" not in raw_text


class TestDecryptionAtUseBoundary:
    def test_get_decrypted_access_token_returns_the_original_plaintext(self, repo: ChasterConnectionRepository) -> None:
        _create(repo, access_token="the-real-access-token")
        assert repo.get_decrypted_access_token("u1", now=FIXED_TIME) == "the-real-access-token"

    def test_get_decrypted_refresh_token_returns_the_original_plaintext(self, repo: ChasterConnectionRepository) -> None:
        _create(repo, refresh_token="the-real-refresh-token")
        assert repo.get_decrypted_refresh_token("u1", now=FIXED_TIME) == "the-real-refresh-token"

    def test_decrypting_for_a_nonexistent_user_raises(self, repo: ChasterConnectionRepository) -> None:
        with pytest.raises(LookupError):
            repo.get_decrypted_access_token("u1", now=FIXED_TIME)


class TestUniqueConstraints:
    def test_one_connection_per_user_reconnect_replaces_not_accumulates(self, repo: ChasterConnectionRepository) -> None:
        _create(repo, chaster_account_id="chaster-acc-1", access_token="first-token")
        _create(repo, chaster_account_id="chaster-acc-1", access_token="second-token")
        connection = repo.get("u1")
        assert repo.get_decrypted_access_token("u1", now=FIXED_TIME) == "second-token"

    def test_same_chaster_account_cannot_belong_to_two_different_users(self, repo: ChasterConnectionRepository) -> None:
        _create(repo, user_id="u1", chaster_account_id="chaster-acc-shared")
        with pytest.raises(Exception):  # sqlite3.IntegrityError -- UNIQUE(chaster_account_id)
            _create(repo, user_id="u2", chaster_account_id="chaster-acc-shared")


class TestStatusTransitions:
    def test_new_connection_is_active(self, repo: ChasterConnectionRepository) -> None:
        _create(repo)
        assert repo.get("u1").connection_status == ConnectionStatus.ACTIVE

    def test_mark_needs_reauthorization_transitions_status(self, repo: ChasterConnectionRepository) -> None:
        _create(repo)
        repo.mark_needs_reauthorization("u1", now=FIXED_TIME)
        assert repo.get("u1").connection_status == ConnectionStatus.NEEDS_REAUTHORIZATION

    def test_corrupted_ciphertext_triggers_needs_reauthorization_on_decrypt_attempt(
        self, repo: ChasterConnectionRepository, core: CoreDatabase,
    ) -> None:
        _create(repo)
        with core.raw_connection() as conn:
            conn.execute(
                "UPDATE chaster_connections SET encrypted_access_token = 'corrupted-not-a-fernet-token' WHERE user_id = ?",
                ("u1",),
            )
            conn.commit()
        with pytest.raises(TokenDecryptionError):
            repo.get_decrypted_access_token("u1", now=FIXED_TIME)
        assert repo.get("u1").connection_status == ConnectionStatus.NEEDS_REAUTHORIZATION


class TestDelete:
    def test_delete_removes_the_connection_entirely(self, repo: ChasterConnectionRepository) -> None:
        _create(repo)
        repo.delete("u1")
        assert repo.get("u1") is None

    def test_delete_is_safe_when_no_connection_exists(self, repo: ChasterConnectionRepository) -> None:
        repo.delete("u1")  # must not raise

    def test_delete_reports_whether_a_connection_existed(self, repo: ChasterConnectionRepository) -> None:
        assert repo.delete("u1") is False
        _create(repo)
        assert repo.delete("u1") is True
        assert repo.delete("u1") is False


class TestExpiryPersistence:
    def test_token_expiry_timestamps_are_stored_and_read_back_exactly(self, repo: ChasterConnectionRepository) -> None:
        access_expiry = FIXED_TIME + timedelta(minutes=5)
        refresh_expiry = FIXED_TIME + timedelta(minutes=30)
        _create(repo, access_token_expires_at=access_expiry, refresh_token_expires_at=refresh_expiry)
        connection = repo.get("u1")
        assert connection.access_token_expires_at == access_expiry
        assert connection.refresh_token_expires_at == refresh_expiry


class TestReprRedaction:
    def test_repr_never_includes_encrypted_token_values(self, repo: ChasterConnectionRepository) -> None:
        _create(repo)
        connection = repo.get("u1")
        text = repr(connection)
        assert connection.encrypted_access_token not in text
        assert connection.encrypted_refresh_token not in text
        assert "<redacted>" in text


class TestDeleteWithDependentRows:
    """Foreign keys are enforced: `chaster_provider_observations`
    REFERENCES `chaster_connections(user_id)`. A naive DELETE of the
    connection would raise IntegrityError once any observation exists,
    which is the normal state of a user who has used `chaster status`
    flows. `delete()` must remove children first, atomically."""

    def _record_observation(self, core, tmp_path) -> None:
        from chaster.models import ChasterProviderStatus
        from chaster.provider_observation_repository import ChasterProviderObservationRecorder
        ChasterProviderObservationRecorder(tmp_path / "test.db", core=core).record(
            connection_id="u1", provider="chaster", chaster_lock_id="lock1",
            status=ChasterProviderStatus.LOCKED, fetched_at=FIXED_TIME, now=FIXED_TIME,
        )

    def test_delete_succeeds_for_a_connection_that_has_observations(self, repo, core, tmp_path) -> None:
        _create(repo)
        self._record_observation(core, tmp_path)
        assert repo.delete("u1") is True
        assert repo.get("u1") is None

    def test_delete_removes_the_connections_observations(self, repo, core, tmp_path) -> None:
        _create(repo)
        self._record_observation(core, tmp_path)
        repo.delete("u1")
        with core.raw_connection() as conn:
            count = conn.execute("SELECT COUNT(*) FROM chaster_provider_observations").fetchone()[0]
        assert count == 0

    def test_delete_only_removes_the_requesting_users_rows(self, repo, core, tmp_path) -> None:
        _create(repo)
        _create(repo, user_id="u2", chaster_account_id="chaster-acc-2")
        self._record_observation(core, tmp_path)
        repo.delete("u1")
        assert repo.get("u2") is not None

    def test_delete_kills_a_pending_authorization_link(self, repo, core, tmp_path) -> None:
        from chaster.repository import ChasterOAuthStateRepository
        states = ChasterOAuthStateRepository(tmp_path / "test.db", core=core)
        state = states.create_or_replace(user_id="u1", now=FIXED_TIME)
        _create(repo)
        repo.delete("u1")
        assert states.consume(state=state, now=FIXED_TIME).matched is False

    def test_delete_never_touches_user_reported_lock_state(self, repo, core) -> None:
        source = (Path(__file__).parent.parent.parent / "chaster" / "repository.py").read_text(encoding="utf-8")
        assert "lock_reports" not in source
