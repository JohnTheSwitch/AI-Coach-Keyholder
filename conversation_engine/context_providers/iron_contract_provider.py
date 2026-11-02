"""
conversation_engine/context_providers/iron_contract_provider.py

IronContractContextProvider (v0.39 Phase G6). When the wearer is locked and the Iron Contract is in force, the model sees `[STATE: LOCKED]` in the
authoritative application state (never inside the fenced memory data). The behaviour that goes with it is a guardrail (rule 7), not text in the context.
The state comes from an injected callable, so conversation_engine imports nothing from iron_contract.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from conversation_engine.models import ConversationContextFragment

__all__ = ["IronContractContextProvider"]


class IronContractContextProvider:
    namespace = "contract"

    def __init__(self, *, is_locked: Callable[[str], bool]) -> None:
        self._is_locked = is_locked

    def provide_context(self, *, subject_key: str, now: datetime) -> ConversationContextFragment | None:
        if self._is_locked(subject_key) is not True:
            return None
        return ConversationContextFragment(namespace=self.namespace, data={"state": "LOCKED"})
