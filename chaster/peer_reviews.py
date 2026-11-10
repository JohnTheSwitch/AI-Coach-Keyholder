"""
chaster/peer_reviews.py

READ-ONLY community review results (v0.39 Phase I4). Since Phase I2 the `tasks` and `verification-picture` extensions of the account's
locks use community review (`all_members`); this module reads what the community decided:

    GET /extensions/tasks/{lockId}/history       TaskHistoryEntry.peerVerification
    GET /locks/{lockId}/verification-pictures    VerificationPictureHistoryEntry.peerVerification (or the deprecated `votes`)

Each review is identified by its peer verification id (`peerVerification._id`, or `peerVerificationId` on a verification picture) and has
a status from PeerVerificationStatusEnum: `ongoing`, `verified` or `rejected`. Entries without a review id (no peer verification, e.g. a
task completed without one) are skipped.

Kept: lock id, source extension, review id, status, vote counts, the submission time. NEVER copied: the task text, the verification code,
the image key, voter names or rejection reason texts. This module never writes anything, to Chaster or to the database.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Iterable

from chaster.lock_client import ChasterLockApiError, ChasterLockClient
from chaster.token_manager import (
    ChasterNotConnectedError,
    ChasterReauthorizationRequiredError,
    ChasterRefreshUnavailableError,
    ChasterTokenManager,
)

logger = logging.getLogger("ai_coach_keyholder.chaster.peer_reviews")

__all__ = ["ReviewSource", "ReviewStatus", "PeerReviewObservation", "PeerReviewReport", "PeerReviewReader",
           "parse_task_history", "parse_verification_pictures"]

_REVIEW_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# A lock without the tasks extension answers 404 to its history, and a lock the account only wears without picture verification answers 403
# (seen live 2026-11-09). Neither is a broken poll: there is simply nothing there to review, and an alert would cry wolf every 10 minutes.
_NOT_ON_THIS_LOCK = frozenset({"api_error:404", "api_error:403"})


class ReviewSource(StrEnum):
    TASKS = "tasks"
    VERIFICATION_PICTURE = "verification-picture"


class ReviewStatus(StrEnum):
    ONGOING = "ongoing"
    VERIFIED = "verified"
    REJECTED = "rejected"


@dataclass(frozen=True, kw_only=True)
class PeerReviewObservation:
    lock_id: str
    source: ReviewSource
    review_id: str                      # the peer verification id: the de-duplication key
    status: ReviewStatus
    verified_votes: int | None = None
    rejected_votes: int | None = None
    submitted_at: str | None = None     # as Chaster reports it (createdAt / submittedAt), only for display


@dataclass
class PeerReviewReport:
    observations: list[PeerReviewObservation] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)       # "<lock id> <source>: <code>"
    unavailable: list[str] = field(default_factory=list)    # "<lock id> <source>": the lock has nothing of that kind to read (404 / 403), not a failure


def _count(value) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None


def _observation(lock_id: str, source: ReviewSource, review: dict, *, fallback_id=None, submitted=None) -> PeerReviewObservation | None:
    review_id = review.get("_id") or fallback_id
    try:
        status = ReviewStatus(review.get("status"))
    except ValueError:
        return None
    if not isinstance(review_id, str) or not _REVIEW_ID.match(review_id):
        return None
    verified = review.get("nbVerifiedVotes", review.get("verifiedVotes"))
    rejected = review.get("nbRejectedVotes", review.get("rejectedVotes"))
    return PeerReviewObservation(lock_id=lock_id, source=source, review_id=review_id, status=status, verified_votes=_count(verified),
                                 rejected_votes=_count(rejected), submitted_at=submitted if isinstance(submitted, str) else None)


def parse_task_history(raw: Iterable, lock_id: str) -> list[PeerReviewObservation]:
    found = []
    for entry in raw or ():
        review = entry.get("peerVerification") if isinstance(entry, dict) else None
        if isinstance(review, dict):
            obs = _observation(lock_id, ReviewSource.TASKS, review, submitted=entry.get("createdAt"))
            if obs is not None:
                found.append(obs)
    return found


def parse_verification_pictures(raw: Iterable, lock_id: str) -> list[PeerReviewObservation]:
    found = []
    for entry in raw or ():
        if not isinstance(entry, dict):
            continue
        review = entry.get("peerVerification") if isinstance(entry.get("peerVerification"), dict) else entry.get("votes")
        if isinstance(review, dict):
            obs = _observation(lock_id, ReviewSource.VERIFICATION_PICTURE, review, fallback_id=entry.get("peerVerificationId"),
                               submitted=entry.get("submittedAt"))
            if obs is not None:
                found.append(obs)
    return found


_SOURCES = (
    (ReviewSource.TASKS, "get_task_history", parse_task_history),
    (ReviewSource.VERIFICATION_PICTURE, "get_verification_pictures", parse_verification_pictures),
)


class PeerReviewReader:
    """Two GETs per lock through the account's connection (401 refresh and retry by the token manager). A failing source is reported as a
    problem and simply yields no observations; it never stops the other source or lock."""

    def __init__(self, *, token_manager: ChasterTokenManager, lock_client: ChasterLockClient) -> None:
        self._tokens = token_manager
        self._client = lock_client

    def read(self, *, user_id: str, lock_ids: Iterable[str], now: datetime) -> PeerReviewReport:
        report = PeerReviewReport()
        for lock_id in dict.fromkeys(lock_ids):
            for source, method, parse in _SOURCES:
                problem = self._read_one(user_id, lock_id, now, method, parse, report)
                if problem in _NOT_ON_THIS_LOCK:
                    report.unavailable.append(f"{lock_id} {source.value}")
                elif problem:
                    report.problems.append(f"{lock_id} {source.value}: {problem}")
        return report

    def _read_one(self, user_id, lock_id, now, method, parse, report) -> str | None:
        fetch = getattr(self._client, method)
        try:
            raw = self._tokens.call_with_token(user_id, now=now, operation=lambda token: fetch(access_token=token, lock_id=lock_id))
        except ValueError:
            return "invalid_lock_id"
        except ChasterNotConnectedError:
            return "not_connected"
        except (ChasterReauthorizationRequiredError, ChasterRefreshUnavailableError):
            return "reauthorization_required"
        except ChasterLockApiError as exc:
            return "api_error" + (f":{exc.status_code}" if exc.status_code else "")
        except Exception:
            logger.exception("Unexpected failure while reading community reviews of lock %s.", lock_id)
            return "failed"
        report.observations.extend(parse(raw, lock_id))
        return None
