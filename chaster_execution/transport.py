"""
chaster_execution/transport.py

The real Chaster write transport (Route A: the KEYHOLDER connection's OAuth token).

Barrier management (v0.39) uses the DOCUMENTED freeze endpoint:
    POST https://api.chaster.app/locks/{lockId}/freeze    {"isFrozen": true | false}
(see `set_frozen`). Extension management (Phase I2) uses the DOCUMENTED
    POST https://api.chaster.app/locks/{lockId}/extensions    {"extensions": [...]}
(see `set_extensions`; reached only from the guarded `python -m cli extension-review`). The time contract below is different: it is owner-supplied.

Request contract -- OWNER-SUPPLIED, not in Chaster's OpenAPI:
    POST https://api.chaster.app/locks/{lockId}/update-time
    Authorization: Bearer <keyholder access token>
    Content-Type: application/json
    {"duration": <integer seconds>}      positive = ADD time, negative = REMOVE time

Chaster's published OpenAPI (v0.35.0) leaves `UpdateTimeDto` with no properties,
so this body and its sign convention come from the project owner and have not
been verified against Chaster. The first live use must therefore be a single
1-hour change on a test lock, checked against the lock's end date before and
after (the audit log stores `end_date_before`). A wrong sign convention would
otherwise fail silently in the wrong direction.

Outcome mapping (documented codes of the endpoint): 204 -> OK; 400 -> NOT_ELIGIBLE;
403 -> FORBIDDEN (missing lock.time.add / lock.time.remove); 404 -> NOT_FOUND;
401 -> the token manager refreshes once and retries once (a 401 means the request
was not processed), else UNAUTHORIZED. Anything else, including any other 2xx,
a 5xx, a read timeout or a dropped connection, is UNKNOWN (ambiguous, never
retried). Only a connect timeout, a missing/unrefreshable connection or a
rejected input counts as NOT_SENT.

Secrets: the token only ever lives in the Authorization header of the one
request; it is never logged, returned or stored. TLS verification is untouched.
"""

from __future__ import annotations

import logging
import re
from typing import Callable

import requests

from chaster import CHASTER_USER_AGENT
from chaster._http import send_with_ordered_headers
from chaster.lock_client import (
    DEFAULT_CONNECT_TIMEOUT_SECONDS,
    DEFAULT_READ_TIMEOUT_SECONDS,
    LOCKS_URL,
    ChasterLockApiError,
)
from chaster.token_manager import (
    ChasterNotConnectedError,
    ChasterReauthorizationRequiredError,
    ChasterRefreshUnavailableError,
    ChasterTokenManager,
)
from chaster_execution.models import LockActionTransport, TransportOutcome, TransportResult
from infrastructure.clock import Clock
from rules_engine.limits import MAX_SINGLE_ACTION_HOURS

logger = logging.getLogger("ai_coach_keyholder.chaster_execution.transport")

__all__ = ["ChasterHttpTransport", "build_keyholder_transport", "UPDATE_TIME_REQUEST_BODY_CONFIRMED"]

# True because the owner supplied the contract above. It is NOT confirmed by
# Chaster's documentation; see the module docstring before the first live call.
UPDATE_TIME_REQUEST_BODY_CONFIRMED = True

_LOCK_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MAX_SECONDS = MAX_SINGLE_ACTION_HOURS * 3600


class ChasterHttpTransport:
    def __init__(
        self,
        *,
        token_manager: ChasterTokenManager,
        keyholder_user_id: str,
        clock: Clock,
        send: Callable[..., requests.Response] = send_with_ordered_headers,
    ) -> None:
        if not keyholder_user_id or not keyholder_user_id.strip():
            raise ValueError("keyholder_user_id must be non-empty")
        self._tokens = token_manager
        self._keyholder_user_id = keyholder_user_id
        self._clock = clock
        self._send = send            # injectable so tests never touch the network

    def add_time(self, *, lock_id: str, seconds: int) -> TransportResult:
        return self._update_time(lock_id, seconds, sign=1)

    def remove_time(self, *, lock_id: str, seconds: int) -> TransportResult:
        return self._update_time(lock_id, seconds, sign=-1)

    # ------------------------------------------------------------------

    def _update_time(self, lock_id: str, seconds: int, *, sign: int) -> TransportResult:
        if (
            not isinstance(lock_id, str) or not _LOCK_ID.match(lock_id)
            or isinstance(seconds, bool) or not isinstance(seconds, int)
            or not (1 <= seconds <= _MAX_SECONDS)
        ):
            return TransportResult(outcome=TransportOutcome.NOT_SENT)   # rejected input; nothing left the process

        return self._post(lock_id, "update-time", {"duration": sign * seconds})

    def set_frozen(self, *, lock_id: str, frozen: bool) -> TransportResult:
        """Barrier management: POST /locks/{lockId}/freeze with the documented body
        {"isFrozen": <bool>} (SetFreezeDto). Needs the lock.freeze / lock.unfreeze permission.
        Same outcome mapping as the time endpoint (204 OK, 400, 403, 404; 401 refresh once)."""
        if not isinstance(lock_id, str) or not _LOCK_ID.match(lock_id) or not isinstance(frozen, bool):
            return TransportResult(outcome=TransportOutcome.NOT_SENT)
        return self._post(lock_id, "freeze", {"isFrozen": frozen})

    def set_extensions(self, *, lock_id: str, body: dict) -> TransportResult:
        """Extension management (Phase I2): POST /locks/{lockId}/extensions with EditLockExtensionsDto {"extensions": [...]} -- the
        COMPLETE list (see chaster/extension_plan.py). Needs lock.extensions.edit. Documented success is 201; 200 and 204 are accepted
        as well. 400 / 403 / 404 as above; anything else is UNKNOWN."""
        if (not isinstance(lock_id, str) or not _LOCK_ID.match(lock_id) or not isinstance(body, dict)
                or set(body) != {"extensions"} or not isinstance(body["extensions"], list) or not body["extensions"]):
            return TransportResult(outcome=TransportOutcome.NOT_SENT)
        return self._post(lock_id, "extensions", body, ok=(200, 201, 204))

    def _post(self, lock_id: str, endpoint: str, body: dict, *, ok: tuple[int, ...] = (204,)) -> TransportResult:
        url = f"{LOCKS_URL}/{lock_id}/{endpoint}"

        def operation(token: str) -> TransportResult:
            try:
                response = self._send(
                    "POST", url, json=body,
                    headers={"Authorization": f"Bearer {token}", "User-Agent": CHASTER_USER_AGENT},
                    timeout=(DEFAULT_CONNECT_TIMEOUT_SECONDS, DEFAULT_READ_TIMEOUT_SECONDS),
                )
            except requests.ConnectTimeout:
                return TransportResult(outcome=TransportOutcome.NOT_SENT)
            except requests.RequestException:
                # Read timeout, reset connection, ...: the request may have been processed.
                return TransportResult(outcome=TransportOutcome.UNKNOWN)

            status = response.status_code
            if status == 401:
                raise ChasterLockApiError("Chaster rejected the access token.", status_code=401)
            mapping = {
                **{code: TransportOutcome.OK for code in ok},
                400: TransportOutcome.NOT_ELIGIBLE,
                403: TransportOutcome.FORBIDDEN,
                404: TransportOutcome.NOT_FOUND,
            }
            return TransportResult(outcome=mapping.get(status, TransportOutcome.UNKNOWN), http_status=status)

        try:
            return self._tokens.call_with_token(self._keyholder_user_id, now=self._clock.now(), operation=operation)
        except (ChasterNotConnectedError, ChasterRefreshUnavailableError):
            return TransportResult(outcome=TransportOutcome.NOT_SENT)
        except ChasterReauthorizationRequiredError:
            return TransportResult(outcome=TransportOutcome.UNAUTHORIZED, http_status=401)
        except ChasterLockApiError:
            return TransportResult(outcome=TransportOutcome.UNKNOWN)
        except Exception:
            logger.exception("Unexpected error in the Chaster transport; outcome treated as unknown.")
            return TransportResult(outcome=TransportOutcome.UNKNOWN)


def build_keyholder_transport(
    *, token_manager: ChasterTokenManager, keyholder_user_id: str, clock: Clock,
) -> LockActionTransport | None:
    if not UPDATE_TIME_REQUEST_BODY_CONFIRMED:
        return None
    return ChasterHttpTransport(token_manager=token_manager, keyholder_user_id=keyholder_user_id, clock=clock)
