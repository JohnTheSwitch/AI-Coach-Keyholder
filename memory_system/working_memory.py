"""
memory_system/working_memory.py

InMemoryWorkingMemory -- process-lifetime, per-subject working memory.
Precise lifetime contract (not a weaker or stronger claim than this):

- Survives multiple messages from the same subject within one process run.
- NOT separated by Discord channel, guild, or conversation session --
  `subject_key` is the only dimension.
- A process restart wipes it completely.
- It is neither long-term memory nor session-identified memory -- no
  session ID or session lifecycle exists here, and none is added
  without a demonstrated need.

Privacy contract: non-persistent does not mean non-sensitive. This
class stores only the raw text of an already-validated user message
and its already-validated assistant response, plus which role each
turn belongs to -- nothing else. No commands, no onboarding messages,
no fallback responses, no system prompt/instructions, no identity
profile, no consent/audit/Discord-message/provider ID, no exception
text, no internal metadata. Raw content is never logged -- this module
does not log at all.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from threading import Lock
from typing import Callable, Protocol

from memory_system.models import (
    WorkingMemoryCapacityError,
    WorkingMemoryRole,
    WorkingMemorySnapshot,
    WorkingMemoryTurn,
)

__all__ = ["WorkingMemoryReader", "WorkingMemoryWriter", "InMemoryWorkingMemory"]


def _require_positive_integer(value: object, *, name: str) -> int:
    """`type(value) is not int`, not `isinstance()` -- `isinstance(True, int)`
    is `True` (bool is a subtype of int in Python), which would let
    `True` silently pass as a valid limit. `type(value) is int` is
    `False` for a bool, correctly rejecting it."""
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer.")
    return value


def _require_non_empty_string(value: object, *, name: str) -> str:
    """Used for subject_key/user_content/assistant_content alike --
    one contract, not three ad-hoc checks. Never normalizes the
    returned value; `.strip()` is used only to detect emptiness."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string.")
    return value


class WorkingMemoryReader(Protocol):
    def read(self, *, subject_key: str) -> WorkingMemorySnapshot: ...


class WorkingMemoryWriter(Protocol):
    def commit_exchange(self, *, subject_key: str, user_content: str, assistant_content: str, at: datetime | None = None) -> None: ...


class InMemoryWorkingMemory(WorkingMemoryReader, WorkingMemoryWriter):
    """
    A short internal `Lock` protects only: reading the internal map,
    the atomic append of a whole exchange, and trimming -- never the
    duration of anything else (this class has no notion of "generation"
    at all; it is a pure data structure). No FIFO guarantee of its own
    -- concurrent commits for the SAME subject are mutually exclusive
    (never corrupt the structure), but their relative order is not
    guaranteed without an external queue; only the order in which they
    actually acquired the internal lock is what happens, and that is
    what this class documents, not a stronger ordering promise.
    """

    def __init__(self, *, max_exchanges_per_subject: int, max_characters_per_subject: int,
                 clock: Callable[[], datetime] | None = None, max_age: timedelta | None = None) -> None:
        self._max_exchanges = _require_positive_integer(max_exchanges_per_subject, name="max_exchanges_per_subject")
        self._max_characters = _require_positive_integer(max_characters_per_subject, name="max_characters_per_subject")
        # Owner, 2026-11-12: something said days ago ("I got burned this Wednesday") must not live on as the current conversation.
        # With a clock and a max_age, older exchanges are left out of what the model reads.
        self._clock = clock
        self._max_age = max_age
        self._lock = Lock()
        self._exchanges: dict[str, list[tuple[WorkingMemoryTurn, WorkingMemoryTurn]]] = {}
        self._times: dict[int, datetime] = {}

    def read(self, *, subject_key: str) -> WorkingMemorySnapshot:
        subject_key = _require_non_empty_string(subject_key, name="subject_key")
        with self._lock:
            pairs = list(self._exchanges.get(subject_key, ()))
            if self._clock is not None and self._max_age is not None:
                cutoff = self._clock() - self._max_age
                pairs = [p for p in pairs if self._times.get(id(p), cutoff) >= cutoff]
        turns: list[WorkingMemoryTurn] = []
        for user_turn, assistant_turn in pairs:
            turns.append(user_turn)
            turns.append(assistant_turn)
        return WorkingMemorySnapshot(turns=tuple(turns))  # a fresh tuple every call

    def commit_exchange(self, *, subject_key: str, user_content: str, assistant_content: str, at: datetime | None = None) -> None:
        subject_key = _require_non_empty_string(subject_key, name="subject_key")
        user_content = _require_non_empty_string(user_content, name="user_content")
        assistant_content = _require_non_empty_string(assistant_content, name="assistant_content")

        exchange_size = len(user_content) + len(assistant_content)
        if exchange_size > self._max_characters:
            raise WorkingMemoryCapacityError(
                "A single exchange exceeds max_characters_per_subject -- not stored."
            )

        pair = (
            WorkingMemoryTurn(role=WorkingMemoryRole.USER, content=user_content),
            WorkingMemoryTurn(role=WorkingMemoryRole.ASSISTANT, content=assistant_content),
        )
        with self._lock:
            existing = self._exchanges.setdefault(subject_key, [])
            existing.append(pair)  # atomic: the whole pair, or nothing (capacity was checked above, before any mutation)
            when = at if at is not None else (self._clock() if self._clock is not None else None)
            if when is not None:
                self._times[id(pair)] = when
            self._trim(existing)
            live = {id(p) for pairs in self._exchanges.values() for p in pairs}
            for key in [k for k in self._times if k not in live]:
                del self._times[key]

    def _trim(self, pairs: list[tuple[WorkingMemoryTurn, WorkingMemoryTurn]]) -> None:
        """Oldest WHOLE exchange first, count then character budget --
        never a lone turn."""
        while len(pairs) > self._max_exchanges:
            pairs.pop(0)
        while pairs and self._total_characters(pairs) > self._max_characters:
            pairs.pop(0)

    @staticmethod
    def _total_characters(pairs: list[tuple[WorkingMemoryTurn, WorkingMemoryTurn]]) -> int:
        return sum(len(u.content) + len(a.content) for u, a in pairs)
