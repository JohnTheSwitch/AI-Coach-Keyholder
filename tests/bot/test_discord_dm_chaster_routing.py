"""
tests/bot/test_discord_dm_chaster_routing.py

The FULL Discord DM path for natural-language Chaster status questions:
discord on_message -> ApplicationService.handle_message -> deterministic
guardrail -> embed sent to the DM channel.

Built the way a misconfigured live bot is built: the conversation
engine has NO tool executor (CHASTER_LLM_TOOLS_ENABLED off). The model is
a plain one that returns the `lock report` advice seen in production.

Hybrid design: the application reads the Chaster data itself. Only when
real lock data was read does the model phrase the reply (from verified
context, tools off). In every other state -- not configured, not
connected, reconnect required, outage -- the model is never called and a
deterministic embed is sent.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import discord
import pytest
from cryptography.fernet import Fernet

from application.service import ApplicationService
from bot.discord_bot import build_bot
from chaster.lock_client import ChasterLockApiError, ChasterLockClient
from chaster.oauth_client import ChasterOAuthClient, ChasterTokenExchangeError
from chaster.repository import ChasterConnectionRepository
from chaster.token_encryptor import TokenEncryptor
from chaster.token_manager import ChasterTokenManager
from conversation_engine.engine import ConversationEngine
from conversation_engine.subject_queue import SubjectConversationQueue
from core.config import Config
from database.database import Database
from infrastructure.clock import FrozenClock
from infrastructure.database import Database as CoreDatabase
from memory_system.working_memory import InMemoryWorkingMemory

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
PHRASE = "Jak to vypadá s mým zámkem?"
AUTHOR_ID = 42
LOCK = {"_id": "l1", "title": "Weekend lock", "status": "locked", "endDate": (NOW + timedelta(days=2, hours=3, minutes=4)).isoformat()}


class HallucinatingModel:
    """Plain (non-tool-capable) model that gives the production answer."""
    def __init__(self) -> None:
        self.calls = 0
        self.requests = []
        self.text = "You can report your state with the `lock report` command."

    def generate(self, *, request) -> str:
        self.calls += 1
        self.requests.append(request)
        return self.text


def _run(coro):
    return asyncio.run(coro)


def _dm(content: str, message_id: int) -> Mock:
    message = Mock()
    message.author = Mock()
    message.author.id = AUTHOR_ID
    message.content = content
    message.id = message_id
    message.channel = Mock(spec=discord.DMChannel)
    message.channel.id = 999
    message.channel.send = AsyncMock(return_value=Mock(id=message_id + 1))
    return message


@pytest.fixture
def core(tmp_path: Path) -> CoreDatabase:
    c = CoreDatabase(tmp_path / "test.db")
    migrations_dir = Path(__file__).parent.parent.parent / "database" / "migrations"
    with c.raw_connection() as conn:
        for path in sorted(migrations_dir.glob("*.sql")):
            conn.executescript(path.read_text(encoding="utf-8"))
    return c


def _bot(core, tmp_path, *, chaster_configured: bool):
    model = HallucinatingModel()
    memory = InMemoryWorkingMemory(max_exchanges_per_subject=10, max_characters_per_subject=8000)
    engine = ConversationEngine(  # NO tool_executor: the "tool inactive" production state
        model=model, working_memory_reader=memory, working_memory_writer=memory, queue=SubjectConversationQueue(),
    )
    kwargs = {}
    if chaster_configured:
        connections = ChasterConnectionRepository(tmp_path / "test.db", core=core, encryptor=TokenEncryptor(Fernet.generate_key()))
        oauth = ChasterOAuthClient(client_id="cid", client_secret="secret", redirect_uri="https://example.com/cb")
        kwargs = dict(
            chaster_connections=connections, chaster_oauth_client=oauth,
            chaster_token_manager=ChasterTokenManager(connections=connections, oauth_client=oauth),
        )
    service = ApplicationService(tmp_path / "test.db", core=core, conversation_engine=engine, **kwargs)
    config = Config(discord_token="test-token", db_path=tmp_path / "test.db")
    bot = build_bot(config, Database(config.db_path, core=core), FrozenClock(NOW), service)
    for i, text in enumerate(("anything", "english", "neutral", "alex")):
        _run(bot.on_message(_dm(text, 100 + i)))
    return bot, service, model


def _link(service) -> None:
    user = service.user_service.get_or_create_user("discord", str(AUTHOR_ID), now=NOW)
    service.chaster_connections.create_or_replace(
        user_id=user.id, chaster_account_id="acc", chaster_username="wearer",
        access_token="real-access-token", refresh_token="real-refresh-token",
        access_token_expires_at=NOW + timedelta(hours=1), refresh_token_expires_at=NOW + timedelta(days=30),
        granted_scopes=("locks",), now=NOW,
    )


def _ask(bot, text: str = PHRASE) -> Mock:
    message = _dm(text, 500)
    _run(bot.on_message(message))
    message.channel.send.assert_called_once()
    return message


def _sent_embed(message: Mock) -> discord.Embed:
    assert message.channel.send.call_args.args == ()
    return message.channel.send.call_args.kwargs["embed"]


class TestDiscordDmChasterGuardrail:
    def test_unlinked_account_gets_a_red_not_connected_embed_not_llm_text(self, core, tmp_path) -> None:
        bot, _, model = _bot(core, tmp_path, chaster_configured=True)
        embed = _sent_embed(_ask(bot))
        assert embed.title == "Chaster Account Not Connected"
        assert "chaster connect" in embed.description
        assert embed.color == discord.Color.red()
        assert model.calls == 0

    def test_linked_account_gets_a_model_written_text_reply_from_verified_data(self, core, tmp_path) -> None:
        bot, service, model = _bot(core, tmp_path, chaster_configured=True)
        _link(service)
        model.text = "Your Weekend lock has 2 days and 3 hours left. Hang in there."
        with patch.object(ChasterLockClient, "list_active_locks", return_value=[LOCK]) as locks:
            message = _ask(bot)
        locks.assert_called_once()
        assert message.channel.send.call_args.args == (model.text,)  # plain text, no embed
        assert "embed" not in message.channel.send.call_args.kwargs
        messages = model.requests[0].messages
        assert messages[-1].role.value == "user" and messages[-2].role.value == "system"
        assert "VERIFIED CHASTER DATA" in messages[-2].content and "2 days, 3 hours and 4 minutes" in messages[-2].content

    def test_model_failure_with_real_data_falls_back_to_the_deterministic_green_embed(self, core, tmp_path) -> None:
        from conversation_engine.model_types import LLMGenerationError
        bot, service, model = _bot(core, tmp_path, chaster_configured=True)
        _link(service)
        model.generate = lambda *, request: (_ for _ in ()).throw(LLMGenerationError("ollama_unavailable"))  # type: ignore[method-assign]
        with patch.object(ChasterLockClient, "list_active_locks", return_value=[LOCK]):
            embed = _sent_embed(_ask(bot))
        assert embed.color == discord.Color.green() and "Weekend lock" in repr(embed.to_dict())

    def test_chaster_not_configured_on_the_bot_gets_an_embed_not_llm_text(self, core, tmp_path) -> None:
        bot, _, model = _bot(core, tmp_path, chaster_configured=False)
        embed = _sent_embed(_ask(bot))
        assert embed.title == "Chaster Not Configured" and embed.color == discord.Color.red()
        assert model.calls == 0

    def test_revoked_authorization_gets_a_reconnect_embed(self, core, tmp_path) -> None:
        bot, service, model = _bot(core, tmp_path, chaster_configured=True)
        _link(service)
        with patch.object(ChasterOAuthClient, "refresh_tokens", side_effect=ChasterTokenExchangeError("rejected", status_code=400)), \
             patch.object(ChasterLockClient, "list_active_locks", side_effect=ChasterLockApiError("expired", status_code=401)):
            embed = _sent_embed(_ask(bot))
        assert embed.title == "Chaster Reconnection Required"
        assert model.calls == 0

    def test_chaster_outage_gets_a_red_embed(self, core, tmp_path) -> None:
        bot, service, model = _bot(core, tmp_path, chaster_configured=True)
        _link(service)
        with patch.object(ChasterLockClient, "list_active_locks", side_effect=ChasterLockApiError("Chaster's locks endpoint returned HTTP 500.", status_code=500)):
            embed = _sent_embed(_ask(bot))
        assert embed.color == discord.Color.red() and model.calls == 0

    @pytest.mark.parametrize("text", [
        PHRASE, "Zkontroluj stav mého zámku přes Chaster", "How much time is left on my lock?", "Jsem zamčený?",
    ])
    def test_matching_phrases_when_not_linked_never_reach_the_model(self, core, tmp_path, text) -> None:
        bot, _, model = _bot(core, tmp_path, chaster_configured=True)
        _sent_embed(_ask(bot, text))
        assert model.calls == 0

    def test_non_chaster_chat_still_reaches_the_model(self, core, tmp_path) -> None:
        bot, _, model = _bot(core, tmp_path, chaster_configured=True)
        message = _ask(bot, "Jak se dnes máš?")
        assert model.calls == 1 and "embed" not in message.channel.send.call_args.kwargs

    def test_a_guardrail_reply_is_logged_for_both_sides(self, core, tmp_path) -> None:
        bot, _, _ = _bot(core, tmp_path, chaster_configured=True)
        _ask(bot)
        with core.raw_connection() as conn:
            user_rows = conn.execute("SELECT COUNT(*) FROM conversation_messages WHERE role='user' AND content=?", (PHRASE,)).fetchone()[0]
            assistant_rows = conn.execute("SELECT COUNT(*) FROM conversation_messages WHERE role='assistant' AND content LIKE '%Chaster%'").fetchone()[0]
        assert user_rows == 1 and assistant_rows >= 1
