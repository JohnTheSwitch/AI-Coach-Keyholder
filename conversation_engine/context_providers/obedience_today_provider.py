"""
conversation_engine/context_providers/obedience_today_provider.py

ObedienceTodayContextProvider (bug fix 2026-11-15). He wrote "The task is done" and the model, knowing nothing about today's obedience task,
praised a workout. This read-only fragment tells the model what today's tasks are and where each stands (planned, in Chaster, photo submitted
and the community vote running, verified, missed ...). It takes an injected callable, so conversation_engine imports nothing from obedience;
it can never change a task.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable

from conversation_engine.models import ConversationContextFragment

__all__ = ["ObedienceTodayContextProvider"]

logger = logging.getLogger("ai_coach_keyholder.conversation")


class ObedienceTodayContextProvider:
    namespace = "todays_tasks"

    def __init__(self, *, read: Callable[[str, datetime], list[dict]]) -> None:
        self._read = read

    def provide_context(self, *, subject_key: str, now: datetime) -> ConversationContextFragment | None:
        try:
            tasks = self._read(subject_key, now)
        except Exception:
            logger.exception("Today's tasks could not be read for the conversation; it goes on without them.")
            return None
        if not tasks:
            return None
        return ConversationContextFragment(namespace=self.namespace, data={"tasks": tasks})
