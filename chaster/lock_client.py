"""
chaster/lock_client.py

ChasterLockClient -- a narrow resource client for the two confirmed
Chaster Public API operations this project's emergency-unlock safety
plane needs. Deliberately separate from `chaster/oauth_client.py`
(authorization-code/refresh-token exchange only) -- this client makes
resource API calls with an already-obtained bearer access token, a
genuinely different concern.

Confirmed directly from the official OpenAPI specification
(https://api.chaster.app/api-json, re-fetched during this project's
own research):

- GET /locks -- operationId LockController_findAll, "Get user locks".
  "Returns a list of all user locks. By default, only active locks
  are returned." Optional `status` query param (enum: active,
  archived, all; default active) -- confirmed, official, used here
  explicitly rather than relying only on the documented default.
  Response: array of LockForWearer. Scope: locks.
- POST /locks/{lockId}/emergency-unlock -- operationId
  LockController_emergencyUnlock, "Emergency-unlock a bondage lock".
  "Wearer-only safety release that bypasses the normal unlock
  constraints; requires at least one safety feature enabled in the
  bondage config." Responses: 204 success, 400 (not a bondage lock /
  no safety feature enabled / not locked), 401, 403 (non-wearer),
  404. Scope: locks.

NOT confirmed from any authoritative first-party source (see
chaster/emergency_unlock_provider.py's own header docstring): the
exact field-level JSON schema of a `LockForWearer` object (its id
field, lock-type field, bondage-config field, and their exact names).
This client therefore returns each lock as a raw, unparsed `dict` --
it does not guess field names. See `unconfirmed_lock_field_extractor`
in emergency_unlock_provider.py for where that gap is isolated.

Never logs the access token; never includes it in an exception
message.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import requests

from chaster import CHASTER_USER_AGENT
from chaster._http import send_with_ordered_headers

__all__ = [
    "ChasterLockClient",
    "ChasterLockApiError",
    "EmergencyUnlockHttpOutcome",
    "EmergencyUnlockHttpResult",
]

LOCKS_URL = "https://api.chaster.app/locks"

DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
DEFAULT_READ_TIMEOUT_SECONDS = 15.0


class ChasterLockApiError(RuntimeError):
    """Raised on a network error or a response this client cannot
    interpret. Never includes the access token in its message.

    `status_code` is the HTTP status of a non-200 response, or None
    for a network error / unparseable body. It exists so callers (see
    chaster/token_manager.py) can recognise an expired access token
    (HTTP 401) without parsing the message text."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class EmergencyUnlockHttpOutcome(StrEnum):
    """Maps directly to the confirmed, documented response codes for
    POST /locks/{lockId}/emergency-unlock -- nothing inferred."""
    SUCCEEDED = "succeeded"          # 204
    NOT_ELIGIBLE = "not_eligible"    # 400 -- not bondage / no safety feature / not locked
    UNAUTHORIZED = "unauthorized"    # 401
    FORBIDDEN = "forbidden"          # 403 -- only the wearer can emergency-unlock
    NOT_FOUND = "not_found"          # 404 -- lock disappeared/changed since discovery
    UNEXPECTED = "unexpected"        # any other status -- never treated as success


@dataclass(frozen=True, kw_only=True)
class EmergencyUnlockHttpResult:
    outcome: EmergencyUnlockHttpOutcome
    http_status: int


class ChasterLockClient:
    def list_active_locks(self, *, access_token: str) -> list[dict]:
        """Returns each active lock as a raw dict -- deliberately NOT
        mapped to a typed object, since this project has not
        confirmed LockForWearer's exact field names from an
        authoritative source (see this module's own header
        docstring). Raises ChasterLockApiError on any network/HTTP
        failure; never returns a partial or fabricated list."""
        if not access_token or not access_token.strip():
            raise ValueError("access_token must be a non-empty string.")
        try:
            response = send_with_ordered_headers(
                "GET", LOCKS_URL, params={"status": "active"},
                headers={"Authorization": f"Bearer {access_token}", "User-Agent": CHASTER_USER_AGENT},
                timeout=(DEFAULT_CONNECT_TIMEOUT_SECONDS, DEFAULT_READ_TIMEOUT_SECONDS),
            )
        except requests.RequestException as exc:
            raise ChasterLockApiError("Network error contacting Chaster's locks endpoint.") from exc

        if response.status_code != 200:
            raise ChasterLockApiError(
                f"Chaster's locks endpoint returned HTTP {response.status_code}.", status_code=response.status_code,
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise ChasterLockApiError("Chaster's locks endpoint returned a response this application could not parse.") from exc

        if not isinstance(body, list):
            raise ChasterLockApiError("Chaster's locks endpoint returned an unexpected response shape (expected a list).")
        return body

    def emergency_unlock(self, *, access_token: str, lock_id: str) -> EmergencyUnlockHttpResult:
        """Calls POST /locks/{lockId}/emergency-unlock. `lock_id` is
        never accepted from any caller outside this project's own
        provider -- see EmergencyUnlockProvider's own Protocol
        docstring for the full boundary. Only HTTP 204 is ever mapped
        to SUCCEEDED; every other status, including a genuine network
        failure surfaced as ChasterLockApiError by the caller's own
        handling, is a form of failure."""
        if not access_token or not access_token.strip():
            raise ValueError("access_token must be a non-empty string.")
        if not lock_id or not lock_id.strip():
            raise ValueError("lock_id must be a non-empty string.")
        try:
            response = send_with_ordered_headers(
                "POST", f"{LOCKS_URL}/{lock_id}/emergency-unlock",
                headers={"Authorization": f"Bearer {access_token}", "User-Agent": CHASTER_USER_AGENT},
                timeout=(DEFAULT_CONNECT_TIMEOUT_SECONDS, DEFAULT_READ_TIMEOUT_SECONDS),
            )
        except requests.RequestException as exc:
            raise ChasterLockApiError("Network error contacting Chaster's emergency-unlock endpoint.") from exc

        outcome_by_status = {
            204: EmergencyUnlockHttpOutcome.SUCCEEDED,
            400: EmergencyUnlockHttpOutcome.NOT_ELIGIBLE,
            401: EmergencyUnlockHttpOutcome.UNAUTHORIZED,
            403: EmergencyUnlockHttpOutcome.FORBIDDEN,
            404: EmergencyUnlockHttpOutcome.NOT_FOUND,
        }
        outcome = outcome_by_status.get(response.status_code, EmergencyUnlockHttpOutcome.UNEXPECTED)
        return EmergencyUnlockHttpResult(outcome=outcome, http_status=response.status_code)
