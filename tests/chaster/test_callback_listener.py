"""
tests/chaster/test_callback_listener.py

Uses asyncio.run() directly around the listener's own async
start()/stop() (pytest-asyncio is not a dependency of this project,
matching tests/bot/test_discord_bot.py's own established pattern),
and plain synchronous `requests` calls against the real bound
localhost port to exercise it -- a real server, a real HTTP request,
no live Chaster/Discord/Cloudflare involved.
"""

from __future__ import annotations

import asyncio
import logging
import socket

import aiohttp
import pytest
import requests
from aiohttp.test_utils import TestClient, TestServer

from chaster.callback_listener import CALLBACK_PATH, ChasterCallbackListener


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestStartupAndShutdown:
    def test_listener_starts_and_stops_cleanly(self) -> None:
        port = _free_port()

        async def _run() -> None:
            calls = []

            async def on_callback(state, code, error):
                calls.append((state, code, error))
                return "ok"

            listener = ChasterCallbackListener(bind_host="127.0.0.1", bind_port=port, on_callback=on_callback)
            await listener.start()
            await listener.stop()

        asyncio.run(_run())  # must not raise

    def test_stopping_a_never_started_listener_is_safe(self) -> None:
        async def _run() -> None:
            listener = ChasterCallbackListener(bind_host="127.0.0.1", bind_port=_free_port(), on_callback=lambda s, c, e: "ok")
            await listener.stop()  # must not raise

        asyncio.run(_run())


class TestRequestHandling:
    def test_a_real_request_reaches_the_callback_handler(self) -> None:
        port = _free_port()
        received: list = []

        async def _run() -> None:
            async def on_callback(state, code, error):
                received.append((state, code, error))
                return "connected"

            listener = ChasterCallbackListener(bind_host="127.0.0.1", bind_port=port, on_callback=on_callback)
            await listener.start()
            try:
                response = await asyncio.to_thread(
                    requests.get, f"http://127.0.0.1:{port}{CALLBACK_PATH}",
                    params={"state": "abc123", "code": "authcode456"}, timeout=5,
                )
                assert response.status_code == 200
                assert response.text == "connected"
            finally:
                await listener.stop()

        asyncio.run(_run())
        assert received == [("abc123", "authcode456", None)]

    def test_a_malformed_request_missing_all_query_params_still_reaches_the_handler_with_nones(self) -> None:
        port = _free_port()
        received: list = []

        async def _run() -> None:
            async def on_callback(state, code, error):
                received.append((state, code, error))
                return "handled"

            listener = ChasterCallbackListener(bind_host="127.0.0.1", bind_port=port, on_callback=on_callback)
            await listener.start()
            try:
                response = await asyncio.to_thread(
                    requests.get, f"http://127.0.0.1:{port}{CALLBACK_PATH}", timeout=5,
                )
                assert response.status_code == 200
            finally:
                await listener.stop()

        asyncio.run(_run())
        assert received == [(None, None, None)]

    def test_an_unhandled_exception_in_the_callback_produces_a_safe_response_not_a_crash(self) -> None:
        port = _free_port()

        async def _run() -> None:
            async def on_callback(state, code, error):
                raise RuntimeError("boom -- simulated bug")

            listener = ChasterCallbackListener(bind_host="127.0.0.1", bind_port=port, on_callback=on_callback)
            await listener.start()
            try:
                response = await asyncio.to_thread(
                    requests.get, f"http://127.0.0.1:{port}{CALLBACK_PATH}",
                    params={"state": "s"}, timeout=5,
                )
                assert response.status_code == 200  # a safe response, not a 500 crash trace
                assert "boom" not in response.text
                assert "Traceback" not in response.text
            finally:
                await listener.stop()

        asyncio.run(_run())

    def test_a_request_to_an_unregistered_path_returns_404_not_a_generic_route(self) -> None:
        """No unnecessary endpoints -- confirms only CALLBACK_PATH exists."""
        port = _free_port()

        async def _run() -> None:
            listener = ChasterCallbackListener(bind_host="127.0.0.1", bind_port=port, on_callback=lambda s, c, e: "ok")
            await listener.start()
            try:
                response = await asyncio.to_thread(
                    requests.get, f"http://127.0.0.1:{port}/some/other/path", timeout=5,
                )
                assert response.status_code == 404
            finally:
                await listener.stop()

        asyncio.run(_run())


SECRET = "SECRET"


def _all_captured_text(caplog: pytest.LogCaptureFixture) -> str:
    """Every captured record's logger name and fully formatted message
    -- what any console/file handler on the root logger would print."""
    return "\n".join(f"{record.name} {record.getMessage()}" for record in caplog.records)


class TestAccessLogNeverLeaksQueryStrings:
    """Regression: aiohttp's default access logger printed the full
    callback URL (OAuth `code`, `state`, `session_state`) at INFO, and
    a follow-up favicon request repeated them in its Referer."""

    def test_callback_and_favicon_requests_never_log_the_query_string_or_referer(self, caplog: pytest.LogCaptureFixture) -> None:
        received: list = []

        async def on_callback(state, code, error):
            received.append((state, code, error))
            return "connected"

        listener = ChasterCallbackListener(bind_host="127.0.0.1", bind_port=0, on_callback=on_callback)

        async def _run() -> None:
            async with TestClient(TestServer(listener.build_app(), host="127.0.0.1")) as client:
                callback_url = f"{CALLBACK_PATH}?code={SECRET}&state={SECRET}&session_state={SECRET}"
                response = await client.get(callback_url)
                assert response.status == 200
                favicon = await client.get(
                    "/favicon.ico", headers={"Referer": f"http://127.0.0.1:8420{callback_url}"},
                )
                assert favicon.status == 404

        caplog.set_level(logging.DEBUG)
        asyncio.run(_run())

        assert received == [(SECRET, SECRET, None)]
        access_lines = [r.getMessage() for r in caplog.records if r.name == "aiohttp.access"]
        # The access log still records the request -- just without the query string.
        assert access_lines == [f"GET {CALLBACK_PATH} -> 200", "GET /favicon.ico -> 404"]
        assert SECRET not in _all_captured_text(caplog)

    def test_the_real_started_listener_never_logs_the_query_string(self, caplog: pytest.LogCaptureFixture) -> None:
        """Same guarantee through start()'s own production AppRunner/TCPSite."""
        port = _free_port()

        async def _run() -> None:
            async def on_callback(state, code, error):
                return "connected"

            listener = ChasterCallbackListener(bind_host="127.0.0.1", bind_port=port, on_callback=on_callback)
            await listener.start()
            try:
                # aiohttp's own client rather than `requests`, whose
                # urllib3 DEBUG line would log the URL on the client side.
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        f"http://127.0.0.1:{port}{CALLBACK_PATH}",
                        params={"code": SECRET, "state": SECRET}, timeout=aiohttp.ClientTimeout(total=5),
                    ) as response:
                        assert response.status == 200
            finally:
                await listener.stop()

        caplog.set_level(logging.DEBUG)
        asyncio.run(_run())

        assert any(r.name == "aiohttp.access" for r in caplog.records)
        assert SECRET not in _all_captured_text(caplog)
