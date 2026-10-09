"""
chaster/token_manager.py

ChasterTokenManager -- the single place that keeps a user's Chaster
access token usable without any manual database intervention.

It owns exactly three behaviours, each previously either missing or
duplicated inside `ChasterProviderObservationService`:

1. PRE-CALL REFRESH. If the stored access token is expired (or within
   `expiry_skew` of expiring), exchange the stored refresh token at
   Chaster's token endpoint (`ChasterOAuthClient.refresh_tokens()`,
   `grant_type=refresh_token`) and persist the new pair.
2. 401 RETRY. If a call is rejected with HTTP 401 despite a token that
   looked valid (revoked server-side, clock skew), refresh ONCE and
   retry the call ONCE. A second 401 is terminal -- never a loop.
3. HONEST FAILURE CLASSIFICATION.
   - `ChasterReauthorizationRequiredError`: Chaster definitively
     rejected the refresh token (or the stored ciphertext cannot be
     decrypted, or a freshly refreshed token was still rejected). The
     connection is marked `needs_reauthorization`; the user must run
     `chaster connect`. This is the ONLY case that changes the stored
     connection status.
   - `ChasterRefreshUnavailableError`: the refresh attempt failed for a
     reason that says nothing about the credentials (network error,
     HTTP 429/5xx). The connection is left untouched and ACTIVE, so a
     later attempt can succeed on its own -- a transient outage must
     never permanently log a user out.
   - `ChasterNotConnectedError`: no connection row exists.

Refresh-token expiry is deliberately NOT pre-checked locally: some
Keycloak refresh tokens (offline tokens) report `refresh_expires_in` of
0, which would be misread as "already expired". The token endpoint is
the only authority on whether a refresh token is still valid.

A per-user lock serialises refreshes, because Keycloak may rotate the
refresh token on every use: two concurrent refreshes with the same
token would make the second one fail and wrongly look like a revoked
token. After acquiring the lock the manager re-reads the connection
and skips the refresh if another caller already did it.

Only the access token is passed to the supplied `operation`, and only
for the duration of that call. No token is ever logged or placed in an
exception message. The emergency-unlock plane does NOT use this class;
its semantics are intentionally unchanged.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Callable, TypeVar

from chaster.lock_client import ChasterLockApiError
from chaster.models import ConnectionStatus
from chaster.oauth_client import ChasterOAuthClient, ChasterTokenExchangeError
from chaster.repository import ChasterConnectionRepository
from chaster.token_encryptor import TokenDecryptionError

__all__ = [
    "ChasterTokenManager",
    "ChasterNotConnectedError",
    "ChasterReauthorizationRequiredError",
    "ChasterRefreshUnavailableError",
    "DEFAULT_EXPIRY_SKEW",
]

T = TypeVar("T")

DEFAULT_EXPIRY_SKEW = timedelta(seconds=30)


class ChasterNotConnectedError(LookupError):
    """No Chaster connection exists for this user."""


class ChasterReauthorizationRequiredError(RuntimeError):
    """The connection can no longer be renewed automatically; the user
    must run `chaster connect`. The connection has been marked
    `needs_reauthorization` before this is raised."""


class ChasterRefreshUnavailableError(RuntimeError):
    """A refresh could not be completed for a transient reason. The
    stored connection was NOT modified."""


class ChasterTokenManager:
    def __init__(
        self, *, connections: ChasterConnectionRepository, oauth_client: ChasterOAuthClient,
        expiry_skew: timedelta = DEFAULT_EXPIRY_SKEW,
    ) -> None:
        self._connections = connections
        self._oauth_client = oauth_client
        self._expiry_skew = expiry_skew
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def get_valid_access_token(self, user_id: str, *, now: datetime) -> str:
        """Returns a plaintext access token that is not expired,
        refreshing first when needed. Raises one of the three errors
        documented in this module's header."""
        connection = self._connections.get(user_id)
        if connection is None:
            raise ChasterNotConnectedError(f"No Chaster connection exists for user_id={user_id!r}.")
        if connection.connection_status == ConnectionStatus.NEEDS_REAUTHORIZATION:
            raise ChasterReauthorizationRequiredError("The Chaster connection needs to be re-authorized.")
        if connection.access_token_expires_at <= now + self._expiry_skew:
            self._refresh(user_id, now=now, rejected_token=None)
        return self._decrypt_access_token(user_id, now=now)

    def call_with_token(self, user_id: str, *, now: datetime, operation: Callable[[str], T]) -> T:
        """Runs `operation(access_token)` with a valid token. On a
        `ChasterLockApiError` with `status_code == 401`, refreshes once
        and retries once; every other error propagates unchanged."""
        token = self.get_valid_access_token(user_id, now=now)
        try:
            return operation(token)
        except ChasterLockApiError as exc:
            if exc.status_code != 401:
                raise
        self._refresh(user_id, now=now, rejected_token=token)
        retry_token = self._decrypt_access_token(user_id, now=now)
        try:
            return operation(retry_token)
        except ChasterLockApiError as exc:
            if exc.status_code != 401:
                raise
        # A token that was just issued by Chaster was rejected as well:
        # renewing again cannot help.
        self._connections.mark_needs_reauthorization(user_id, now=now)
        raise ChasterReauthorizationRequiredError("Chaster rejected a freshly refreshed access token.")

    def _decrypt_access_token(self, user_id: str, *, now: datetime) -> str:
        try:
            return self._connections.get_decrypted_access_token(user_id, now=now)
        except LookupError as exc:
            raise ChasterNotConnectedError(str(exc)) from exc
        except TokenDecryptionError as exc:
            # The repository has already marked NEEDS_REAUTHORIZATION.
            raise ChasterReauthorizationRequiredError("Stored Chaster tokens could not be decrypted.") from exc

    def _user_lock(self, user_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(user_id, threading.Lock())

    def _refresh(self, user_id: str, *, now: datetime, rejected_token: str | None) -> None:
        with self._user_lock(user_id):
            connection = self._connections.get(user_id)
            if connection is None:
                raise ChasterNotConnectedError(f"No Chaster connection exists for user_id={user_id!r}.")
            if connection.connection_status == ConnectionStatus.NEEDS_REAUTHORIZATION:
                raise ChasterReauthorizationRequiredError("The Chaster connection needs to be re-authorized.")

            # Another caller may have refreshed while we waited.
            if rejected_token is None:
                if connection.access_token_expires_at > now + self._expiry_skew:
                    return
            elif self._decrypt_access_token(user_id, now=now) != rejected_token:
                return

            try:
                refresh_token = self._connections.get_decrypted_refresh_token(user_id, now=now)
            except TokenDecryptionError as exc:
                raise ChasterReauthorizationRequiredError("Stored Chaster tokens could not be decrypted.") from exc

            try:
                tokens = self._oauth_client.refresh_tokens(refresh_token=refresh_token)
            except ChasterTokenExchangeError as exc:
                if exc.transient:
                    raise ChasterRefreshUnavailableError("Chaster's token endpoint is temporarily unavailable.") from exc
                self._connections.mark_needs_reauthorization(user_id, now=now)
                raise ChasterReauthorizationRequiredError("Chaster rejected the refresh token.") from exc

            self._connections.create_or_replace(
                user_id=user_id, chaster_account_id=connection.chaster_account_id,
                chaster_username=connection.chaster_username,
                access_token=tokens.access_token, refresh_token=tokens.refresh_token,
                access_token_expires_at=now + timedelta(seconds=tokens.expires_in),
                refresh_token_expires_at=now + timedelta(seconds=tokens.refresh_expires_in),
                granted_scopes=tokens.granted_scopes, now=now,
            )
