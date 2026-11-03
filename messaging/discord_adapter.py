"""
messaging/discord_adapter.py

DiscordMessagingAdapter (Phase B): the Discord side of the messaging layer. It wraps the delivery the bot already did with
`user.send(...)` and the normalisation of an incoming direct message, behind the channel-neutral interface.

- `send_message(target_id, content, view=None)`: target_id is a Discord user id as text; long text is split into Discord-sized
  chunks; an optional discord.ui.View (buttons) is attached to the LAST chunk; returns False (never raises) when the client is
  missing/not ready, the id is malformed, DMs are closed or Discord fails.
- `unify(message, user_id)`: builds a `UnifiedMessage` from a discord.Message (ids only in `raw_payload`).
- `handle_incoming_message(unified)`: hands the message to the injected handler (the application pipeline) and sends the text
  it returns back through `unified.reply()`. A failing handler is logged and answered with a fixed neutral text.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

from messaging.adapter import BaseMessagingAdapter
from messaging.models import ChannelType, UnifiedMessage

logger = logging.getLogger("ai_coach_keyholder.messaging.discord")

__all__ = ["DiscordMessagingAdapter", "DISCORD_MAX_MESSAGE_CHARS", "split_message", "FAILURE_REPLY"]

DISCORD_MAX_MESSAGE_CHARS = 2000
FAILURE_REPLY = "Something went wrong handling that. It's been logged."


def split_message(text: str, limit: int = DISCORD_MAX_MESSAGE_CHARS) -> list[str]:
    """Splits on line breaks where possible so no chunk exceeds `limit`; never returns an empty chunk."""
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit)
        cut = limit if cut <= 0 else cut
        chunks.append(rest[:cut])
        rest = rest[cut:].lstrip("\n") if cut != limit else rest[cut:]
    if rest.strip():
        chunks.append(rest)
    return [c for c in chunks if c.strip()]


class DiscordMessagingAdapter(BaseMessagingAdapter):
    channel_type = ChannelType.DISCORD

    def __init__(self, *, handler: Callable[[UnifiedMessage], Awaitable[str | None]] | None = None) -> None:
        self._client = None
        self._handler = handler

    def attach(self, client) -> None:
        """The Discord client does not exist yet when the wiring is built; it is attached afterwards."""
        self._client = client

    @property
    def client(self):
        return self._client

    async def send_message(self, target_id: str, content: str, *, view=None) -> bool:
        client = self._client
        if client is None or not client.is_ready() or not isinstance(content, str) or not content.strip():
            return False
        try:
            discord_id = int(target_id)
        except (TypeError, ValueError):
            return False
        try:
            user = await client.fetch_user(discord_id)
            chunks = split_message(content)
            for i, chunk in enumerate(chunks):
                if view is not None and i == len(chunks) - 1:
                    await user.send(chunk, view=view)
                else:
                    await user.send(chunk)
            return True
        except Exception as exc:       # Forbidden (DMs closed), NotFound, HTTP failures: all simply "not delivered"
            logger.warning("Discord delivery failed (%s).", type(exc).__name__)
            return False

    def unify(self, message, user_id: str) -> UnifiedMessage:
        async def respond(text: str) -> bool:
            return await self.send_message(str(message.author.id), text)

        return UnifiedMessage(
            user_id=user_id, channel_type=ChannelType.DISCORD, content=message.content or "",
            raw_payload={"discord_user_id": str(message.author.id), "discord_message_id": str(message.id),
                         "discord_channel_id": str(message.channel.id)},
            _responder=respond,
        )

    async def handle_incoming_message(self, payload: UnifiedMessage) -> None:
        if self._handler is None:
            return
        try:
            text = await self._handler(payload)
        except Exception:
            logger.exception("The message handler failed for a Discord message.")
            text = FAILURE_REPLY
        if text:
            await payload.reply(text)
