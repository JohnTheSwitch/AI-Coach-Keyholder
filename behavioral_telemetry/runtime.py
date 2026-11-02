"""
behavioral_telemetry/runtime.py

RuntimeTelemetryBuffer (v0.39 Phase G2, Tier C of docs/architecture/phase_g_proposal.md section 4). The ONLY telemetry code that runs in the live reply path.

`record` is handed to the conversation engine as its `runtime_sink`. It does nothing but a counter step, a dict read/write and `deque.append` on a bounded
`collections.deque(maxlen=N)`: no I/O, no SQLite, no lock, no await, and it never raises. Each of those operations is atomic in CPython, so the engine's
worker threads can call it concurrently while the periodic cycle drains it (`take`). When the deque is full the oldest sample is dropped; a crash loses what has
not been drained yet (at most N samples), which is accepted for research data.

Facts recorded here exist nowhere else: the model's generation latency and structured-output status (`llm_generation`) and the time the wearer took to write
again after the bot's previous reply (`conversation_gap`). `forget` marks a user so that samples taken before `forget all` are dropped at drain time.
"""

from __future__ import annotations

import itertools
from collections import deque
from datetime import datetime, timedelta

__all__ = ["RuntimeTelemetryBuffer", "DEFAULT_MAXLEN", "MAX_TRACKED_SUBJECTS"]

DEFAULT_MAXLEN = 2000
MAX_TRACKED_SUBJECTS = 1000


class RuntimeTelemetryBuffer:
    def __init__(self, *, maxlen: int = DEFAULT_MAXLEN) -> None:
        self._items: deque = deque(maxlen=maxlen)
        self._seq = itertools.count(1)
        self._last_reply_at: dict[str, datetime] = {}
        self._forgotten: dict[str, int] = {}

    def record(self, subject_key: str, kind: str, metrics: dict, at: datetime) -> None:
        """The engine's runtime sink. O(1), in memory only, never raises."""
        try:
            seq = next(self._seq)
            if kind == "llm_generation":
                previous = self._last_reply_at.get(subject_key)
                if previous is not None and at > previous:
                    self._items.append((seq, subject_key, "conversation_gap", {"seconds_since_last_reply": int((at - previous).total_seconds())}, at))
                if len(self._last_reply_at) >= MAX_TRACKED_SUBJECTS and subject_key not in self._last_reply_at:
                    self._last_reply_at.clear()
                self._last_reply_at[subject_key] = at + timedelta(milliseconds=int(metrics.get("latency_ms") or 0))
            self._items.append((seq, subject_key, kind, dict(metrics), at))
        except Exception:
            pass

    def take(self, limit: int | None = None) -> list[tuple]:
        """Removes and returns up to `limit` samples, oldest first (the periodic drain)."""
        out = []
        while limit is None or len(out) < limit:
            try:
                out.append(self._items.popleft())
            except IndexError:
                break
        return out

    def forget(self, subject_key: str) -> None:
        """Called by `forget all` BEFORE the stored rows are deleted: every sample of this user taken until now is dropped at drain time."""
        self._forgotten[subject_key] = next(self._seq)
        self._last_reply_at.pop(subject_key, None)

    def is_forgotten(self, subject_key: str, seq: int) -> bool:
        mark = self._forgotten.get(subject_key)
        return mark is not None and seq < mark

    def __len__(self) -> int:
        return len(self._items)
