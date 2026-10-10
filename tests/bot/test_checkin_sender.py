"""DiscordCheckInSender with a fake client on a real event loop thread. No Discord network."""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import discord
import pytest

from bot.checkin_sender import DiscordCheckInSender
from infrastructure.database import Database as CoreDatabase
from supervision.checkins import SendOutcome

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
ROOT = Path(__file__).parent.parent.parent


def http_error(cls, status):
    response = SimpleNamespace(status=status, reason="x")
    return cls(response, "boom")


class FakeUser:
    def __init__(self, error=None) -> None:
        self.error, self.sent = error, []

    async def send(self, text):
        if self.error:
            raise self.error
        self.sent.append(text)


class FakeClient:
    def __init__(self, loop, *, ready=True, fetch_error=None, user=None) -> None:
        self.loop, self._ready, self.fetch_error = loop, ready, fetch_error
        self.user = user or FakeUser()
        self.fetched = []

    def is_ready(self):
        return self._ready

    async def fetch_user(self, discord_id):
        self.fetched.append(discord_id)
        if self.fetch_error:
            raise self.fetch_error
        return self.user


@pytest.fixture
def loop():
    lp = asyncio.new_event_loop()
    t = threading.Thread(target=lp.run_forever, daemon=True)
    t.start()
    yield lp
    lp.call_soon_threadsafe(lp.stop)
    t.join(timeout=5)
    lp.close()


@pytest.fixture
def core(tmp_path: Path) -> CoreDatabase:
    c = CoreDatabase(tmp_path / "test.db")
    with c.raw_connection() as conn:
        for path in sorted((ROOT / "database" / "migrations").glob("*.sql")):
            conn.executescript(path.read_text(encoding="utf-8"))
        conn.execute("INSERT INTO user_accounts (id, created_at, last_seen_at) VALUES ('u1', ?, ?)", (NOW.isoformat(),) * 2)
        conn.execute("INSERT INTO user_channel_identities (id, user_account_id, channel, external_id, created_at) "
                     "VALUES ('i1', 'u1', 'discord', '123456789', ?)", (NOW.isoformat(),))
        conn.execute("INSERT INTO user_accounts (id, created_at, last_seen_at) VALUES ('u2', ?, ?)", (NOW.isoformat(),) * 2)
        conn.execute("INSERT INTO user_channel_identities (id, user_account_id, channel, external_id, created_at) "
                     "VALUES ('i2', 'u2', 'discord', 'not-a-number', ?)", (NOW.isoformat(),))
        conn.commit()
    return c


def sender(core, client=None):
    s = DiscordCheckInSender(core.db_path, core=core)
    if client is not None:
        s.attach(client)
    return s


def test_delivered(core, loop) -> None:
    client = FakeClient(loop)
    assert sender(core, client).send(user_id="u1", text="hello") is SendOutcome.DELIVERED
    assert client.fetched == [123456789] and client.user.sent == ["hello"]


def test_not_attached_or_not_ready_is_undeliverable(core, loop) -> None:
    assert sender(core).send(user_id="u1", text="x") is SendOutcome.UNDELIVERABLE
    assert sender(core, FakeClient(loop, ready=False)).send(user_id="u1", text="x") is SendOutcome.UNDELIVERABLE


@pytest.mark.parametrize("user_id", ["nobody", "u2"])
def test_unknown_or_malformed_discord_identity_is_undeliverable(core, loop, user_id) -> None:
    client = FakeClient(loop)
    assert sender(core, client).send(user_id=user_id, text="x") is SendOutcome.UNDELIVERABLE and client.fetched == []


def test_closed_dms_is_undeliverable(core, loop) -> None:
    client = FakeClient(loop, user=FakeUser(error=http_error(discord.Forbidden, 403)))
    assert sender(core, client).send(user_id="u1", text="x") is SendOutcome.UNDELIVERABLE


def test_unknown_user_is_undeliverable(core, loop) -> None:
    client = FakeClient(loop, fetch_error=http_error(discord.NotFound, 404))
    assert sender(core, client).send(user_id="u1", text="x") is SendOutcome.UNDELIVERABLE


def test_other_http_errors_are_ambiguous(core, loop) -> None:
    client = FakeClient(loop, user=FakeUser(error=http_error(discord.HTTPException, 500)))
    assert sender(core, client).send(user_id="u1", text="x") is SendOutcome.UNKNOWN


def test_unexpected_exceptions_are_ambiguous(core, loop) -> None:
    client = FakeClient(loop, user=FakeUser(error=RuntimeError("weird")))
    assert sender(core, client).send(user_id="u1", text="x") is SendOutcome.UNKNOWN


def test_a_timeout_is_ambiguous(core, loop, monkeypatch) -> None:
    import bot.checkin_sender as mod
    monkeypatch.setattr(mod, "_SEND_TIMEOUT_SECONDS", 0.2)

    class Slow(FakeClient):
        async def fetch_user(self, discord_id):
            await asyncio.sleep(2)
            return self.user

    assert sender(core, Slow(loop)).send(user_id="u1", text="x") is SendOutcome.UNKNOWN
