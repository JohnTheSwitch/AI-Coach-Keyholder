"""
conversation_engine/context_providers/detector_questions_provider.py

DetectorQuestionsContextProvider (owner, 2026-11-12). Live: asked "What about my incident?" ten minutes after a detector finding had been counted, the model
answered "No report from you yet", because nothing told it. This hands it the user's open and recent detector questions (infractions/signals.py) as
authoritative state: what was found, where it stands, and until when the user can still answer or appeal. Read-only, through an injected callable, so
conversation_engine imports no database code. Only the fields below pass; there is no Trust here and no field that could carry it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from conversation_engine.models import ConversationContextFragment

__all__ = ["DetectorQuestionsContextProvider"]

_FIELDS = ("detail", "stage", "until", "closed")
_STAGES = frozenset({"waiting_for_answer", "being_judged", "appeal_open", "counted", "dropped", "awaiting_confirmation"})


class DetectorQuestionsContextProvider:
    namespace = "detector_questions"

    def __init__(self, *, read: Callable[[str, datetime], list[dict]]) -> None:
        self._read = read

    def provide_context(self, *, subject_key: str, now: datetime) -> ConversationContextFragment | None:
        items = [{key: item.get(key) for key in _FIELDS} for item in (self._read(subject_key, now) or []) if item.get("stage") in _STAGES][:3]
        if not items:
            return None
        return ConversationContextFragment(namespace=self.namespace, data={"questions": items})
