"""
conversation_engine/context_providers/address_provider.py

AddressContextProvider (owner, 2026-11-15/16; autonomous queue A3): how the keyholder addresses him, from visible facts only (an active
Penalty Window, a task verified in the last 24 hours), never Trust. Tone only: it gives the voice fixed words and changes nothing.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable

from conversation_engine.models import ConversationContextFragment

__all__ = ["AddressContextProvider"]

logger = logging.getLogger("ai_coach_keyholder.conversation")


class AddressContextProvider:
    namespace = "address"

    def __init__(self, *, window_active: Callable[[datetime], bool], praise_earned: Callable[[str, datetime], bool],
                 title: Callable[[str], str | None] = lambda user_id: None) -> None:
        self._window, self._praise, self._title = window_active, praise_earned, title

    def provide_context(self, *, subject_key: str, now: datetime) -> ConversationContextFragment | None:
        try:
            window = bool(self._window(now))
            praise = (not window) and bool(self._praise(subject_key, now))
            title = self._title(subject_key)
        except Exception:
            logger.exception("The address facts could not be read; the conversation goes on without them.")
            return None
        return ConversationContextFragment(namespace=self.namespace, data={"window": window, "praise": praise, "title": title})
