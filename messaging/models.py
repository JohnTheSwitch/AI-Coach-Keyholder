"""
messaging/models.py

The channel-neutral message value (v0.39 dual-role, Phase B). `UnifiedMessage` is what an adapter hands to the application when
something arrives, whatever the channel; `reply()` answers on the SAME channel through the adapter that produced it.

`raw_payload` holds channel metadata (ids, not secrets). It is excluded from repr/equality so a stray log line or a test failure
never prints message metadata, and the message text is capped at construction time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Awaitable, Callable

__all__ = ["ChannelType", "UnifiedMessage", "MAX_CONTENT_CHARS"]

MAX_CONTENT_CHARS = 4000


class ChannelType(StrEnum):
    DISCORD = "discord"
    CHASTER = "chaster"


@dataclass(frozen=True, kw_only=True)
class UnifiedMessage:
    user_id: str                                     # normalised local user id (never a channel-specific id)
    channel_type: ChannelType
    content: str
    raw_payload: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)
    _responder: Callable[[str], Awaitable[bool]] | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.user_id, str) or not self.user_id.strip():
            raise ValueError("user_id must be a non-empty string.")
        if not isinstance(self.channel_type, ChannelType):
            raise TypeError("channel_type must be a ChannelType.")
        if not isinstance(self.content, str):
            raise TypeError("content must be a string.")
        if len(self.content) > MAX_CONTENT_CHARS:
            object.__setattr__(self, "content", self.content[:MAX_CONTENT_CHARS])

    async def reply(self, text: str) -> bool:
        """Answers on the channel the message came from. False when there is no way to answer or the send failed."""
        if self._responder is None or not isinstance(text, str) or not text.strip():
            return False
        try:
            return bool(await self._responder(text))
        except Exception:
            return False
