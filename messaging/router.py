"""
messaging/router.py

MessageRouter (Phase B): picks the adapter for a user from the stored channel preference and delivers. The target of a Discord
send is the user's Discord id; the target of a Chaster send is the stored conversation id. A preference whose adapter is not
registered (for example `chaster` before phase C) is NOT silently redirected: the send simply reports False and is logged,
unless `fallback_to_discord` is switched on. `send_blocking` is the bridge for worker threads (the periodic cycle).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging

from messaging.adapter import BaseMessagingAdapter
from messaging.models import ChannelType
from messaging.routes import MessageRouteRepository

logger = logging.getLogger("ai_coach_keyholder.messaging.router")

__all__ = ["MessageRouter"]


class MessageRouter:
    def __init__(self, *, routes: MessageRouteRepository, adapters: list[BaseMessagingAdapter],
                 fallback_to_discord: bool = False) -> None:
        self._routes = routes
        self._adapters = {a.channel_type: a for a in adapters}
        self._fallback = fallback_to_discord

    def adapter_for(self, channel: ChannelType) -> BaseMessagingAdapter | None:
        return self._adapters.get(channel)

    async def send_to_user(self, user_id: str, content: str, *, chaster_lock_id: str | None = None) -> bool:
        route = self._routes.get_route(user_id, chaster_lock_id)
        if await self._deliver(route.channel, user_id, content, route.chaster_conversation_id):
            return True
        if self._fallback and route.channel is not ChannelType.DISCORD:
            logger.warning("Delivery on %s failed; falling back to Discord.", route.channel.value)
            return await self._deliver(ChannelType.DISCORD, user_id, content, None)
        return False

    def send_blocking(self, user_id: str, content: str, *, loop: asyncio.AbstractEventLoop, timeout: float = 30.0,
                      chaster_lock_id: str | None = None) -> bool:
        """For worker threads: runs the send on the Discord client's own loop. False on timeout or any failure."""
        future = asyncio.run_coroutine_threadsafe(self.send_to_user(user_id, content, chaster_lock_id=chaster_lock_id), loop)
        try:
            return bool(future.result(timeout=timeout))
        except concurrent.futures.TimeoutError:
            future.cancel()
            return False
        except Exception:
            logger.exception("Routed delivery failed unexpectedly.")
            return False

    async def _deliver(self, channel: ChannelType, user_id: str, content: str, conversation_id: str | None) -> bool:
        adapter = self._adapters.get(channel)
        if adapter is None:
            logger.warning("No messaging adapter is registered for channel %s.", channel.value)
            return False
        target = self._routes.discord_id_for(user_id) if channel is ChannelType.DISCORD else conversation_id
        if not target:
            return False
        try:
            return bool(await adapter.send_message(target, content))
        except Exception:
            logger.exception("A messaging adapter raised; treating the message as not delivered.")
            return False
