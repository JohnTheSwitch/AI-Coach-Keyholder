"""
messaging/adapter.py

The Strategy interface of the messaging layer (Phase B). One adapter per channel; the application never touches a channel API
directly. `send_message` is best-effort and returns a boolean: it NEVER raises into a caller (the periodic cycle, a reply path).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from messaging.models import ChannelType, UnifiedMessage

__all__ = ["BaseMessagingAdapter"]


class BaseMessagingAdapter(ABC):
    channel_type: ChannelType

    @abstractmethod
    async def send_message(self, target_id: str, content: str) -> bool:
        """Delivers `content` to a channel-specific target (a Discord user id, a Chaster conversation id).
        True only when the channel accepted the message."""

    @abstractmethod
    async def handle_incoming_message(self, payload: UnifiedMessage) -> None:
        """Passes a normalised incoming message on to the application. Must not raise."""
