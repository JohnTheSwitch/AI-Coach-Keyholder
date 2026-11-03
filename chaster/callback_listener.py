"""
chaster/callback_listener.py

ChasterCallbackListener -- the ONLY aiohttp.web server in this
project. Exactly one route, localhost-only bind (Cloudflare Tunnel
provides the actual public HTTPS exposure -- this listener itself is
never directly reachable from outside the local machine, and never
knows about the tunnel at all -- see
docs/architecture/chaster_integration_technical_design.md Section 8's
own repository/Cloudflare boundary). Started/stopped by
CoachKeyholderBot.setup_hook()/close() (bot/discord_bot.py) -- this
class itself knows nothing about discord.py.

No health endpoint (no concrete repository/operational requirement
for one, per this slice's own explicit scope). No generic routing --
one path, one purpose.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from aiohttp import web

from chaster.access_log import SAFE_HANDLER_ARGS

logger = logging.getLogger("ai_coach_keyholder.chaster.callback_listener")

CALLBACK_PATH = "/oauth/chaster/callback"

# (state, code, error) -> the plain-text response body to show the
# browser. Any of the three may be None (see ChasterCallbackService,
# which does the actual validation -- this listener never inspects
# these values beyond extracting them from the query string).
CallbackHandler = Callable[[str | None, str | None, str | None], Awaitable[str]]


class ChasterCallbackListener:
    """Wraps aiohttp's AppRunner/TCPSite lifecycle. Deliberately thin
    -- bind, route, extract query params, hand off, respond. All
    actual OAuth logic lives in ChasterCallbackService."""

    def __init__(self, *, bind_host: str, bind_port: int, on_callback: CallbackHandler) -> None:
        self._bind_host = bind_host
        self._bind_port = bind_port
        self._on_callback = on_callback
        self._runner: web.AppRunner | None = None

    def build_app(self) -> web.Application:
        # SAFE_HANDLER_ARGS replaces aiohttp's default access log line,
        # which would otherwise print the OAuth `code`/`state` query
        # string (and the Referer repeating it) -- see chaster/access_log.py.
        app = web.Application(handler_args=SAFE_HANDLER_ARGS)
        app.router.add_get(CALLBACK_PATH, self._handle_callback)
        return app

    async def start(self) -> None:
        runner = web.AppRunner(self.build_app())
        await runner.setup()
        site = web.TCPSite(runner, self._bind_host, self._bind_port)
        await site.start()
        self._runner = runner
        logger.info(
            "Chaster callback listener bound to %s:%s%s (localhost-only)",
            self._bind_host, self._bind_port, CALLBACK_PATH,
        )

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def _handle_callback(self, request: web.Request) -> web.Response:
        state = request.query.get("state")
        code = request.query.get("code")
        error = request.query.get("error")
        try:
            body = await self._on_callback(state, code, error)
        except Exception:
            # Never lets a bug in callback handling crash the shared
            # event loop or leak an internal error to the browser --
            # the same "one bad request never crashes the process"
            # discipline bot/discord_bot.py already applies to Discord
            # messages.
            logger.exception("Unhandled error processing a Chaster OAuth callback.")
            body = "Something went wrong completing the Chaster connection. Please try `chaster connect` again."
        return web.Response(text=body, content_type="text/plain")
