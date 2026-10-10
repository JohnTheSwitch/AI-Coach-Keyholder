"""
chaster/models.py

docs/architecture/chaster_integration_technical_design.md. This
module implements CHASTER-01A's own data shapes (OAuth connection
foundation) plus CHASTER-01B Increment 2's own data shape (the
provider observation log, migration 023) -- still no lock fetching
beyond what CHASTER-01A's emergency-unlock provider already does, no
context-provider wiring (that remains explicitly deferred, gated on
its own separate decision).

Entities, all genuinely one-row-per-user or append-only-per-connection
(never accumulated-without-bound in a way that loses the "current
state is the latest row" read pattern -- see task_catalog/README.md's
own "id vs. user_id as PK" reasoning this module mirrors for the same
structural reason `user_preferences` already uses it):

- ChasterOAuthState -- a pending, single-use OAuth `state` value.
  Replaced (never accumulated) by a new `chaster connect`; consumed
  exactly once by a real callback.
- ChasterConnection -- a durable, encrypted-at-rest OAuth connection.
  `connection_status` has exactly two values -- see
  ConnectionStatus's own docstring for why the design deliberately
  does NOT add more.
- ChasterProviderObservation -- one row per real `GET /locks` fetch,
  append-only (mirrors lock_state's own `lock_reports` discipline --
  migration 019 -- structurally separate, never merged). Represents
  what Chaster's own API reported, at the moment it was fetched --
  PROVIDER-OBSERVED, never "verified" (see ChasterProviderStatus's
  own docstring for why VERIFIED is not, and will never be, a value
  here).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class ConnectionStatus(StrEnum):
    """Exactly two values -- deliberately not more, mirroring this
    project's own repeatedly-demonstrated minimal-state-model
    discipline (task_runtime's own "exactly three states... not added
    'for the future'"). Every other state you might expect ("not
    connected", "disconnected", "authorization in progress", "token
    refresh required") is represented structurally instead of as a
    stored value here:
    - "not connected"/"disconnected": no ChasterConnection row exists
      at all -- absence of a row IS the state, not a status value.
    - "authorization in progress": lives exclusively in
      ChasterOAuthState, never here -- the two entities are never
      both "about" the same in-progress attempt at once.
    - "token refresh required": transient runtime behavior (a refresh
      is attempted transparently at the moment a token is needed) --
      never persisted. Only if the refresh itself fails does the
      status change, to NEEDS_REAUTHORIZATION.
    - "unusable/corrupt credentials" (e.g. TokenDecryptionError):
      maps to NEEDS_REAUTHORIZATION too, reusing this same value
      rather than inventing a new one -- from the user's own
      perspective, both cases mean the same thing: reconnect."""
    ACTIVE = "active"
    NEEDS_REAUTHORIZATION = "needs_reauthorization"


@dataclass(frozen=True, kw_only=True)
class ChasterOAuthState:
    """A pending, single-use OAuth `state` value bound to exactly one
    internal user (user_accounts.id, never a raw Discord snowflake).
    Frozen/append-only in spirit, but the underlying table is NOT
    append-only in the LockReport sense -- a second `chaster connect`
    for the same user REPLACES this row entirely (see
    ChasterOAuthStateRepository.create_or_replace()), and a
    successful or failed callback DELETES it -- there is never more
    than one row per user, ever."""
    state: str
    user_id: str
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True, kw_only=True)
class ChasterConnection:
    """A durable Chaster OAuth connection for exactly one internal
    user. `encrypted_access_token`/`encrypted_refresh_token` are
    Fernet ciphertext (TEXT, never BLOB -- a Fernet token is already
    URL-safe base64 ASCII) -- this dataclass itself never holds
    plaintext; decryption happens only at the repository boundary,
    immediately before a value is actually needed for an outbound
    call (chaster_integration_technical_design.md Section 10's own
    "load-only-at-point-of-use" principle).

    `__repr__` is intentionally NOT the dataclass default: the
    default would include both encrypted-token fields verbatim,
    which must never appear in a stray print()/traceback/test-failure
    message even as ciphertext -- see this module's own tests."""
    user_id: str
    chaster_account_id: str
    chaster_username: str | None
    encrypted_access_token: str
    encrypted_refresh_token: str
    encryption_key_version: str | None
    access_token_expires_at: datetime
    refresh_token_expires_at: datetime
    granted_scopes: tuple[str, ...]
    connection_status: ConnectionStatus
    created_at: datetime
    updated_at: datetime

    def __repr__(self) -> str:
        return (
            f"ChasterConnection(user_id={self.user_id!r}, "
            f"chaster_account_id={self.chaster_account_id!r}, "
            f"chaster_username={self.chaster_username!r}, "
            f"encrypted_access_token=<redacted>, encrypted_refresh_token=<redacted>, "
            f"connection_status={self.connection_status!r})"
        )


class ChasterProviderStatus(StrEnum):
    """Exactly three values -- directly matching the confirmed,
    official `LockStatusEnum` (chaster_integration_technical_design.md
    Section 5/11: `locked` / `unlocked` / `deserted`). Deliberately
    named `ChasterProviderStatus`, not `LockStatus` -- this
    distinguishes it, at the type level and not just in prose, from
    `lock_state.models.LockKnowledgeState`'s own, structurally
    unrelated three-value type.

    NO `VERIFIED` MEMBER EXISTS HERE, AND NONE MAY EVER BE ADDED.
    A value from this enum means "this is what Chaster's own API
    reported, at the moment it was fetched" -- never independent
    physical confirmation. This is the same naming discipline
    `chaster/emergency_unlock_provider.py`'s own `ProviderUnlockOutcome`
    already established for a different Chaster status concept, and
    `lock_state/models.py`'s own header docstring establishes for
    user-reported state -- applied here a third time, for provider-
    observed state specifically."""
    LOCKED = "locked"
    UNLOCKED = "unlocked"
    DESERTED = "deserted"


@dataclass(frozen=True, kw_only=True)
class ChasterProviderObservation:
    """One real, append-only observation of Chaster's own reported
    lock status for one connection, fetched at `fetched_at`. Never
    mutated or deleted after creation -- the same discipline
    `lock_state.models.LockReport` already applies to user reports
    (migration 019), applied here to provider observations (migration
    023). `chaster_lock_id` is `None` specifically to represent "zero
    active locks found" -- a real, valid, already-handled outcome
    (see `chaster/emergency_unlock_provider.py::RealChasterEmergencyUnlockProvider`'s
    own confirmed handling of the same case) -- never a placeholder
    for "not yet fetched" (the absence of any row at all is what
    represents that, exactly as `LockKnowledgeState.UNKNOWN` does for
    user reports)."""
    id: str
    connection_id: str
    provider: str
    chaster_lock_id: str | None
    status: ChasterProviderStatus
    fetched_at: datetime
    created_at: datetime
    # Migration 026: provider-reported, None = absent / not the expected type
    # (always "unknown", never guessed). `canBeUnlocked` is deprecated by
    # Chaster and deliberately not stored.
    is_frozen: bool | None = None
    has_keyholder: bool | None = None
    end_date: datetime | None = None
