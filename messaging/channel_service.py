"""
messaging/channel_service.py

ChannelPreferenceService (Phase C): the user's explicit opt-in to the Chaster chat channel and the way back. It is a deterministic
domain operation reached only through the `channel ...` commands (ApplicationService -> CommandRouter) and the local CLI, never from a
model and never from an inbound Chaster message.

Opt-in finds the existing conversation between the bot account and the user's own Chaster account
(`GET /conversations/by-user/{userId}`, scope `messaging`, with the bot's token). It does not create a conversation (that endpoint needs a
first message and an unspecified Turnstile token): the user starts the chat on Chaster once, then opts in. Opting out removes the route, so the
messages go back to Discord at once.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from chaster.conversation_client import MESSAGING_SCOPE, ChasterConversationClient
from chaster.repository import ChasterConnectionRepository
from chaster.token_manager import ChasterTokenManager
from messaging.models import ChannelType
from messaging.routes import MessageRouteRepository

logger = logging.getLogger("ai_coach_keyholder.messaging.channel_service")

__all__ = ["ChannelPreferenceService", "OptInResult", "OptInStatus"]


class OptInStatus(StrEnum):
    ENABLED = "enabled"
    DISABLED_BY_OWNER = "disabled_by_owner"        # ENABLE_CHASTER_MESSAGING is off
    NOT_CONNECTED = "not_connected"                # the user has no Chaster connection
    BOT_NOT_READY = "bot_not_ready"                # the bot account lacks a connection with the messaging scope
    NO_CONVERSATION = "no_conversation"            # nothing to send to yet: start a chat with the bot on Chaster first
    UNAVAILABLE = "unavailable"                    # Chaster failed


@dataclass(frozen=True)
class OptInResult:
    status: OptInStatus


class ChannelPreferenceService:
    def __init__(self, *, routes: MessageRouteRepository, connections: ChasterConnectionRepository | None,
                 token_manager: ChasterTokenManager | None, client: ChasterConversationClient, bot_user_id: str | None,
                 chaster_messaging_enabled: bool, chaster_account_of=None) -> None:
        """`connections` holds the BOT's connection (scope check); `chaster_account_of(user_id)` gives the user's Chaster account id. By default
        it is read from the user's own connection in `connections` (single-wearer); a tenant passes its confirmed pairing instead (Phase E5)."""
        self._routes = routes
        self._account_of = chaster_account_of or self._own_connection_account
        self._connections = connections
        self._tokens = token_manager
        self._client = client
        self._bot_user_id = bot_user_id
        self._enabled = chaster_messaging_enabled

    def current_channel(self, user_id: str) -> ChannelType:
        return self._routes.get_route(user_id).channel

    def opt_in_chaster(self, user_id: str, now: datetime) -> OptInResult:
        if not self._enabled:
            return OptInResult(OptInStatus.DISABLED_BY_OWNER)
        if self._connections is None or self._tokens is None or not self._bot_user_id:
            return OptInResult(OptInStatus.BOT_NOT_READY)
        user_account = self._account_of(user_id)
        if not user_account:
            return OptInResult(OptInStatus.NOT_CONNECTED)
        bot_connection = self._connections.get(self._bot_user_id)
        if bot_connection is None or MESSAGING_SCOPE not in bot_connection.granted_scopes:
            return OptInResult(OptInStatus.BOT_NOT_READY)
        try:
            conversation = self._tokens.call_with_token(
                self._bot_user_id, now=now,
                operation=lambda token: self._client.find_conversation_by_user(
                    access_token=token, chaster_user_id=user_account))
        except Exception as exc:
            logger.warning("Looking up the Chaster conversation failed (%s).", type(exc).__name__)
            return OptInResult(OptInStatus.UNAVAILABLE)
        if conversation is None:
            return OptInResult(OptInStatus.NO_CONVERSATION)
        self._routes.set_route(user_id=user_id, channel=ChannelType.CHASTER, chaster_conversation_id=conversation["_id"], now=now)
        return OptInResult(OptInStatus.ENABLED)

    def _own_connection_account(self, user_id: str) -> str | None:
        connection = self._connections.get(user_id) if self._connections is not None else None
        return None if connection is None else connection.chaster_account_id

    def opt_out(self, user_id: str) -> bool:
        """Back to Discord. True when a Chaster route existed."""
        route = self._routes.get_route(user_id)
        if route.channel is not ChannelType.CHASTER:
            return False
        return self._routes.clear_route(user_id=user_id)
