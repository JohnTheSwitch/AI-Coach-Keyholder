"""
conversation_engine/context_providers/held_lock_provider.py

Bug fix 2026-11-16: the owner's Discord account is connected to the keyholder's Chaster account, so the model saw no lock of his and told him
"You're not literally locked". This provider gives the keyholder's latest observation of his lock (status and whether it is frozen), labelled
provider-reported. Read-only, through an injected callable, so conversation_engine imports nothing from system/ or chaster/.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from conversation_engine.models import ConversationContextFragment

__all__ = ["HeldLockContextProvider"]


class HeldLockContextProvider:
    namespace = "held_lock"

    def __init__(self, *, get_latest: Callable[[], object | None]) -> None:
        self._get = get_latest

    def provide_context(self, *, subject_key: str, now: datetime) -> ConversationContextFragment | None:
        obs = self._get()
        if obs is None:
            return None
        status = getattr(getattr(obs, "status", None), "value", None)
        if status not in ("locked", "unlocked"):
            return None
        return ConversationContextFragment(namespace=self.namespace, data={"status": status, "frozen": bool(getattr(obs, "is_frozen", False))})
