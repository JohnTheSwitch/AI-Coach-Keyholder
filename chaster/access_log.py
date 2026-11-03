"""
chaster/access_log.py

Secret-safe access logging for this project's two local aiohttp.web
listeners (chaster/callback_listener.py, chaster/emergency_unlock_listener.py).

WHY THIS EXISTS: aiohttp's default `AccessLogger` writes the full
request line -- path AND query string -- plus the `Referer` and
`User-Agent` headers to the `aiohttp.access` logger at INFO. On the
OAuth callback that is the Chaster authorization `code`, `state` and
`session_state`; a follow-up favicon request repeats them all in its
`Referer`. This project never logs a token or secret, so neither
listener may use the default format.

`QuerylessAccessLogger` logs only the method, the path WITHOUT any
query string, and the response status -- never the query string, never
any header, never the remote address.

`SAFE_HANDLER_ARGS` is passed as `web.Application(handler_args=...)`
rather than to `AppRunner` alone: aiohttp applies an application's own
handler_args AFTER any runner kwargs, so every runner that serves the
app -- the production `AppRunner` and aiohttp's own `TestServer` alike
-- uses this logger, with no way for a caller to forget it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aiohttp.abc import AbstractAccessLogger
from aiohttp.web import BaseRequest, StreamResponse


class QuerylessAccessLogger(AbstractAccessLogger):
    def log(self, request: BaseRequest, response: StreamResponse, time: float) -> None:
        # `request.path` is the decoded path only -- the query string
        # is never part of it.
        self.logger.info("%s %s -> %s", request.method, request.path, response.status)


SAFE_HANDLER_ARGS: Mapping[str, Any] = {"access_log_class": QuerylessAccessLogger}
