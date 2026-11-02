"""
conversation_engine/context_providers/coaching_memory_provider.py

CoachingMemoryContextProvider (v0.39 Phase F2). Hands the user's most recent Coaching Memory entries to the prompt builder, which renders them inside a fenced
`<context>` DATA block AFTER the system boundaries and the authoritative application state, never as instructions and never as application state. The
entries are passed in through an injected callable, so conversation_engine imports nothing from memory_system's storage.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from conversation_engine.models import ConversationContextFragment

__all__ = ["CoachingMemoryContextProvider", "MAX_RENDERED_ENTRIES"]

MAX_RENDERED_ENTRIES = 5
_ALLOWED = frozenset({"behavior_pattern", "stated_goal", "quote"})


class CoachingMemoryContextProvider:
    namespace = "coaching_memory"

    def __init__(self, *, get_entries: Callable[[str], list[tuple[str, str]]]) -> None:
        self._get_entries = get_entries

    def provide_context(self, *, subject_key: str, now: datetime) -> ConversationContextFragment | None:
        entries = [(c, t) for c, t in self._get_entries(subject_key)[:MAX_RENDERED_ENTRIES]
                   if c in _ALLOWED and isinstance(t, str) and t.strip()]
        if not entries:
            return None
        return ConversationContextFragment(
            namespace=self.namespace, data={"entries": [{"category": c, "content": t} for c, t in entries]})
