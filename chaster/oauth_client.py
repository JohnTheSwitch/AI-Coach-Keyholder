"""
chaster/oauth_client.py

ChasterOAuthClient -- the ONLY file in this project that speaks
Chaster's OAuth 2 protocol directly. Built only from confirmed
official facts (docs.chaster.app); does not guess any endpoint,
scope, or behavior beyond what has been directly verified. See
docs/architecture/chaster_integration_technical_design.md Sections
5-7.

Confirmed endpoints (official docs.chaster.app, verified during the
research phase of this project):
- Authorization: https://sso.chaster.app/auth/realms/app/protocol/openid-connect/auth
- Token:         https://sso.chaster.app/auth/realms/app/protocol/openid-connect/token

Never logs, and never includes in any exception message: the
authorization code, access token, refresh token, or client secret.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode

import requests

from chaster import CHASTER_USER_AGENT
from chaster._http import send_with_ordered_headers

__all__ = [
    "ChasterOAuthClient",
    "ChasterTokenResponse",
    "ChasterTokenExchangeError",
    "AUTHORIZATION_URL",
    "TOKEN_URL",
]

AUTHORIZATION_URL = "https://sso.chaster.app/auth/realms/app/protocol/openid-connect/auth"
TOKEN_URL = "https://sso.chaster.app/auth/realms/app/protocol/openid-connect/token"

DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
DEFAULT_READ_TIMEOUT_SECONDS = 15.0


class ChasterTokenExchangeError(RuntimeError):
    """Raised when exchanging an authorization code for tokens fails
    -- a non-2xx response, a malformed response body, or a network
    error. The message NEVER includes the authorization code, any
    token value, or the client secret -- only the HTTP status code
    (if any) and a short, generic description.

    `transient=True` marks a failure that says nothing about whether
    the credentials themselves are valid (a network error, HTTP 429,
    or a 5xx) -- a caller must NOT treat it as a revoked/expired
    refresh token. The default (False) is the conservative reading: a
    definitive rejection, an unparseable reply, or a code-exchange
    failure."""

    def __init__(self, message: str, *, status_code: int | None = None, transient: bool = False) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.transient = transient


@dataclass(frozen=True, kw_only=True)
class ChasterTokenResponse:
    """The token-endpoint response, mapped to plain fields. `scope`
    is what Chaster ACTUALLY granted -- which may differ from what was
    requested (chaster_integration_technical_design.md Section 10) --
    never assumed to equal the request's own scope parameter.

    `__repr__` is intentionally NOT the dataclass default: the default
    would include both raw token values verbatim, which must never
    appear in a stray print()/traceback/test-failure message."""
    access_token: str
    refresh_token: str
    expires_in: int
    refresh_expires_in: int
    granted_scopes: tuple[str, ...]

    def __repr__(self) -> str:
        return (
            f"ChasterTokenResponse(access_token=<redacted>, refresh_token=<redacted>, "
            f"expires_in={self.expires_in!r}, refresh_expires_in={self.refresh_expires_in!r}, "
            f"granted_scopes={self.granted_scopes!r})"
        )


class ChasterOAuthClient:
    """Stateless -- holds only the application's own client
    credentials (client_id always, client_secret only for the token
    exchange), never a specific user's tokens. One instance can be
    shared across every user's connection attempt."""

    def __init__(self, *, client_id: str, client_secret: str, redirect_uri: str) -> None:
        if not client_id or not client_id.strip():
            raise ValueError("client_id must be a non-empty string.")
        if not client_secret or not client_secret.strip():
            raise ValueError("client_secret must be a non-empty string.")
        if not redirect_uri or not redirect_uri.strip():
            raise ValueError("redirect_uri must be a non-empty string.")
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri

    def build_authorization_url(self, *, state: str, scopes: tuple[str, ...] = ("locks",)) -> str:
        """Builds the URL the user's browser is sent to. Only the
        `locks` scope is requested by default
        (chaster_integration_technical_design.md Section 2's own
        scope decision) -- callers may pass a different tuple, but
        CHASTER-01A itself never requests more than `locks`."""
        if not state or not state.strip():
            raise ValueError("state must be a non-empty string.")
        params = {
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes),
            "state": state,
        }
        return f"{AUTHORIZATION_URL}?{urlencode(params)}"

    def exchange_code_for_tokens(self, *, code: str) -> ChasterTokenResponse:
        """Exchanges a single-use authorization code for a token pair.
        A server-to-server call only -- `code`/the resulting tokens
        never pass through the browser again. Raises
        ChasterTokenExchangeError on any failure; never includes
        `code`, any token value, or the client secret in that
        exception's message."""
        if not code or not code.strip():
            raise ValueError("code must be a non-empty string.")
        try:
            response = send_with_ordered_headers(
                "POST", TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self._redirect_uri,
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
                headers={"User-Agent": CHASTER_USER_AGENT},
                timeout=(DEFAULT_CONNECT_TIMEOUT_SECONDS, DEFAULT_READ_TIMEOUT_SECONDS),
            )
        except requests.RequestException as exc:
            raise ChasterTokenExchangeError("Network error contacting Chaster's token endpoint.") from exc

        if response.status_code != 200:
            raise ChasterTokenExchangeError(
                f"Chaster's token endpoint returned HTTP {response.status_code}."
            )

        try:
            body = response.json()
            access_token = body["access_token"]
            refresh_token = body["refresh_token"]
            expires_in = int(body["expires_in"])
            refresh_expires_in = int(body["refresh_expires_in"])
            granted_scopes = tuple(body.get("scope", "").split())
        except (ValueError, KeyError, TypeError) as exc:
            raise ChasterTokenExchangeError(
                "Chaster's token endpoint returned a response this application could not parse."
            ) from exc

        return ChasterTokenResponse(
            access_token=access_token, refresh_token=refresh_token,
            expires_in=expires_in, refresh_expires_in=refresh_expires_in,
            granted_scopes=granted_scopes,
        )

    def refresh_tokens(self, *, refresh_token: str) -> ChasterTokenResponse:
        """Exchanges a refresh token for a new token pair. Same
        request shape as the code exchange, `grant_type=refresh_token`
        instead. Never logs or echoes `refresh_token` on failure."""
        if not refresh_token or not refresh_token.strip():
            raise ValueError("refresh_token must be a non-empty string.")
        try:
            response = send_with_ordered_headers(
                "POST", TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
                headers={"User-Agent": CHASTER_USER_AGENT},
                timeout=(DEFAULT_CONNECT_TIMEOUT_SECONDS, DEFAULT_READ_TIMEOUT_SECONDS),
            )
        except requests.RequestException as exc:
            raise ChasterTokenExchangeError(
                "Network error contacting Chaster's token endpoint.", transient=True,
            ) from exc

        if response.status_code != 200:
            status = response.status_code
            raise ChasterTokenExchangeError(
                f"Chaster's token endpoint returned HTTP {status} for a refresh attempt.",
                status_code=status, transient=(status == 429 or status >= 500),
            )

        try:
            body = response.json()
            return ChasterTokenResponse(
                access_token=body["access_token"], refresh_token=body["refresh_token"],
                expires_in=int(body["expires_in"]), refresh_expires_in=int(body["refresh_expires_in"]),
                granted_scopes=tuple(body.get("scope", "").split()),
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise ChasterTokenExchangeError(
                "Chaster's token endpoint returned a response this application could not parse for a refresh attempt."
            ) from exc

    def fetch_raw_profile(self, *, access_token: str) -> dict:
        """Calls the confirmed `GET /auth/profile` endpoint
        (operationId AuthMeController_me, chaster_integration_technical_design.md
        Section 11) and returns the raw, unparsed response dict.

        Now used by TWO callers: `chaster/callback_service.py::build_real_identity_resolver()`
        (production -- reads only the two confirmed fields it needs,
        `_id`/`username`) and `scripts/dev_chaster_production_client_smoke_test.py`
        (a manual, local diagnostic for inspecting the full real shape
        -- the real response has ~90 fields total; production never
        reads more than the two it needs). This method itself does
        not choose what to do with the response -- it only fetches
        and returns it unparsed, exactly the same "return a raw dict"
        pattern `chaster/lock_client.py::ChasterLockClient.list_active_locks()`
        uses. Never logs the access token, never persists the
        response, never writes it anywhere -- each caller is
        responsible for its own safe handling of what it reads back."""
        if not access_token or not access_token.strip():
            raise ValueError("access_token must be a non-empty string.")
        try:
            response = send_with_ordered_headers(
                "GET", "https://api.chaster.app/auth/profile",
                headers={"Authorization": f"Bearer {access_token}", "User-Agent": CHASTER_USER_AGENT},
                timeout=(DEFAULT_CONNECT_TIMEOUT_SECONDS, DEFAULT_READ_TIMEOUT_SECONDS),
            )
        except requests.RequestException as exc:
            raise ChasterTokenExchangeError("Network error contacting Chaster's profile endpoint.") from exc

        if response.status_code != 200:
            raise ChasterTokenExchangeError(f"Chaster's profile endpoint returned HTTP {response.status_code}.")

        try:
            body = response.json()
        except ValueError as exc:
            raise ChasterTokenExchangeError("Chaster's profile endpoint returned a response this application could not parse.") from exc

        if not isinstance(body, dict):
            raise ChasterTokenExchangeError("Chaster's profile endpoint returned an unexpected response shape (expected an object).")
        return body
