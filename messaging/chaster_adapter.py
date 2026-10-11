"""
messaging/chaster_adapter.py

ChasterMessagingAdapter (v0.39 dual-role, Phase C): the Chaster chat channel behind the channel-neutral interface.

OUTBOUND  `send_message(conversation_id, text)` -> `POST /conversations/{id}` with the bot (keyholder) account's token, scope
          `messaging`. Long text is split into conservative chunks (Chaster documents no length limit). Best effort: returns False,
          never raises. Runs the blocking HTTP call on a worker thread.
INBOUND   `handle_incoming_message(unified)` passes a normalised message to the injected handler and replies on the same
          conversation. The handler is the application's CONVERSATION-ONLY entry: a Chaster message is untrusted text and is never
          matched against commands, onboarding or approvals (see ApplicationService.handle_conversation_only).

Secrecy: this module imports nothing from the release engine, the rules engine or the Trust Manager (AST-guarded in tests), so no
release timing or plan data can reach a Chaster message through it; whatever the conversation engine says has already passed the
same guardrails and coarse context as on Discord.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from chaster.conversation_client import MESSAGING_SCOPE, ChasterConversationClient
from chaster.repository import ChasterConnectionRepository
from chaster.token_manager import ChasterTokenManager
from infrastructure.clock import Clock
from messaging.adapter import BaseMessagingAdapter
from messaging.discord_adapter import FAILURE_REPLY, split_message
from messaging.models import ChannelType, UnifiedMessage

logger = logging.getLogger("ai_coach_keyholder.messaging.chaster")

__all__ = ["ChasterMessagingAdapter", "CHASTER_CHUNK_CHARS"]

CHASTER_CHUNK_CHARS = 1500           # conservative: the OpenAPI document gives no maximum


class ChasterMessagingAdapter(BaseMessagingAdapter):
    channel_type = ChannelType.CHASTER

    def __init__(
        self, *, token_manager: ChasterTokenManager, client: ChasterConversationClient,
        connections: ChasterConnectionRepository, bot_user_id: str, clock: Clock,
        handler: Callable[[UnifiedMessage], Awaitable[str | None]] | None = None,
    ) -> None:
        if not bot_user_id or not bot_user_id.strip():
            raise ValueError("bot_user_id must be a non-empty string.")
        self._tokens = token_manager
        self._client = client
        self._connections = connections
        self._bot_user_id = bot_user_id
        self._clock = clock
        self._handler = handler

    async def send_message(self, target_id: str, content: str) -> bool:
        if not isinstance(target_id, str) or not target_id.strip() or not isinstance(content, str) or not content.strip():
            return False
        return await asyncio.to_thread(self._send_sync, target_id, content)

    def unify(self, *, user_id: str, conversation_id: str, message: dict) -> UnifiedMessage:
        async def respond(text: str) -> bool:
            return await self.send_message(conversation_id, text)

        text = message.get("message")
        return UnifiedMessage(
            user_id=user_id, channel_type=ChannelType.CHASTER, content=text if isinstance(text, str) else "",
            raw_payload={"chaster_conversation_id": conversation_id, "chaster_message_id": str(message.get("_id", ""))},
            _responder=respond,
        )

    async def handle_incoming_message(self, payload: UnifiedMessage) -> None:
        if self._handler is None:
            return
        try:
            text = await self._handler(payload)
        except Exception:
            logger.exception("The message handler failed for a Chaster message.")
            text = FAILURE_REPLY
        if text:
            await payload.reply(text)

    # ------------------------------------------------------------------ internals

    def _send_sync(self, conversation_id: str, content: str) -> bool:
        try:
            connection = self._connections.get(self._bot_user_id)
            if connection is None or MESSAGING_SCOPE not in connection.granted_scopes:
                logger.warning("Chaster messaging is unavailable: the bot account has no connection with the messaging scope.")
                return False
            for chunk in split_message(content, CHASTER_CHUNK_CHARS):
                self._tokens.call_with_token(
                    self._bot_user_id, now=self._clock.now(),
                    operation=lambda token, c=chunk: self._client.send_message(
                        access_token=token, conversation_id=conversation_id, message=c))
            return True
        except Exception as exc:
            logger.warning("Chaster delivery failed (%s).", type(exc).__name__)
            return False
