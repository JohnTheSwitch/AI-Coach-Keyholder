"""
conversation_engine/context_providers/coach_provider.py

CoachContextProvider (v0.39 Phase J3): the user's goals, commitments and recent tasks for the prompt. It takes an injected callable (coach.desk.CoachDesk.context),
so conversation_engine imports no database code, and only the fields below pass: a new field in the source cannot reach the model by accident. A failing read
is an ordinary provider failure: the turn goes on without this fragment.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable, Mapping

from conversation_engine.models import ConversationContextFragment

__all__ = ["CoachContextProvider"]

_FIELDS = ("goals", "commitments", "tasks", "open_proposal")


class CoachContextProvider:
    namespace = "coach"

    def __init__(self, *, read: Callable[[str, datetime], Mapping | None]) -> None:
        self._read = read

    def provide_context(self, *, subject_key: str, now: datetime) -> ConversationContextFragment | None:
        data = self._read(subject_key, now)
        if data is None:
            return None
        return ConversationContextFragment(namespace=self.namespace, data={key: data[key] for key in _FIELDS})
