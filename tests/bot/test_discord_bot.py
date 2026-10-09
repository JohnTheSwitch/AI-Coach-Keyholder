"""
tests/bot/test_discord_bot.py

Tests the adapter's own logic (DM filtering, message conversion, error
handling) WITHOUT a live Discord connection -- discord.Client.on_message()
is a plain coroutine method; calling it directly with a fake message
object (via asyncio.run(), stdlib only -- pytest-asyncio is not a
dependency of this project) exercises the exact same code path a real
gateway event would, without needing bot.run()/a network connection at
all.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from application.service import ApplicationService
from bot.discord_bot import build_bot
from core.config import Config
from database.database import Database
from infrastructure.clock import FrozenClock
from infrastructure.database import Database as CoreDatabase

FIXED_TIME = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _run(coro):
    return asyncio.run(coro)


def _apply_migrations(core: CoreDatabase) -> None:
    migrations_dir = Path(__file__).parent.parent.parent / "database" / "migrations"
    with core.raw_connection() as conn:
        for path in sorted(migrations_dir.glob("*.sql")):
            conn.executescript(path.read_text(encoding="utf-8"))


@pytest.fixture
def core(tmp_path: Path) -> CoreDatabase:
    c = CoreDatabase(tmp_path / "test.db")
    _apply_migrations(c)
    return c


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(discord_token="test-token", db_path=tmp_path / "test.db")


@pytest.fixture
def bot(config: Config, core: CoreDatabase):
    db = Database(config.db_path, core=core)
    clock = FrozenClock(FIXED_TIME)
    application_service = ApplicationService(config.db_path, core=core)
    return build_bot(config, db, clock, application_service)


def _fake_dm_message(content: str, author_id: int = 12345, message_id: int = 1) -> Mock:
    message = Mock()
    message.author = Mock()
    message.author.id = author_id
    message.content = content
    message.id = message_id
    message.channel = Mock(spec=discord.DMChannel)
    message.channel.id = 999
    message.channel.send = AsyncMock(return_value=Mock(id=message_id + 1))
    return message


def _fake_guild_message(content: str) -> Mock:
    message = Mock()
    message.author = Mock()
    message.author.id = 12345
    message.content = content
    message.channel = Mock(spec=discord.TextChannel)  # NOT a DMChannel
    message.channel.send = AsyncMock()
    return message


def _complete_onboarding(bot, *, author_id: int = 12345) -> None:
    """Drives a fresh user through all three onboarding steps via the
    real bot.on_message() path -- every TestMessageFlow/
    TestAdapterLevelErrorHandling test predates onboarding and assumed
    a fresh user reached the command router immediately; onboarding
    now intercepts first."""
    for i, text in enumerate(["anything", "english", "neutral", "alex"]):
        _run(bot.on_message(_fake_dm_message(text, author_id=author_id, message_id=100 + i)))


class TestDMFiltering:
    def test_dm_messages_get_a_reply(self, bot) -> None:
        message = _fake_dm_message("help")
        _run(bot.on_message(message))
        message.channel.send.assert_called_once()

    def test_guild_channel_messages_are_ignored(self, bot) -> None:
        """Per this phase's explicit scope: Discord communication only via DM."""
        message = _fake_guild_message("help")
        _run(bot.on_message(message))
        message.channel.send.assert_not_called()

    def test_bots_own_messages_are_ignored(self, bot, monkeypatch) -> None:
        message = _fake_dm_message("help")
        # discord.Client.user is a read-only property (backed by internal
        # connection state) -- overridden at the class level for this
        # test only, since there is no public setter to simulate "the
        # bot has logged in as this account."
        monkeypatch.setattr(type(bot), "user", property(lambda self: message.author))
        _run(bot.on_message(message))
        message.channel.send.assert_not_called()


class TestMessageFlow:
    def test_help_command_reaches_the_application_service_and_replies(self, bot) -> None:
        _complete_onboarding(bot)
        message = _fake_dm_message("help")
        _run(bot.on_message(message))
        reply_text = message.channel.send.call_args[0][0]
        assert "status" in reply_text.lower()

    def test_status_command_reaches_a_real_domain_module(self, bot) -> None:
        _complete_onboarding(bot)
        message = _fake_dm_message("status")
        _run(bot.on_message(message))
        reply_text = message.channel.send.call_args[0][0]
        assert "no active penalty window" in reply_text.lower()

    def test_both_sides_of_the_exchange_are_logged(self, bot, core: CoreDatabase) -> None:
        message = _fake_dm_message("help")
        _run(bot.on_message(message))
        with core.transaction() as tx:
            rows = tx.fetch_all("SELECT * FROM conversation_messages ORDER BY created_at")
        roles = [r["role"] for r in rows]
        assert "user" in roles
        assert "assistant" in roles


class TestAdapterLevelErrorHandling:
    def test_a_failure_in_audit_logging_does_not_prevent_a_real_reply(self, bot) -> None:
        """Audit logging is best-effort -- a failure there must never
        replace the real reply with the generic fallback (found while
        writing this test: the first implementation coupled the two,
        so a logging failure produced the generic error instead of the
        real 'help' response)."""
        _complete_onboarding(bot)
        message = _fake_dm_message("help")
        bot.db.save_conversation_message = Mock(side_effect=RuntimeError("simulated DB failure"))

        _run(bot.on_message(message))

        reply_text = message.channel.send.call_args[0][0]
        assert "available commands" in reply_text.lower()  # the REAL reply, not the fallback

    def test_a_failure_reaching_the_application_service_yields_a_safe_reply(self, bot) -> None:
        """Simulates a failure in the adapter's own call into the
        application layer -- the adapter's try/except around that call
        is a second, independent safety net on top of
        ApplicationService.handle_message()'s own internal one."""
        message = _fake_dm_message("help")
        bot.application_service.handle_message = Mock(side_effect=RuntimeError("simulated failure"))

        _run(bot.on_message(message))

        reply_text = message.channel.send.call_args[0][0]
        assert "went wrong" in reply_text.lower()
        assert "RuntimeError" not in reply_text  # never leaks internals to the user

    def test_a_discord_send_failure_does_not_corrupt_onboarding_state(self, bot, core: CoreDatabase) -> None:
        """The onboarding write always happens before send() is even
        attempted (application/onboarding_service.py's own write-
        before-send discipline) -- a failure sending the confirmation
        must never leave the persisted onboarding_step out of sync
        with what was actually processed."""
        # First message (creates the row at LANGUAGE, shows the prompt) -- must succeed normally.
        _run(bot.on_message(_fake_dm_message("anything", author_id=555, message_id=1)))

        # Second message answers LANGUAGE -- simulate Discord itself
        # failing to deliver the AI_GENDER prompt that should follow.
        failing_message = _fake_dm_message("english", author_id=555, message_id=2)
        failing_message.channel.send = AsyncMock(side_effect=RuntimeError("simulated Discord API failure"))

        _run(bot.on_message(failing_message))  # must not raise -- send() is wrapped

        with core.transaction() as tx:
            row = tx.fetch_one(
                "SELECT up.* FROM user_preferences up "
                "JOIN user_channel_identities uci ON uci.user_account_id = up.user_id "
                "WHERE uci.external_id = '555'"
            )
        assert row["onboarding_step"] == "ai_gender"  # the write succeeded despite the failed send
        assert row["language"] == "en"

    def test_no_discord_token_appears_in_any_error_reply(self, bot, monkeypatch) -> None:
        """A generic failure's reply text must never echo configuration
        (e.g. the bot's own token, however that failure was triggered)."""
        message = _fake_dm_message("help")

        def boom(*args, **kwargs):
            raise RuntimeError(f"failed using token={bot.config.discord_token}")

        monkeypatch.setattr(bot.application_service, "handle_message", boom)
        _run(bot.on_message(message))

        reply_text = message.channel.send.call_args[0][0]
        assert bot.config.discord_token not in reply_text
        assert "test-token" not in reply_text


class TestModeCommandsThroughTheRealAdapter:
    def test_discord_message_id_flows_through_to_the_persisted_consent_reference(self, bot, core: CoreDatabase) -> None:
        """Proves external_message_id genuinely flows Discord message.id
        -> IncomingMessage -> RequestContext -> the consent reference
        actually persisted -- not merely that the field exists."""
        _complete_onboarding(bot)
        _run(bot.on_message(_fake_dm_message("mode request advanced", author_id=12345, message_id=555)))

        with core.transaction() as tx:
            row = tx.fetch_one(
                "SELECT * FROM mode_transition_requests WHERE status NOT IN ('cancelled', 'completed', 'invalidated')",
            )
        assert row["requested_via_consent_id"] == "discord_message:555"

    def test_a_send_failure_after_mode_request_does_not_corrupt_the_committed_request(self, bot, core: CoreDatabase) -> None:
        """Mirrors the existing onboarding send-failure test -- the
        write (creating the mode transition request) happens before
        send() is attempted, so a failed send leaves the persisted
        state correct and unaffected."""
        _complete_onboarding(bot)
        failing_message = _fake_dm_message("mode request advanced", author_id=12345, message_id=777)
        failing_message.channel.send = AsyncMock(side_effect=RuntimeError("simulated Discord API failure"))

        _run(bot.on_message(failing_message))  # must not raise -- send() is wrapped

        with core.transaction() as tx:
            row = tx.fetch_one(
                "SELECT * FROM mode_transition_requests WHERE status NOT IN ('cancelled', 'completed', 'invalidated')",
            )
        assert row is not None
        assert row["requested_via_consent_id"] == "discord_message:777"
        assert row["status"] == "waiting"


class TestBuildDiscordEmbed:
    """`bot.discord_bot._build_discord_embed()` -- translates the
    channel-agnostic `application.models.EmbedSpec` into a real
    `discord.Embed`. The only place in this project that constructs
    a discord.Embed at all -- application/ itself never imports
    discord, per its own header docstring."""

    def test_title_and_description_carry_over(self) -> None:
        from application.models import EmbedSpec
        from bot.discord_bot import _build_discord_embed
        embed = _build_discord_embed(EmbedSpec(title="My Title", description="My description", color="red"))
        assert embed.title == "My Title"
        assert embed.description == "My description"

    def test_none_description_is_accepted(self) -> None:
        from application.models import EmbedSpec
        from bot.discord_bot import _build_discord_embed
        embed = _build_discord_embed(EmbedSpec(title="My Title", color="blue"))  # description defaults to None
        assert embed.title == "My Title"

    def test_red_blue_green_map_to_distinct_real_colors(self) -> None:
        from application.models import EmbedSpec
        from bot.discord_bot import _build_discord_embed
        red = _build_discord_embed(EmbedSpec(title="t", color="red"))
        blue = _build_discord_embed(EmbedSpec(title="t", color="blue"))
        green = _build_discord_embed(EmbedSpec(title="t", color="green"))
        assert red.color == discord.Color.red()
        assert blue.color == discord.Color.blue()
        assert green.color == discord.Color.green()
        assert len({red.color, blue.color, green.color}) == 3

    def test_unrecognized_color_name_does_not_raise(self) -> None:
        from application.models import EmbedSpec
        from bot.discord_bot import _build_discord_embed
        embed = _build_discord_embed(EmbedSpec(title="t", color="not-a-real-color"))  # must not raise
        assert embed.title == "t"

    def test_fields_carry_over_in_order(self) -> None:
        from application.models import EmbedField, EmbedSpec
        from bot.discord_bot import _build_discord_embed
        spec = EmbedSpec(title="t", color="green", fields=(EmbedField(name="A", value="1"), EmbedField(name="B", value="2")))
        embed = _build_discord_embed(spec)
        assert [f.name for f in embed.fields] == ["A", "B"]
        assert [f.value for f in embed.fields] == ["1", "2"]

    def test_empty_fields_produces_no_embed_fields(self) -> None:
        from application.models import EmbedSpec
        from bot.discord_bot import _build_discord_embed
        embed = _build_discord_embed(EmbedSpec(title="t", color="blue"))
        assert len(embed.fields) == 0


class TestEmbedSendingThroughTheRealAdapter:
    """Confirms on_message() actually sends via `embed=` when
    OutgoingMessage carries one, and via plain text otherwise --
    mocking only ApplicationService.handle_message()'s return value,
    exercising the real adapter send logic around it."""

    def test_an_outgoing_message_with_an_embed_is_sent_as_embed(self, bot) -> None:
        from unittest.mock import patch

        from application.models import EmbedSpec, OutgoingMessage
        _complete_onboarding(bot)
        message = _fake_dm_message("chaster status", message_id=200)
        fake_outgoing = OutgoingMessage(text="fallback text", embed=EmbedSpec(title="Test Embed", color="red"))
        with patch.object(bot.application_service, "handle_message", return_value=fake_outgoing):
            _run(bot.on_message(message))
        assert "embed" in message.channel.send.call_args.kwargs
        sent_embed = message.channel.send.call_args.kwargs["embed"]
        assert sent_embed.title == "Test Embed"
        # Plain text content is NOT also sent alongside the embed.
        assert message.channel.send.call_args.args == ()

    def test_an_outgoing_message_without_an_embed_is_sent_as_plain_text(self, bot) -> None:
        from unittest.mock import patch

        from application.models import OutgoingMessage
        _complete_onboarding(bot)
        message = _fake_dm_message("some ordinary command", message_id=201)
        fake_outgoing = OutgoingMessage(text="plain reply, no embed")
        with patch.object(bot.application_service, "handle_message", return_value=fake_outgoing):
            _run(bot.on_message(message))
        assert message.channel.send.call_args.args == ("plain reply, no embed",)
        assert "embed" not in message.channel.send.call_args.kwargs
