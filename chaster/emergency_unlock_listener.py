"""
chaster/emergency_unlock_listener.py

The ONLY network surface for the emergency-unlock safety plane.
Deliberately its OWN small `aiohttp.web` application -- NOT the same
`aiohttp.web.Application` `chaster/callback_listener.py` builds for the
OAuth callback, and NOT started from `CoachKeyholderBot.setup_hook()`
at all (see chaster/emergency_unlock_server.py, the standalone process
that runs this). This separation is deliberate and load-bearing: the
whole purpose of this safety plane is to remain usable even if the
Discord bot process itself is hung, crashed, or otherwise
malfunctioning -- sharing a process with the thing it exists to work
around would defeat that purpose.

Exactly one route, one operation. No general-purpose admin API.
"""

from __future__ import annotations

import logging

from aiohttp import web

from chaster.access_log import SAFE_HANDLER_ARGS
from chaster.emergency_unlock import EmergencyUnlockRequestStatus, EmergencyUnlockService
from infrastructure.clock import Clock

logger = logging.getLogger("ai_coach_keyholder.chaster.emergency_unlock_listener")

UNLOCK_PATH = "/emergency/unlock"

# HTTP status per terminal EmergencyUnlockRequestStatus -- deliberately
# distinct codes so a caller (a human using curl, or a tiny local
# script) can tell auth failure apart from "nothing to unlock" apart
# from a genuine provider failure, without parsing the body.
_STATUS_HTTP_CODE = {
    EmergencyUnlockRequestStatus.AUTH_FAILED: 401,
    EmergencyUnlockRequestStatus.NO_CONNECTION: 404,
    EmergencyUnlockRequestStatus.PROVIDER_SUCCEEDED: 200,
    EmergencyUnlockRequestStatus.PROVIDER_FAILED: 502,
}


class EmergencyUnlockListener:
    def __init__(
        self, *, bind_host: str, bind_port: int, service: EmergencyUnlockService, clock: Clock,
    ) -> None:
        self._bind_host = bind_host
        self._bind_port = bind_port
        self._service = service
        self._clock = clock
        self._runner: web.AppRunner | None = None

    def build_app(self) -> web.Application:
        # SAFE_HANDLER_ARGS replaces aiohttp's default access log line
        # (query string, Referer, User-Agent) -- see chaster/access_log.py.
        app = web.Application(handler_args=SAFE_HANDLER_ARGS)
        app.router.add_post(UNLOCK_PATH, self._handle_unlock)
        return app

    async def start(self) -> None:
        runner = web.AppRunner(self.build_app())
        await runner.setup()
        site = web.TCPSite(runner, self._bind_host, self._bind_port)
        await site.start()
        self._runner = runner
        logger.info(
            "Chaster emergency-unlock listener bound to %s:%s%s (localhost-only, separate process)",
            self._bind_host, self._bind_port, UNLOCK_PATH,
        )

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def _handle_unlock(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"message": "Malformed request body."}, status=400)

        presented_secret = body.get("secret") if isinstance(body, dict) else None
        user_id = body.get("user_id") if isinstance(body, dict) else None
        if not isinstance(presented_secret, str) or not isinstance(user_id, str) or not user_id:
            return web.json_response({"message": "Malformed request -- expected {\"secret\": ..., \"user_id\": ...}."}, status=400)

        try:
            result = self._service.request_unlock(
                presented_secret=presented_secret, user_id=user_id, now=self._clock.now(),
            )
        except Exception:
            logger.exception("Unhandled error processing an emergency-unlock request.")
            return web.json_response({"message": "Internal error."}, status=500)

        status_code = _STATUS_HTTP_CODE[result.status]
        return web.json_response({"status": result.status.value, "message": result.message}, status=status_code)
