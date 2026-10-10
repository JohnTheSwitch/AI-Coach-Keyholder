"""
conversation_engine/context_providers/release_phase_provider.py

ReleasePhaseContextProvider (v0.39). The ONLY release information that may ever reach the
model: one coarse enum. It takes an injected callable (supplied by the composition root), so
conversation_engine imports nothing from release_engine and can never see a plan, a target
time, a token balance or an offset. Anything outside the two allowed values yields no fragment.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from conversation_engine.models import ConversationContextFragment

__all__ = ["ReleasePhaseContextProvider", "ALLOWED_RELEASE_PHASES"]

ALLOWED_RELEASE_PHASES = frozenset({"requirements_met_pending", "requirements_not_met"})


class ReleasePhaseContextProvider:
    namespace = "release"

    def __init__(self, *, get_phase: Callable[[str], str | None]) -> None:
        self._get_phase = get_phase

    def provide_context(self, *, subject_key: str, now: datetime) -> ConversationContextFragment | None:
        phase = self._get_phase(subject_key)
        if phase not in ALLOWED_RELEASE_PHASES:
            return None
        return ConversationContextFragment(namespace=self.namespace, data={"release_phase": phase})
