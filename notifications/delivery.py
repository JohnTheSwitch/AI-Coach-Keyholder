"""
notifications/delivery.py

The one channel decision for proactive messages. A plain notice follows the user's stored channel preference through `MessageRouter`, Chaster chat included.
A card with buttons or an embed always goes to Discord: Chaster chat can show neither, and a decision the user cannot press is worse than a later one.

The channel objects are injected, so this module imports no Discord type (`embed_for` and `view_for` turn the neutral specs into the channel's own).
"""

from __future__ import annotations

from typing import Callable

from messaging.discord_adapter import DmOutcome
from notifications.queue import DeliveryResult, Notification

__all__ = ["NotificationDelivery"]

_RESULT = {DmOutcome.DELIVERED: DeliveryResult.DELIVERED, DmOutcome.UNDELIVERABLE: DeliveryResult.UNDELIVERABLE, DmOutcome.UNKNOWN: DeliveryResult.UNKNOWN}


class NotificationDelivery:
    def __init__(self, *, discord, discord_id_for: Callable[[str], str | None], router=None, embed_for: Callable | None = None,
                 view_for: Callable | None = None) -> None:
        self._discord = discord
        self._discord_id_for = discord_id_for
        self._router = router
        self._embed_for = embed_for
        self._view_for = view_for

    async def __call__(self, notification: Notification) -> DeliveryResult:
        if notification.embed is None and not notification.actions and self._router is not None:
            sent = await self._router.send_to_user(notification.user_id, notification.text)
            return DeliveryResult.DELIVERED if sent else DeliveryResult.UNKNOWN
        target = self._discord_id_for(notification.user_id)
        if not target:
            return DeliveryResult.UNDELIVERABLE
        embed = self._embed_for(notification.embed) if notification.embed is not None and self._embed_for is not None else None
        view = self._view_for(notification.actions) if notification.actions and self._view_for is not None else None
        return _RESULT[await self._discord.send_outcome(target, notification.text, view=view, embed=embed)]
