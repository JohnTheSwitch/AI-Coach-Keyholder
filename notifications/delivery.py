"""
notifications/delivery.py

The one channel decision for proactive messages. A plain notice follows the user's stored channel preference through `MessageRouter`, Chaster chat included.
A card with buttons or an embed always goes to Discord: Chaster chat can show neither, and a decision the user cannot press is worse than a later one.

The channel objects are injected, so this module imports no Discord type (`embed_for` and `view_for` turn the neutral specs into the channel's own).

Owner, 2026-11-17 ("I want all of these"): with `style_for` (visuals/style.py), a notice of a known kind is sent as a coloured card when it goes to
Discord anyway: one that already goes there (buttons or an embed), or a plain one whose user reads notices on Discord. A plain notice for a
user who reads them in Chaster chat stays plain text there; the card never moves a message to another channel. `rich_for(spec, heading)`
builds the channel's card with its drawn picture.
"""

from __future__ import annotations

from typing import Callable

from messaging.discord_adapter import DmOutcome
from messaging.models import ChannelType
from notifications.queue import DeliveryResult, Notification

__all__ = ["NotificationDelivery"]

_RESULT = {DmOutcome.DELIVERED: DeliveryResult.DELIVERED, DmOutcome.UNDELIVERABLE: DeliveryResult.UNDELIVERABLE, DmOutcome.UNKNOWN: DeliveryResult.UNKNOWN}


class NotificationDelivery:
    def __init__(self, *, discord, discord_id_for: Callable[[str], str | None], router=None, embed_for: Callable | None = None,
                 view_for: Callable | None = None, style_for: Callable | None = None, rich_for: Callable | None = None,
                 heading_for: Callable[[str], str | None] | None = None) -> None:
        self._discord = discord
        self._discord_id_for = discord_id_for
        self._router = router
        self._embed_for = embed_for
        self._view_for = view_for
        self._style_for = style_for
        self._rich_for = rich_for
        self._heading_for = heading_for

    def _on_discord(self, notification: Notification) -> bool:
        if notification.embed is not None or notification.actions or self._router is None:
            return True
        try:
            return self._router.channel_for(notification.user_id) is ChannelType.DISCORD
        except Exception:
            return False                         # unknown: the plain notice keeps its usual path

    async def __call__(self, notification: Notification) -> DeliveryResult:
        spec = notification.embed
        if spec is None and self._style_for is not None and self._on_discord(notification):
            spec = self._style_for(notification.kind, notification.text, offer=bool(notification.actions))
        if spec is None and not notification.actions and self._router is not None:
            sent = await self._router.send_to_user(notification.user_id, notification.text)
            return DeliveryResult.DELIVERED if sent else DeliveryResult.UNKNOWN
        target = self._discord_id_for(notification.user_id)
        if not target:
            return DeliveryResult.UNDELIVERABLE
        embed = None
        if spec is not None:
            heading = self._heading_for(notification.kind) if self._heading_for is not None else None
            if self._rich_for is not None:
                embed = self._rich_for(spec, heading)
            elif self._embed_for is not None:
                embed = self._embed_for(spec)
        view = self._view_for(notification.actions) if notification.actions and self._view_for is not None else None
        return _RESULT[await self._discord.send_outcome(target, notification.text, view=view, embed=embed)]
