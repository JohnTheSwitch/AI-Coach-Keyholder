"""
messaging/discord_adapter.py

DiscordMessagingAdapter (Phase B): the Discord side of the messaging layer. It wraps the delivery the bot already did with
`user.send(...)` and the normalisation of an incoming direct message, behind the channel-neutral interface.

- `send_message(target_id, content, view=None)`: target_id is a Discord user id as text; long text is split into Discord-sized
  chunks; an optional discord.ui.View (buttons) is attached to the LAST chunk; with an optional discord.Embed the embed is sent
  INSTEAD of the text (one message, with the view); returns False (never raises) when the client is missing/not ready, the id is
  malformed, DMs are closed or Discord fails.
- `unify(message, user_id)`: builds a `UnifiedMessage` from a discord.Message (ids only in `raw_payload`).
- `handle_incoming_message(unified)`: hands the message to the injected handler (the application pipeline) and sends the text
  it returns back through `unified.reply()`. A failing handler is logged and answered with a fixed neutral text.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import Awaitable, Callable

from messaging.adapter import BaseMessagingAdapter
from messaging.models import ChannelType, UnifiedMessage

logger = logging.getLogger("ai_coach_keyholder.messaging.discord")

__all__ = ["DiscordMessagingAdapter", "DISCORD_MAX_MESSAGE_CHARS", "split_message", "FAILURE_REPLY", "DmOutcome", "send_dm"]

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


class DmOutcome(StrEnum):
    DELIVERED = "delivered"
    UNDELIVERABLE = "undeliverable"                # provably not sent: DMs closed, or no such user
    UNKNOWN = "unknown"                            # a timeout or server error: it may or may not have arrived


async def send_dm(client, discord_id: int, content: str, *, view=None, embed=None) -> DmOutcome:
    """The only place in the project that opens a DM. Everything proactive ends here, the check-in prompts included. The exception names are compared because
    this package stays free of `discord` imports; a chunked message that fails half way is UNKNOWN, since part of it arrived."""
    try:
        user = await client.fetch_user(discord_id)
        if embed is not None:                      # Phase J1: the embed carries the whole message; `content` is the fallback text
            await user.send(embed=embed, **({"view": view} if view is not None else {}))
            return DmOutcome.DELIVERED
        chunks = split_message(content)
        for i, chunk in enumerate(chunks):
            if view is not None and i == len(chunks) - 1:
                await user.send(chunk, view=view)
            else:
                await user.send(chunk)
        return DmOutcome.DELIVERED
    except Exception as exc:
        logger.warning("Discord delivery failed (%s).", type(exc).__name__)
        return DmOutcome.UNDELIVERABLE if type(exc).__name__ in ("Forbidden", "NotFound") else DmOutcome.UNKNOWN


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

    async def send_message(self, target_id: str, content: str, *, view=None, embed=None) -> bool:
        return await self.send_outcome(target_id, content, view=view, embed=embed) is DmOutcome.DELIVERED

    async def send_outcome(self, target_id: str, content: str, *, view=None, embed=None) -> DmOutcome:
        client = self._client
        if client is None or not client.is_ready() or not isinstance(content, str) or not content.strip():
            return DmOutcome.UNKNOWN                # not ready yet is a matter of time, not a closed door
        try:
            discord_id = int(target_id)
        except (TypeError, ValueError):
            return DmOutcome.UNDELIVERABLE
        return await send_dm(client, discord_id, content, view=view, embed=embed)

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
