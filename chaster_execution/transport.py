"""
chaster_execution/transport.py

Factory for the real Chaster write transport.

BLOCKED (do not remove this gate without evidence): the official OpenAPI
document (api.chaster.app/api-json, v0.35.0, read directly) defines
`POST /locks/{lockId}/update-time` (scope `locks`, permissions
`lock.time.add` / `lock.time.remove`, 204 on success) but its request schema
`UpdateTimeDto` is an object with NO properties. How an amount of time and
its direction/unit are sent is therefore undocumented, and this project must
not guess a request body for an irreversible action. The only documented time
bodies belong to the Extensions API (`add_time` / `remove_time`, `params` in
seconds, scope `developer`), which is a different route from the approved
Route A.

Until the body is confirmed (a documented schema, or a captured real request
from the owner), the factory returns None and the execution plane is not
started: with ENABLE_CHASTER_EXECUTION=true the app logs why and keeps
recording dry-run decisions only.
"""

from __future__ import annotations

from chaster_execution.models import LockActionTransport

__all__ = ["UPDATE_TIME_REQUEST_BODY_CONFIRMED", "TRANSPORT_UNAVAILABLE_REASON", "build_keyholder_transport"]

UPDATE_TIME_REQUEST_BODY_CONFIRMED = False

TRANSPORT_UNAVAILABLE_REASON = (
    "the request body of POST /locks/{lockId}/update-time is not documented in Chaster's OpenAPI "
    "(UpdateTimeDto has no properties); refusing to guess it for an irreversible action"
)


def build_keyholder_transport() -> LockActionTransport | None:
    if not UPDATE_TIME_REQUEST_BODY_CONFIRMED:
        return None
    raise NotImplementedError("Implement the confirmed request body here.")  # pragma: no cover
