"""
chaster/repository.py

docs/architecture/chaster_integration_technical_design.md (draft, not
approved for implementation as a whole -- see chaster/README.md for
the exact CHASTER-01A boundary this module implements).

Two structurally separate classes, the same split
task_catalog/lock_state/advanced_mode already established:

- `ChasterOAuthStateRepository` -- pending, single-use OAuth `state`
  values. No encryption (chaster_integration_technical_design.md
  Section 10/21) -- unguessability, not database confidentiality, is
  this table's own security property.
- `ChasterConnectionRepository` -- durable, encrypted-at-rest OAuth
  connections. Owns the encryption boundary: callers pass/receive
  PLAINTEXT tokens only; this class is the only thing that ever calls
  TokenEncryptor.encrypt()/.decrypt(), immediately before a write or
  immediately after a read -- application/service code above this
  layer never touches ciphertext directly.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta
from pathlib import Path

from chaster.models import ChasterConnection, ChasterOAuthState, ConnectionStatus
from chaster.token_encryptor import TokenDecryptionError, TokenEncryptor
from infrastructure.database import Database as CoreDatabase
from infrastructure.time_format import iso as _iso
from infrastructure.time_format import parse_iso as _parse_iso

__all__ = [
    "ChasterOAuthStateRepository",
    "ChasterConnectionRepository",
    "OAuthStateConsumeResult",
]

# RFC 6749 does not mandate a specific state length; Chaster's own
# authorization codes are single-use and expire in 10 minutes
# (confirmed research) -- this state value shares that same window
# (chaster_integration_technical_design.md Section 6/7). 32 random
# bytes, URL-safe-base64-encoded, is well beyond any realistic
# brute-force concern for a value that also expires this quickly.
STATE_BYTE_LENGTH = 32
STATE_TTL = timedelta(minutes=10)


def _require_user_id(user_id: str) -> None:
    if not user_id or not user_id.strip():
        raise ValueError("user_id must be a non-empty string.")


class ChasterOAuthStateRepository:
    """Owns `chaster_oauth_states`. The `state` value itself is never
    logged by this class -- every log call here references only
    `user_id`, never `state` (see this module's own tests)."""

    def __init__(self, db_path: str | Path, *, core: CoreDatabase | None = None) -> None:
        self.db_path = Path(db_path)
        self._core = core if core is not None else CoreDatabase(self.db_path)

    def create_or_replace(self, *, user_id: str, now: datetime) -> str:
        """Generates a fresh, single-use `state` value and stores it,
        REPLACING any earlier still-pending state for this same user
        (the UNIQUE(user_id) constraint enforces at most one pending
        row per user at the database level -- migration 022). The
        previous state value, if any, is immediately dead the moment
        this call returns: if the user goes back and clicks an older
        authorization link, its callback will find no matching row
        and fail safely as "unknown state" -- the exact,
        previously-analyzed consequence of this replace semantics."""
        _require_user_id(user_id)
        state = secrets.token_urlsafe(STATE_BYTE_LENGTH)
        expires_at = now + STATE_TTL
        with self._core.transaction() as tx:
            tx.execute("DELETE FROM chaster_oauth_states WHERE user_id = ?", (user_id,))
            tx.execute(
                "INSERT INTO chaster_oauth_states (state, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (state, user_id, _iso(now), _iso(expires_at)),
            )
        return state

    def consume(self, *, state: str, now: datetime) -> "OAuthStateConsumeResult":
        """Looks up and DELETES the row in one atomic transaction
        (`Database.transaction()` uses `BEGIN IMMEDIATE`, acquiring
        the write lock upfront) -- this is exactly what makes the
        critical invariant hold: of two concurrent callbacks racing
        for the same `state`, only the transaction that commits first
        ever sees the row; the second finds nothing, whether it reads
        a fraction of a second later or was blocked waiting for the
        first transaction's own lock. The row is deleted regardless of
        whether the returned result is valid or expired -- single-use
        means consumed on lookup, not only on a fully successful
        downstream exchange (chaster_integration_technical_design.md
        Section 7's own explicit reasoning: minimizing the replay
        window matters more than accommodating a rare retry)."""
        with self._core.transaction() as tx:
            row = tx.fetch_one(
                "SELECT user_id, expires_at FROM chaster_oauth_states WHERE state = ?", (state,),
            )
            if row is None:
                return OAuthStateConsumeResult(matched=False, expired=False, user_id=None)
            tx.execute("DELETE FROM chaster_oauth_states WHERE state = ?", (state,))
            if _parse_iso(row["expires_at"]) <= now:
                return OAuthStateConsumeResult(matched=True, expired=True, user_id=None)
            return OAuthStateConsumeResult(matched=True, expired=False, user_id=row["user_id"])


class OAuthStateConsumeResult:
    """The outcome of consuming a `state` value. `matched=False` (an
    unknown state) and `matched=True, expired=True` (a real-but-stale
    state) are DELIBERATELY represented the same way to any external
    caller that only checks `.user_id is None` -- neither leaks
    whether a given state string was ever real, which is the safer
    behavior for anything an attacker could probe with a guessed
    value."""
    __slots__ = ("matched", "expired", "user_id")

    def __init__(self, *, matched: bool, expired: bool, user_id: str | None) -> None:
        self.matched = matched
        self.expired = expired
        self.user_id = user_id


def _row_to_connection(row) -> ChasterConnection:
    return ChasterConnection(
        user_id=row["user_id"], chaster_account_id=row["chaster_account_id"],
        chaster_username=row["chaster_username"],
        encrypted_access_token=row["encrypted_access_token"],
        encrypted_refresh_token=row["encrypted_refresh_token"],
        encryption_key_version=row["encryption_key_version"],
        access_token_expires_at=_parse_iso(row["access_token_expires_at"]),
        refresh_token_expires_at=_parse_iso(row["refresh_token_expires_at"]),
        granted_scopes=tuple(json.loads(row["granted_scopes_json"])),
        connection_status=ConnectionStatus(row["connection_status"]),
        created_at=_parse_iso(row["created_at"]), updated_at=_parse_iso(row["updated_at"]),
    )


class ChasterConnectionRepository:
    """Owns `chaster_connections` AND the encryption boundary --
    callers of this class only ever pass/receive plaintext access and
    refresh tokens; ciphertext never crosses this class's own public
    API in either direction."""

    def __init__(
        self, db_path: str | Path, *, core: CoreDatabase | None = None, encryptor: TokenEncryptor,
    ) -> None:
        self.db_path = Path(db_path)
        self._core = core if core is not None else CoreDatabase(self.db_path)
        self._encryptor = encryptor

    def create_or_replace(
        self, *, user_id: str, chaster_account_id: str, chaster_username: str | None,
        access_token: str, refresh_token: str,
        access_token_expires_at: datetime, refresh_token_expires_at: datetime,
        granted_scopes: tuple[str, ...], now: datetime,
    ) -> ChasterConnection:
        """Encrypts both tokens, then writes the row atomically --
        never a partial write. `encryption_key_version` is always
        NULL here (CHASTER-01A has exactly one key; reserved for a
        future rotation slice, chaster_integration_technical_design.md
        Section 10)."""
        _require_user_id(user_id)
        if not chaster_account_id or not chaster_account_id.strip():
            raise ValueError("chaster_account_id must be a non-empty string.")
        encrypted_access_token = self._encryptor.encrypt(access_token)
        encrypted_refresh_token = self._encryptor.encrypt(refresh_token)
        with self._core.transaction() as tx:
            tx.execute(
                """
                INSERT INTO chaster_connections
                    (user_id, chaster_account_id, chaster_username,
                     encrypted_access_token, encrypted_refresh_token, encryption_key_version,
                     access_token_expires_at, refresh_token_expires_at, granted_scopes_json,
                     connection_status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    chaster_account_id = excluded.chaster_account_id,
                    chaster_username = excluded.chaster_username,
                    encrypted_access_token = excluded.encrypted_access_token,
                    encrypted_refresh_token = excluded.encrypted_refresh_token,
                    encryption_key_version = excluded.encryption_key_version,
                    access_token_expires_at = excluded.access_token_expires_at,
                    refresh_token_expires_at = excluded.refresh_token_expires_at,
                    granted_scopes_json = excluded.granted_scopes_json,
                    connection_status = excluded.connection_status,
                    updated_at = excluded.updated_at
                """,
                (
                    user_id, chaster_account_id, chaster_username,
                    encrypted_access_token, encrypted_refresh_token,
                    _iso(access_token_expires_at), _iso(refresh_token_expires_at),
                    json.dumps(list(granted_scopes)), ConnectionStatus.ACTIVE.value,
                    _iso(now), _iso(now),
                ),
            )
            row = tx.fetch_one("SELECT * FROM chaster_connections WHERE user_id = ?", (user_id,))
        return _row_to_connection(row)

    def get(self, user_id: str) -> ChasterConnection | None:
        """Returns the connection row with its tokens still encrypted
        -- callers needing a plaintext token must use
        `get_decrypted_access_token()`/`get_decrypted_refresh_token()`
        below, which decrypt only the one value actually needed,
        immediately before use."""
        _require_user_id(user_id)
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT * FROM chaster_connections WHERE user_id = ?", (user_id,))
        return _row_to_connection(row) if row is not None else None

    def get_decrypted_access_token(self, user_id: str, *, now: datetime) -> str:
        """Decrypts and returns ONLY the access token, for immediate
        use in one outbound call. Never returns the connection object
        alongside it -- a caller that also needs connection metadata
        should call `get()` separately; this keeps the plaintext's
        lifetime as short as possible in the caller's own code."""
        connection = self._require_connection(user_id)
        try:
            return self._encryptor.decrypt(connection.encrypted_access_token)
        except TokenDecryptionError:
            self.mark_needs_reauthorization(user_id, now=now)
            raise

    def get_decrypted_refresh_token(self, user_id: str, *, now: datetime) -> str:
        connection = self._require_connection(user_id)
        try:
            return self._encryptor.decrypt(connection.encrypted_refresh_token)
        except TokenDecryptionError:
            self.mark_needs_reauthorization(user_id, now=now)
            raise

    def mark_needs_reauthorization(self, user_id: str, *, now: datetime) -> None:
        """A refresh definitively failed (e.g. Chaster rejected the
        refresh token as expired/revoked) -- moves the connection to
        NEEDS_REAUTHORIZATION without deleting it, so the user's
        `chaster_username`/history remain visible while they
        reconnect."""
        _require_user_id(user_id)
        with self._core.transaction() as tx:
            tx.execute(
                "UPDATE chaster_connections SET connection_status = ?, updated_at = ? WHERE user_id = ?",
                (ConnectionStatus.NEEDS_REAUTHORIZATION.value, _iso(now), user_id),
            )

    def delete(self, user_id: str) -> bool:
        """Local-only disconnect (chaster_integration_technical_design.md
        Section 19). Returns True if a connection existed, False if
        there was nothing to delete. Does not, and cannot, revoke
        anything on Chaster's own side (no revocation endpoint is
        confirmed to exist).

        In ONE transaction this removes, in FK-safe order:
        - the user's provider observations
          (`chaster_provider_observations.connection_id` REFERENCES
          `chaster_connections(user_id)` with foreign keys enforced, so
          the connection row cannot be deleted while they exist). This
          is the single deliberate exception to that table's
          append-only rule: the rows are children of the connection and
          are meaningless without it; disconnecting removes the
          user's Chaster data entirely rather than orphaning it.
        - any pending OAuth state (so an outstanding authorization
          link stops working the moment the user disconnects);
        - the connection row itself.
        `lock_state`'s user-reported lock history is a separate
        concern and is never touched."""
        _require_user_id(user_id)
        with self._core.transaction() as tx:
            existing = tx.fetch_one("SELECT 1 FROM chaster_connections WHERE user_id = ?", (user_id,))
            tx.execute("DELETE FROM chaster_provider_observations WHERE connection_id = ?", (user_id,))
            tx.execute("DELETE FROM chaster_oauth_states WHERE user_id = ?", (user_id,))
            tx.execute("DELETE FROM chaster_connections WHERE user_id = ?", (user_id,))
        return existing is not None

    def _require_connection(self, user_id: str) -> ChasterConnection:
        connection = self.get(user_id)
        if connection is None:
            raise LookupError(f"No Chaster connection exists for user_id={user_id!r}.")
        return connection
