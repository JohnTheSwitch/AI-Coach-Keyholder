"""
conversation_engine/context_providers/coaching_memory_provider.py

CoachingMemoryContextProvider (v0.39 Phase F2). Hands the user's most recent Coaching Memory entries to the prompt builder, which renders them inside a fenced
`<context>` DATA block AFTER the system boundaries and the authoritative application state, never as instructions and never as application state. The
entries are passed in through an injected callable, so conversation_engine imports nothing from memory_system's storage.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable

from conversation_engine.models import ConversationContextFragment

__all__ = ["CoachingMemoryContextProvider", "MAX_RENDERED_ENTRIES", "MAX_AGE"]

MAX_RENDERED_ENTRIES = 5
_ALLOWED = frozenset({"behavior_pattern", "stated_goal", "quote"})
# Owner, 2026-11-12: a one-off ("I got burned this Wednesday") must not be talked about for months. A note is shown with the day it was
# saved, and a note older than its category's age is no longer shown at all (it stays stored, and the user still sees and erases it).
# BOOTSTRAP_DEFAULT(owner=undecided, mechanism=code):
MAX_AGE = {"quote": timedelta(days=7), "behavior_pattern": timedelta(days=21), "stated_goal": timedelta(days=60)}


class CoachingMemoryContextProvider:
    namespace = "coaching_memory"

    def __init__(self, *, get_entries: Callable[[str], list[tuple]]) -> None:
        """`get_entries(user_id)` returns (category, text) or (category, text, saved_at) tuples, newest first."""
        self._get_entries = get_entries

    def provide_context(self, *, subject_key: str, now: datetime) -> ConversationContextFragment | None:
        entries = []
        for item in self._get_entries(subject_key):
            category, text = item[0], item[1]
            saved_at = item[2] if len(item) > 2 else None
            if category not in _ALLOWED or not isinstance(text, str) or not text.strip():
                continue
            if isinstance(saved_at, datetime) and now - saved_at > MAX_AGE[category]:
                continue
            entry = {"category": category, "content": text}
            if isinstance(saved_at, datetime):
                entry["saved"] = _saved(saved_at, now)
            entries.append(entry)
            if len(entries) >= MAX_RENDERED_ENTRIES:
                break
        if not entries:
            return None
        return ConversationContextFragment(namespace=self.namespace, data={"entries": entries})


def _saved(saved_at: datetime, now: datetime) -> str:
    days = (now.date() - saved_at.date()).days
    when = "today" if days <= 0 else "yesterday" if days == 1 else f"{days} days ago"
    return f"saved {saved_at.strftime('%a %d %b')}, {when}"
