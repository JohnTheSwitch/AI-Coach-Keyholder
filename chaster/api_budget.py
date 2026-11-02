"""
chaster/api_budget.py

A shared request budget for the ONE keyholder (bot) Chaster account (v0.39 Phase E5). Every tenant uses that account for lock searches, observation
refreshes and chat polling, so N tenants could otherwise multiply the request rate. The budget is a sliding one-minute window shared by everything in
this process that calls Chaster with the keyholder token.

Two lanes:
- BACKGROUND (observation refreshes, keyholder searches, chat polling and chat sends): allowed only while the window has room. When it is full the call
  is refused with `ApiBudgetExceeded` (a `ChasterLockApiError`), which every caller already treats as "Chaster unavailable": a read goes stale and is
  retried later, a send reports "not delivered". Nothing is guessed.
- ESSENTIAL (an authorised lock action of the execution plane): never refused by the budget, because a release or a freeze that was already decided must
  not be dropped by background traffic; it is still COUNTED, so background work backs off after it.

The budget lives in memory in the bot process; the local CLI is a separate process and is not counted.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from enum import StrEnum
from typing import Callable, TypeVar

from chaster.lock_client import ChasterLockApiError

__all__ = ["ApiBudget", "Lane", "ApiBudgetExceeded", "BudgetedTokenManager", "DEFAULT_REQUESTS_PER_MINUTE"]

DEFAULT_REQUESTS_PER_MINUTE = 30
T = TypeVar("T")


class Lane(StrEnum):
    BACKGROUND = "background"
    ESSENTIAL = "essential"


class ApiBudgetExceeded(ChasterLockApiError):
    def __init__(self) -> None:
        super().__init__("The shared Chaster request budget is used up for this minute; try again later.")


class ApiBudget:
    def __init__(self, *, requests_per_minute: int = DEFAULT_REQUESTS_PER_MINUTE, window_seconds: float = 60.0,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        if isinstance(requests_per_minute, bool) or not isinstance(requests_per_minute, int) or requests_per_minute < 1:
            raise ValueError("requests_per_minute must be a positive int")
        self._limit = requests_per_minute
        self._window = float(window_seconds)
        self._now = monotonic
        self._stamps: deque[float] = deque()
        self._lock = threading.Lock()
        self.refused = 0

    def try_acquire(self, lane: Lane = Lane.BACKGROUND) -> bool:
        with self._lock:
            now = self._now()
            while self._stamps and now - self._stamps[0] >= self._window:
                self._stamps.popleft()
            if lane is Lane.BACKGROUND and len(self._stamps) >= self._limit:
                self.refused += 1
                return False
            self._stamps.append(now)
            return True

    @property
    def used(self) -> int:
        with self._lock:
            now = self._now()
            return sum(1 for s in self._stamps if now - s < self._window)

    @property
    def limit(self) -> int:
        return self._limit


class BudgetedTokenManager:
    """Wraps a ChasterTokenManager: each `call_with_token` for the budgeted account takes one request from the shared budget first (a refresh-and-retry
    inside the manager is not charged twice). Other accounts (a wearer's own connection) pass through untouched."""

    def __init__(self, inner, budget: ApiBudget, *, account_user_id: str | None, lane: Lane) -> None:
        self._inner = inner
        self._budget = budget
        self._account = account_user_id
        self._lane = lane

    def call_with_token(self, user_id: str, *, now, operation: Callable[[str], T]) -> T:
        if self._account is not None and user_id == self._account and not self._budget.try_acquire(self._lane):
            raise ApiBudgetExceeded()
        return self._inner.call_with_token(user_id, now=now, operation=operation)

    def get_valid_access_token(self, user_id: str, *, now):
        return self._inner.get_valid_access_token(user_id, now=now)

    def __getattr__(self, name):
        return getattr(self._inner, name)
