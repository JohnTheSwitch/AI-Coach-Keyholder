"""v0.39 dual-role Phase B: UnifiedMessage, adapters, stored channel preference and the router. Fake clients only."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import discord
import pytest

from infrastructure.database import Database as CoreDatabase
from messaging.adapter import BaseMessagingAdapter
from messaging.discord_adapter import DISCORD_MAX_MESSAGE_CHARS, FAILURE_REPLY, DiscordMessagingAdapter, split_message
from messaging.models import MAX_CONTENT_CHARS, ChannelType, UnifiedMessage
from messaging.router import MessageRouter
from messaging.routes import MessageRouteRepository

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).parent.parent.parent


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def core(tmp_path: Path) -> CoreDatabase:
    c = CoreDatabase(tmp_path / "t.db")
    with c.raw_connection() as conn:
        for path in sorted((ROOT / "database" / "migrations").glob("*.sql")):
            conn.executescript(path.read_text(encoding="utf-8"))
        conn.execute("INSERT INTO user_accounts (id, created_at, last_seen_at) VALUES ('u1', ?, ?)", (NOW.isoformat(),) * 2)
        conn.execute("INSERT INTO user_channel_identities (id, user_account_id, channel, external_id, created_at) "
                     "VALUES ('i1', 'u1', 'discord', '4242', ?)", (NOW.isoformat(),))
        conn.commit()
    return c


class FakeUser:
    def __init__(self, error=None) -> None:
        self.sent, self.error = [], error

    async def send(self, text):
        if self.error:
            raise self.error
        self.sent.append(text)


class FakeClient:
    def __init__(self, *, ready=True, user=None, fetch_error=None) -> None:
        self._ready, self.user, self.fetch_error, self.fetched = ready, user or FakeUser(), fetch_error, []

    def is_ready(self):
        return self._ready

    async def fetch_user(self, discord_id):
        self.fetched.append(discord_id)
        if self.fetch_error:
            raise self.fetch_error
        return self.user


def http_error(cls, status=403):
    return cls(SimpleNamespace(status=status, reason="x"), "boom")


def attached(**kw):
    client = FakeClient(**kw)
    adapter = DiscordMessagingAdapter()
    adapter.attach(client)
    return adapter, client


class TestUnifiedMessage:
    def test_fields_and_validation(self) -> None:
        m = UnifiedMessage(user_id="u1", channel_type=ChannelType.DISCORD, content="hi", raw_payload={"id": "1"})
        assert (m.user_id, m.channel_type, m.content, m.raw_payload) == ("u1", ChannelType.DISCORD, "hi", {"id": "1"})
        for bad in ({"user_id": " "}, {"content": 5}, {"channel_type": "discord"}):
            args = {"user_id": "u", "channel_type": ChannelType.DISCORD, "content": "x", **bad}
            with pytest.raises((ValueError, TypeError)):
                UnifiedMessage(**args)

    def test_long_content_is_capped_and_metadata_stays_out_of_repr(self) -> None:
        m = UnifiedMessage(user_id="u", channel_type=ChannelType.CHASTER, content="x" * (MAX_CONTENT_CHARS + 5),
                           raw_payload={"secret_meta": "abc"})
        assert len(m.content) == MAX_CONTENT_CHARS and "abc" not in repr(m)

    def test_reply_goes_through_the_responder(self) -> None:
        sent = []

        async def responder(text):
            sent.append(text)
            return True

        m = UnifiedMessage(user_id="u", channel_type=ChannelType.DISCORD, content="q", _responder=responder)
        assert run(m.reply("answer")) is True and sent == ["answer"]

    def test_reply_without_a_responder_or_with_blank_text_or_failure_is_false(self) -> None:
        assert run(UnifiedMessage(user_id="u", channel_type=ChannelType.DISCORD, content="q").reply("x")) is False

        async def boom(text):
            raise RuntimeError("x")

        m = UnifiedMessage(user_id="u", channel_type=ChannelType.DISCORD, content="q", _responder=boom)
        assert run(m.reply("x")) is False

        async def ok(text):
            return True

        assert run(UnifiedMessage(user_id="u", channel_type=ChannelType.DISCORD, content="q", _responder=ok).reply("  ")) is False


class TestInterface:
    def test_the_base_class_cannot_be_instantiated(self) -> None:
        with pytest.raises(TypeError):
            BaseMessagingAdapter()                                      # abstract

    def test_discord_adapter_implements_it(self) -> None:
        assert issubclass(DiscordMessagingAdapter, BaseMessagingAdapter)
        assert DiscordMessagingAdapter.channel_type is ChannelType.DISCORD


class TestDiscordAdapter:
    def test_delivered(self) -> None:
        adapter, client = attached()
        assert run(adapter.send_message("4242", "hello")) is True
        assert client.fetched == [4242] and client.user.sent == ["hello"]

    def test_long_text_is_split(self) -> None:
        adapter, client = attached()
        assert run(adapter.send_message("1", "a" * (DISCORD_MAX_MESSAGE_CHARS * 2 + 10))) is True
        assert len(client.user.sent) == 3 and all(len(c) <= DISCORD_MAX_MESSAGE_CHARS for c in client.user.sent)

    @pytest.mark.parametrize("target,content", [("abc", "x"), ("", "x"), (None, "x"), ("1", " "), ("1", None)])
    def test_bad_input_is_false(self, target, content) -> None:
        adapter, client = attached()
        assert run(adapter.send_message(target, content)) is False and client.fetched == []

    def test_not_attached_or_not_ready_is_false(self) -> None:
        assert run(DiscordMessagingAdapter().send_message("1", "x")) is False
        adapter, _ = attached(ready=False)
        assert run(adapter.send_message("1", "x")) is False

    def test_closed_dms_unknown_user_and_http_failures_are_false(self) -> None:
        for kw in (dict(user=FakeUser(error=http_error(discord.Forbidden))),
                   dict(fetch_error=http_error(discord.NotFound, 404)),
                   dict(user=FakeUser(error=http_error(discord.HTTPException, 500))),
                   dict(user=FakeUser(error=RuntimeError("weird")))):
            adapter, _ = attached(**kw)
            assert run(adapter.send_message("1", "x")) is False

    def test_unify_builds_a_normalised_message_with_a_working_reply(self) -> None:
        adapter, client = attached()
        msg = SimpleNamespace(content="hello", author=SimpleNamespace(id=4242), id=99, channel=SimpleNamespace(id=7))
        unified = adapter.unify(msg, "u1")
        assert (unified.user_id, unified.channel_type, unified.content) == ("u1", ChannelType.DISCORD, "hello")
        assert unified.raw_payload == {"discord_user_id": "4242", "discord_message_id": "99", "discord_channel_id": "7"}
        assert run(unified.reply("pong")) is True and client.user.sent == ["pong"]

    def test_incoming_messages_go_to_the_handler_and_the_answer_is_sent_back(self) -> None:
        seen = []

        async def handler(m):
            seen.append(m.content)
            return "the answer"

        adapter = DiscordMessagingAdapter(handler=handler)
        client = FakeClient()
        adapter.attach(client)
        msg = SimpleNamespace(content="question", author=SimpleNamespace(id=4242), id=1, channel=SimpleNamespace(id=2))
        run(adapter.handle_incoming_message(adapter.unify(msg, "u1")))
        assert seen == ["question"] and client.user.sent == ["the answer"]

    def test_a_failing_handler_gets_a_neutral_reply_and_never_raises(self) -> None:
        async def handler(m):
            raise RuntimeError("secret detail")

        adapter = DiscordMessagingAdapter(handler=handler)
        client = FakeClient()
        adapter.attach(client)
        msg = SimpleNamespace(content="q", author=SimpleNamespace(id=4242), id=1, channel=SimpleNamespace(id=2))
        run(adapter.handle_incoming_message(adapter.unify(msg, "u1")))
        assert client.user.sent == [FAILURE_REPLY] and "secret" not in FAILURE_REPLY

    def test_no_handler_or_empty_answer_sends_nothing(self) -> None:
        adapter, client = attached()
        msg = SimpleNamespace(content="q", author=SimpleNamespace(id=4242), id=1, channel=SimpleNamespace(id=2))
        run(adapter.handle_incoming_message(adapter.unify(msg, "u1")))

        async def silent(m):
            return None

        quiet = DiscordMessagingAdapter(handler=silent)
        quiet.attach(client)
        run(quiet.handle_incoming_message(quiet.unify(msg, "u1")))
        assert client.user.sent == []


def test_split_message() -> None:
    assert split_message("short") == ["short"] and split_message("   ") == []
    text = "\n".join(["x" * 900] * 5)
    parts = split_message(text)
    assert all(len(p) <= DISCORD_MAX_MESSAGE_CHARS for p in parts) and "".join(parts).replace("\n", "") == text.replace("\n", "")


class TestRoutes:
    def test_default_is_discord_without_any_row(self, core) -> None:
        routes = MessageRouteRepository(core.db_path, core=core)
        assert routes.get_route("u1").channel is ChannelType.DISCORD

    def test_user_default_and_per_lock_override(self, core) -> None:
        routes = MessageRouteRepository(core.db_path, core=core)
        routes.set_route(user_id="u1", channel=ChannelType.CHASTER, chaster_conversation_id="conv-1", now=NOW)
        routes.set_route(user_id="u1", channel=ChannelType.DISCORD, chaster_lock_id="lock-9", now=NOW)
        assert routes.get_route("u1").channel is ChannelType.CHASTER
        assert routes.get_route("u1", "lock-9").channel is ChannelType.DISCORD
        assert routes.get_route("u1", "other-lock").channel is ChannelType.CHASTER
        assert routes.get_route("u1").chaster_conversation_id == "conv-1"

    def test_updates_replace_and_clear_removes(self, core) -> None:
        routes = MessageRouteRepository(core.db_path, core=core)
        routes.set_route(user_id="u1", channel=ChannelType.CHASTER, chaster_conversation_id="a", now=NOW)
        routes.set_route(user_id="u1", channel=ChannelType.CHASTER, chaster_conversation_id="b", now=NOW)
        assert routes.get_route("u1").chaster_conversation_id == "b"
        assert routes.clear_route(user_id="u1") is True and routes.clear_route(user_id="u1") is False
        assert routes.get_route("u1").channel is ChannelType.DISCORD

    def test_a_chaster_route_needs_a_conversation(self, core) -> None:
        routes = MessageRouteRepository(core.db_path, core=core)
        with pytest.raises(ValueError):
            routes.set_route(user_id="u1", channel=ChannelType.CHASTER, now=NOW)
        with pytest.raises(ValueError):
            routes.set_route(user_id=" ", channel=ChannelType.DISCORD, now=NOW)
        with pytest.raises(TypeError):
            routes.set_route(user_id="u1", channel="discord", now=NOW)

    def test_the_database_enforces_the_invariants(self, core) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            with core.transaction() as tx:
                tx.execute("INSERT INTO message_routes (user_id, lock_key, channel, updated_at) VALUES ('u','','chaster','x')")
        with pytest.raises(sqlite3.IntegrityError):
            with core.transaction() as tx:
                tx.execute("INSERT INTO message_routes (user_id, lock_key, channel, updated_at) VALUES ('u','','sms','x')")
        with pytest.raises(sqlite3.IntegrityError):
            with core.transaction() as tx:
                tx.execute("INSERT INTO message_routes (user_id, lock_key, channel, chaster_lock_id, updated_at) "
                           "VALUES ('u','wrong','discord','lock-1','x')")

    def test_discord_id_lookup(self, core) -> None:
        routes = MessageRouteRepository(core.db_path, core=core)
        assert routes.discord_id_for("u1") == "4242" and routes.discord_id_for("nobody") is None

    def test_the_migration_is_additive(self, core) -> None:
        with core.transaction() as tx:
            assert tx.fetch_one("SELECT version FROM schema_version WHERE version = 32")
            assert tx.fetch_one("SELECT 1 FROM sqlite_master WHERE name = 'message_routes'")


class FakeChasterAdapter(BaseMessagingAdapter):
    channel_type = ChannelType.CHASTER

    def __init__(self, result=True, raises=False) -> None:
        self.sent, self.result, self.raises = [], result, raises

    async def send_message(self, target_id, content):
        if self.raises:
            raise RuntimeError("x")
        self.sent.append((target_id, content))
        return self.result

    async def handle_incoming_message(self, payload):
        return None


class TestRouter:
    def _router(self, core, adapters, **kw):
        return MessageRouter(routes=MessageRouteRepository(core.db_path, core=core), adapters=adapters, **kw)

    def test_default_user_goes_to_discord(self, core) -> None:
        adapter, client = attached()
        assert run(self._router(core, [adapter]).send_to_user("u1", "hi")) is True
        assert client.fetched == [4242] and client.user.sent == ["hi"]

    def test_a_chaster_preference_uses_the_chaster_adapter_and_its_conversation(self, core) -> None:
        MessageRouteRepository(core.db_path, core=core).set_route(
            user_id="u1", channel=ChannelType.CHASTER, chaster_conversation_id="conv-7", now=NOW)
        discord_adapter, client = attached()
        chaster = FakeChasterAdapter()
        assert run(self._router(core, [discord_adapter, chaster]).send_to_user("u1", "note")) is True
        assert chaster.sent == [("conv-7", "note")] and client.user.sent == []

    def test_a_per_lock_route_wins(self, core) -> None:
        routes = MessageRouteRepository(core.db_path, core=core)
        routes.set_route(user_id="u1", channel=ChannelType.CHASTER, chaster_conversation_id="c", now=NOW)
        routes.set_route(user_id="u1", channel=ChannelType.DISCORD, chaster_lock_id="L", now=NOW)
        discord_adapter, client = attached()
        chaster = FakeChasterAdapter()
        router = self._router(core, [discord_adapter, chaster])
        assert run(router.send_to_user("u1", "x", chaster_lock_id="L")) is True and client.user.sent == ["x"] and chaster.sent == []

    def test_a_missing_adapter_is_not_silently_redirected(self, core) -> None:
        MessageRouteRepository(core.db_path, core=core).set_route(
            user_id="u1", channel=ChannelType.CHASTER, chaster_conversation_id="c", now=NOW)
        adapter, client = attached()
        assert run(self._router(core, [adapter]).send_to_user("u1", "x")) is False and client.user.sent == []

    def test_fallback_to_discord_is_opt_in(self, core) -> None:
        MessageRouteRepository(core.db_path, core=core).set_route(
            user_id="u1", channel=ChannelType.CHASTER, chaster_conversation_id="c", now=NOW)
        adapter, client = attached()
        chaster = FakeChasterAdapter(result=False)
        assert run(self._router(core, [adapter, chaster], fallback_to_discord=True).send_to_user("u1", "x")) is True
        assert client.user.sent == ["x"]
        adapter2, client2 = attached()
        assert run(self._router(core, [adapter2, FakeChasterAdapter(result=False)]).send_to_user("u1", "x")) is False

    def test_unknown_user_and_adapter_exceptions_are_false(self, core) -> None:
        adapter, _ = attached()
        assert run(self._router(core, [adapter]).send_to_user("nobody", "x")) is False
        MessageRouteRepository(core.db_path, core=core).set_route(
            user_id="u1", channel=ChannelType.CHASTER, chaster_conversation_id="c", now=NOW)
        assert run(self._router(core, [adapter, FakeChasterAdapter(raises=True)]).send_to_user("u1", "x")) is False

    def test_send_blocking_bridges_a_worker_thread(self, core) -> None:
        loop = asyncio.new_event_loop()
        t = threading.Thread(target=loop.run_forever, daemon=True)
        t.start()
        try:
            adapter, client = attached()
            router = self._router(core, [adapter])
            assert router.send_blocking("u1", "from a thread", loop=loop, timeout=5) is True
            assert client.user.sent == ["from a thread"]
            assert router.send_blocking("nobody", "x", loop=loop, timeout=5) is False
        finally:
            loop.call_soon_threadsafe(loop.stop)
            t.join(timeout=5)
            loop.close()
