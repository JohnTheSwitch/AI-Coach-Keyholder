"""
tests/chaster/test_emergency_unlock_listener.py
"""

from __future__ import annotations

import asyncio
import logging
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests
from aiohttp.test_utils import TestClient, TestServer
from cryptography.fernet import Fernet

from chaster.emergency_unlock import EmergencyUnlockService
from chaster.emergency_unlock_listener import UNLOCK_PATH, EmergencyUnlockListener
from chaster.emergency_unlock_provider import UnwiredEmergencyUnlockProvider
from chaster.repository import ChasterConnectionRepository
from chaster.token_encryptor import TokenEncryptor
from infrastructure.clock import FrozenClock
from infrastructure.database import Database as CoreDatabase

FIXED_TIME = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
REAL_SECRET = "the-real-emergency-secret"


def _apply_migrations(core: CoreDatabase) -> None:
    migrations_dir = Path(__file__).parent.parent.parent / "database" / "migrations"
    with core.raw_connection() as conn:
        for path in sorted(migrations_dir.glob("*.sql")):
            conn.executescript(path.read_text(encoding="utf-8"))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _build_listener(tmp_path: Path, *, port: int, bind_host: str = "127.0.0.1"):
    core = CoreDatabase(tmp_path / "test.db")
    _apply_migrations(core)
    with core.raw_connection() as conn:
        conn.execute(
            "INSERT INTO user_accounts (id, created_at, last_seen_at) VALUES (?, ?, ?)",
            ("u1", FIXED_TIME.isoformat(), FIXED_TIME.isoformat()),
        )
        conn.commit()
    connections = ChasterConnectionRepository(tmp_path / "test.db", core=core, encryptor=TokenEncryptor(Fernet.generate_key()))
    service = EmergencyUnlockService(
        tmp_path / "test.db", core=core, connections=connections,
        provider=UnwiredEmergencyUnlockProvider(), expected_secret=REAL_SECRET,
    )
    listener = EmergencyUnlockListener(bind_host=bind_host, bind_port=port, service=service, clock=FrozenClock(FIXED_TIME))
    return listener, core, connections


class TestLocalExposure:
    def test_listener_binds_to_localhost_only_by_default(self) -> None:
        """Confirms the config default this listener is constructed
        with in production (core/config.py) is 127.0.0.1, never a
        public bind address."""
        from core.config import Config
        assert Config(discord_token="test-token").chaster_emergency_bind_host == "127.0.0.1"

    def test_a_real_request_to_127_0_0_1_reaches_the_listener(self, tmp_path: Path) -> None:
        port = _free_port()
        listener, core, connections = _build_listener(tmp_path, port=port)

        async def _run() -> None:
            await listener.start()
            try:
                response = await asyncio.to_thread(
                    requests.post, f"http://127.0.0.1:{port}{UNLOCK_PATH}",
                    json={"secret": "wrong", "user_id": "u1"}, timeout=5,
                )
                assert response.status_code == 401
            finally:
                await listener.stop()

        asyncio.run(_run())

    def test_emergency_endpoint_is_not_registered_in_the_discord_command_router(self) -> None:
        import inspect

        import application.service as module
        source = inspect.getsource(module)
        assert "emergency" not in source.lower()

    def test_emergency_listener_is_a_separate_aiohttp_application_from_the_oauth_callback_listener(self) -> None:
        """The OAuth callback listener and the emergency listener must
        never share an aiohttp.web.Application -- confirmed by
        checking each builds its own in its own build_app(), which its
        own start() serves."""
        import inspect

        from chaster.callback_listener import ChasterCallbackListener
        for listener_class in (EmergencyUnlockListener, ChasterCallbackListener):
            assert "web.Application(handler_args=SAFE_HANDLER_ARGS)" in inspect.getsource(listener_class.build_app)
            assert "web.AppRunner(self.build_app())" in inspect.getsource(listener_class.start)


class TestRequestHandling:
    def test_successful_auth_but_no_connection_returns_404(self, tmp_path: Path) -> None:
        port = _free_port()
        listener, core, connections = _build_listener(tmp_path, port=port)

        async def _run() -> None:
            await listener.start()
            try:
                response = await asyncio.to_thread(
                    requests.post, f"http://127.0.0.1:{port}{UNLOCK_PATH}",
                    json={"secret": REAL_SECRET, "user_id": "u1"}, timeout=5,
                )
                assert response.status_code == 404
                assert response.json()["status"] == "no_connection"
            finally:
                await listener.stop()

        asyncio.run(_run())

    def test_provider_failure_returns_502(self, tmp_path: Path) -> None:
        port = _free_port()
        listener, core, connections = _build_listener(tmp_path, port=port)
        connections.create_or_replace(
            user_id="u1", chaster_account_id="chaster-acc-1", chaster_username="wearer1",
            access_token="at", refresh_token="rt",
            access_token_expires_at=FIXED_TIME + timedelta(minutes=5),
            refresh_token_expires_at=FIXED_TIME + timedelta(minutes=30),
            granted_scopes=("locks",), now=FIXED_TIME,
        )

        async def _run() -> None:
            await listener.start()
            try:
                response = await asyncio.to_thread(
                    requests.post, f"http://127.0.0.1:{port}{UNLOCK_PATH}",
                    json={"secret": REAL_SECRET, "user_id": "u1"}, timeout=5,
                )
                assert response.status_code == 502  # UnwiredEmergencyUnlockProvider always fails
                assert response.json()["status"] == "provider_failed"
            finally:
                await listener.stop()

        asyncio.run(_run())

    def test_malformed_request_body_is_rejected_with_400(self, tmp_path: Path) -> None:
        port = _free_port()
        listener, core, connections = _build_listener(tmp_path, port=port)

        async def _run() -> None:
            await listener.start()
            try:
                response = await asyncio.to_thread(
                    requests.post, f"http://127.0.0.1:{port}{UNLOCK_PATH}",
                    data="not json", timeout=5,
                )
                assert response.status_code == 400
            finally:
                await listener.stop()

        asyncio.run(_run())

    def test_missing_fields_rejected_with_400(self, tmp_path: Path) -> None:
        port = _free_port()
        listener, core, connections = _build_listener(tmp_path, port=port)

        async def _run() -> None:
            await listener.start()
            try:
                response = await asyncio.to_thread(
                    requests.post, f"http://127.0.0.1:{port}{UNLOCK_PATH}",
                    json={"secret": REAL_SECRET}, timeout=5,  # missing user_id
                )
                assert response.status_code == 400
            finally:
                await listener.stop()

        asyncio.run(_run())

    def test_response_never_leaks_the_expected_secret(self, tmp_path: Path) -> None:
        port = _free_port()
        listener, core, connections = _build_listener(tmp_path, port=port)

        async def _run() -> None:
            await listener.start()
            try:
                response = await asyncio.to_thread(
                    requests.post, f"http://127.0.0.1:{port}{UNLOCK_PATH}",
                    json={"secret": "wrong", "user_id": "u1"}, timeout=5,
                )
                assert REAL_SECRET not in response.text
            finally:
                await listener.stop()

        asyncio.run(_run())

    def test_startup_and_shutdown_are_clean(self, tmp_path: Path) -> None:
        port = _free_port()
        listener, core, connections = _build_listener(tmp_path, port=port)

        async def _run() -> None:
            await listener.start()
            await listener.stop()

        asyncio.run(_run())  # must not raise


class TestAccessLogNeverLeaksSecrets:
    def test_query_string_referer_and_body_secret_are_never_logged(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        listener, core, connections = _build_listener(tmp_path, port=0)

        async def _run() -> None:
            async with TestClient(TestServer(listener.build_app(), host="127.0.0.1")) as client:
                response = await client.post(
                    f"{UNLOCK_PATH}?secret=SECRET&state=SECRET",
                    json={"secret": "SECRET", "user_id": "u1"},
                    headers={"Referer": "http://127.0.0.1/?code=SECRET"},
                )
                assert response.status == 401

        caplog.set_level(logging.DEBUG)
        asyncio.run(_run())

        access_lines = [r.getMessage() for r in caplog.records if r.name == "aiohttp.access"]
        assert access_lines == [f"POST {UNLOCK_PATH} -> 401"]
        assert "SECRET" not in "\n".join(f"{r.name} {r.getMessage()}" for r in caplog.records)
