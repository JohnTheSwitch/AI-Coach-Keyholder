"""
chaster_execution/models.py

Types for the Chaster execution plane (Phase 3). Canonical:
docs/architecture/chaster_execution_phase3_proposal.md.

The transport interface has exactly THREE methods: add_time, remove_time and (v0.39)
set_frozen for barrier management. There is, and will never be, a method for the
combination, recovery or unlock endpoints.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

__all__ = [
    "ExecStatus", "TransportOutcome", "TransportResult", "LockActionTransport",
    "EXECUTABLE_ACTIONS", "BARRIER_ACTIONS", "BARRIER_RULE_IDS", "DECISION_MAX_AGE_MINUTES",
]


class ExecStatus(StrEnum):
    DRY_RUN = "dry_run"
    PENDING = "pending"
    EXECUTING = "executing"
    EXECUTED = "executed"
    FAILED = "failed"
    BLOCKED = "blocked"          # a pre-flight check refused; nothing was sent
    EXPIRED = "expired"          # older than the staleness window; nothing was sent
    NEEDS_REVIEW = "needs_review"  # outcome unknown; NEVER retried automatically


# Only time adjustments may execute in Phase 3 (freeze/unfreeze stay disabled).
EXECUTABLE_ACTIONS = frozenset({"add_time", "remove_time"})

# v0.39: freeze/unfreeze may execute ONLY for barrier management, i.e. for decisions created by the
# release engine under one of these rule ids. A freeze/unfreeze from any other rule (for example a
# custom Rules Engine ruleset) never executes.
BARRIER_ACTIONS = frozenset({"freeze", "unfreeze"})
BARRIER_RULE_IDS = frozenset({"barrier.release", "barrier.freeze"})

DECISION_MAX_AGE_MINUTES = 15


class TransportOutcome(StrEnum):
    OK = "ok"                          # Chaster accepted the change (HTTP 204)
    NOT_SENT = "not_sent"              # provably failed before the request left (e.g. no connection)
    UNAUTHORIZED = "unauthorized"      # 401
    FORBIDDEN = "forbidden"            # 403: missing lock.time.add / lock.time.remove permission
    NOT_ELIGIBLE = "not_eligible"      # 400: e.g. the lock is not locked
    NOT_FOUND = "not_found"            # 404
    UNKNOWN = "unknown"                # timeout / 5xx / anything after sending: outcome ambiguous


@dataclass(frozen=True, kw_only=True)
class TransportResult:
    outcome: TransportOutcome
    http_status: int | None = None


class LockActionTransport(Protocol):
    """Authenticated as the KEYHOLDER connection (Route A). Implementations
    must never log or return tokens, headers or bodies."""

    def add_time(self, *, lock_id: str, seconds: int) -> TransportResult: ...

    def remove_time(self, *, lock_id: str, seconds: int) -> TransportResult: ...

    def set_frozen(self, *, lock_id: str, frozen: bool) -> TransportResult: ...
