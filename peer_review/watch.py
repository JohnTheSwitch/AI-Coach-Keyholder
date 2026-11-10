"""
peer_review/watch.py

PeerReviewWatch (v0.39 Phase I4): reads the community review results of the account's locked locks (chaster/peer_reviews.py, read-only),
records every status change in the ledger (migration 043) and turns a REJECTED review into exactly ONE Incident that stays UNCONFIRMED until
the keyholder decides (philosophy 2.10, 2.12: never act on a single signal).

No double jeopardy: this module never deducts tokens, never adds time, never freezes and never confirms anything. An UNCONFIRMED Incident has
no consumer: the Penalty Engine reacts only to `incident.confirmation_changed`, and every reader of Incidents (Penalty Engine, daily
evaluation, plugin SDK) counts CONFIRMED ones only. Chaster's own punishments were cleared in Phase I2.

One poll, per lock:
  1. The first poll of a lock records its existing reviews as a BASELINE: a review that had already ended (verified or rejected) before the
     watch started never creates an Incident; one still `ongoing` is followed like any new review.
  2. Every new review and every status change is written in ONE transaction: the latest state (`peer_reviews`), an append-only transition
     (`peer_review_transitions`) and, when a review first becomes `rejected`, an Incident id claimed on that row. A review can therefore
     claim at most one Incident, however often it is polled, by however many processes.
  3. Claimed Incidents are registered with the chosen id (TrustManager.register_incident_report(incident_id=...)): a retry after a crash
     fails on the primary key instead of creating a duplicate, and the row is then marked registered.
  4. `pending_notifications()` lists registered Incidents the owner has not been told about yet; the bot sends a Discord DM and calls
     `mark_notified()`. A failed DM is retried on the next poll.

The poll is not stopped by the execution runtime halt: it only reads Chaster and writes local, unconfirmed records.

`PeerReviewDecisions` is the owner's manual decision (CLI only): `confirm` advances the Incident to CONFIRMED with source KEYHOLDER_REVIEW
(from then on it counts like any confirmed Incident), `dismiss` closes it in the ledger and leaves it UNCONFIRMED for good.
"""

from __future__ import annotations

import hmac
import logging
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Callable, Iterable

from chaster.peer_reviews import PeerReviewObservation, PeerReviewReport, ReviewSource, ReviewStatus
from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.time_format import iso as _iso

logger = logging.getLogger("ai_coach_keyholder.peer_review.watch")

__all__ = ["RULE_GROUP", "PeerReviewWatch", "PollResult", "PendingNotice", "PeerReviewDecisions", "DecisionOutcome", "ReviewRow",
           "PollHealth", "FAILURE_THRESHOLD", "incident_description", "notice_text"]

RULE_GROUP = "peer_review_rejection"
FAILURE_THRESHOLD = 3                   # consecutive failed polls before the keyholder is alerted
_ENDED = (ReviewStatus.VERIFIED.value, ReviewStatus.REJECTED.value)
_SOURCE_TEXT = {ReviewSource.TASKS.value: "task photo", ReviewSource.VERIFICATION_PICTURE.value: "verification picture"}


def _votes(verified, rejected) -> str:
    return f"{'?' if verified is None else verified} verified / {'?' if rejected is None else rejected} rejected votes"


def incident_description(*, lock_id: str, source: str, review_id: str, verified, rejected) -> str:
    return (f"Community review REJECTED a {_SOURCE_TEXT.get(source, source)} on lock {lock_id} (review {review_id}, "
            f"{_votes(verified, rejected)}). Unconfirmed: awaiting the keyholder's review; nothing was deducted or added.")


def notice_text(*, lock_id: str, source: str, incident_id: str, verified, rejected) -> str:
    return (f"Community review rejected a {_SOURCE_TEXT.get(source, source)} on lock {lock_id} ({_votes(verified, rejected)}).\n"
            f"An UNCONFIRMED incident was recorded for your review (id {incident_id}). Nothing was deducted and no time was added.\n"
            f"Decide with the buttons below, or on the bot's machine: "
            f"python -m cli peer-reviews confirm|dismiss {incident_id} --owner-id <your id> --yes")


@dataclass
class PollResult:
    locks: int = 0
    observed: int = 0
    transitions: int = 0
    incidents_claimed: int = 0
    incidents_registered: int = 0
    problems: list[str] = field(default_factory=list)


@dataclass(frozen=True, kw_only=True)
class PendingNotice:
    lock_id: str
    review_id: str
    incident_id: str
    text: str
    source: str = ""
    verified_votes: int | None = None
    rejected_votes: int | None = None


@dataclass(frozen=True, kw_only=True)
class ReviewRow:
    lock_id: str
    review_id: str
    source: str
    status: str
    verified_votes: int | None
    rejected_votes: int | None
    first_seen_at: str
    updated_at: str
    incident_id: str | None
    incident_registered: bool
    notified: bool
    owner_decision: str | None


def _row(r) -> ReviewRow:
    return ReviewRow(lock_id=r["lock_id"], review_id=r["review_id"], source=r["source"], status=r["status"],
                     verified_votes=r["verified_votes"], rejected_votes=r["rejected_votes"], first_seen_at=r["first_seen_at"],
                     updated_at=r["updated_at"], incident_id=r["incident_id"], incident_registered=r["incident_registered_at"] is not None,
                     notified=r["notified_at"] is not None, owner_decision=r["owner_decision"])


class PeerReviewWatch:
    def __init__(self, db_path: str | Path, *, core: CoreDatabase | None = None, reader, account_user_id: str,
                 list_lock_ids: Callable[[datetime], tuple[list[str], list[str]]],
                 register_incident: Callable[[str, str, datetime], None]) -> None:
        """`list_lock_ids(now) -> (locked lock ids, problems)`; `register_incident(incident_id, description, now)` registers an
        UNCONFIRMED Incident with exactly that id (it must raise on a duplicate id)."""
        self._core = core if core is not None else CoreDatabase(Path(db_path))
        self._reader = reader
        self._account = account_user_id
        self._list_lock_ids = list_lock_ids
        self._register = register_incident
        self._unavailable: set[str] = set()

    # ------------------------------------------------------------------ the poll

    def poll(self, now: datetime) -> PollResult:
        result = PollResult()
        lock_ids, problems = self._list_lock_ids(now)
        result.problems += list(problems)
        result.locks = len(lock_ids)
        if lock_ids:
            report: PeerReviewReport = self._reader.read(user_id=self._account, lock_ids=lock_ids, now=now)
            result.problems += report.problems
            unavailable = set(getattr(report, "unavailable", ()))
            if unavailable - self._unavailable:
                logger.info("Community review watch: nothing to read on %s (not on that lock).", "; ".join(sorted(unavailable - self._unavailable)))
            self._unavailable = unavailable
            failed = {p.split(" ", 1)[0] for p in report.problems}
            for lock_id in lock_ids:
                found = [o for o in report.observations if o.lock_id == lock_id]
                result.observed += len(found)
                transitions, claimed = self._record(lock_id, found, now, complete=lock_id not in failed)
                result.transitions += transitions
                result.incidents_claimed += claimed
        result.incidents_registered = self._register_claimed(now)
        if result.transitions or result.incidents_registered or result.problems:
            logger.info("Peer review poll: %d lock(s), %d review(s), %d transition(s), %d incident(s) registered%s.", result.locks,
                        result.observed, result.transitions, result.incidents_registered,
                        f", problems: {'; '.join(result.problems)}" if result.problems else "")
        return result

    def _record(self, lock_id: str, observations: Iterable[PeerReviewObservation], now: datetime, *, complete: bool) -> tuple[int, int]:
        """`complete` is False when one of the lock's two reads failed: the baseline is then postponed (a review missing from a partial
        read could otherwise look new later and turn an old rejection into an Incident)."""
        stamp = _iso(now)

        def write(tx: Transaction, _s: object) -> tuple[int, int]:
            baselining = tx.fetch_one("SELECT 1 FROM peer_review_locks WHERE lock_id = ?", (lock_id,)) is None
            if baselining and not complete:
                return 0, 0
            transitions = claimed = 0
            for obs in {o.review_id: o for o in observations}.values():       # one entry per review
                row = tx.fetch_one("SELECT * FROM peer_reviews WHERE lock_id = ? AND review_id = ?", (lock_id, obs.review_id))
                if row is None:
                    baseline = baselining and obs.status.value in _ENDED
                    tx.execute(
                        "INSERT INTO peer_reviews (lock_id, review_id, source, status, verified_votes, rejected_votes, submitted_at, baseline, "
                        "first_seen_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (lock_id, obs.review_id, obs.source.value, obs.status.value, obs.verified_votes, obs.rejected_votes,
                         obs.submitted_at, 1 if baseline else 0, stamp, stamp))
                    previous, is_baseline, incident = None, baseline, None
                elif row["status"] != obs.status.value or (row["verified_votes"], row["rejected_votes"]) != (obs.verified_votes, obs.rejected_votes):
                    tx.execute("UPDATE peer_reviews SET status = ?, verified_votes = ?, rejected_votes = ?, updated_at = ? "
                               "WHERE lock_id = ? AND review_id = ?",
                               (obs.status.value, obs.verified_votes, obs.rejected_votes, stamp, lock_id, obs.review_id))
                    if row["status"] == obs.status.value:
                        continue                                                # only the vote counts moved: not a transition
                    previous, is_baseline, incident = row["status"], bool(row["baseline"]), row["incident_id"]
                else:
                    continue
                tx.execute(
                    "INSERT INTO peer_review_transitions (id, created_at, lock_id, review_id, source, previous_status, new_status, "
                    "verified_votes, rejected_votes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (str(uuid.uuid4()), stamp, lock_id, obs.review_id, obs.source.value, previous, obs.status.value,
                     obs.verified_votes, obs.rejected_votes))
                transitions += 1
                if obs.status is ReviewStatus.REJECTED and incident is None and not is_baseline:
                    tx.execute("UPDATE peer_reviews SET incident_id = ? WHERE lock_id = ? AND review_id = ? AND incident_id IS NULL",
                               (str(uuid.uuid4()), lock_id, obs.review_id))
                    claimed += 1
            if baselining:
                tx.execute("INSERT OR IGNORE INTO peer_review_locks (lock_id, baselined_at) VALUES (?, ?)", (lock_id, stamp))
            return transitions, claimed

        return apply_transition(self._core, write=write)

    def _register_claimed(self, now: datetime) -> int:
        with self._core.transaction() as tx:
            pending = [dict(r) for r in tx.fetch_all(
                "SELECT * FROM peer_reviews WHERE incident_id IS NOT NULL AND incident_registered_at IS NULL ORDER BY first_seen_at")]
        registered = 0
        for r in pending:
            try:
                self._register(r["incident_id"], incident_description(lock_id=r["lock_id"], source=r["source"], review_id=r["review_id"],
                                                                      verified=r["verified_votes"], rejected=r["rejected_votes"]), now)
            except sqlite3.IntegrityError:
                if not self._incident_exists(r["incident_id"]):
                    logger.exception("Registering the incident for review %s failed; will retry.", r["review_id"])
                    continue                                                    # a different constraint: retry next poll
            except Exception:
                logger.exception("Registering the incident for review %s failed; will retry.", r["review_id"])
                continue

            def mark(tx: Transaction, _s: object, incident_id=r["incident_id"]) -> None:
                tx.execute("UPDATE peer_reviews SET incident_registered_at = ? WHERE incident_id = ? AND incident_registered_at IS NULL",
                           (_iso(now), incident_id))

            apply_transition(self._core, write=mark)
            registered += 1
            logger.info("Community review %s on lock %s was rejected: UNCONFIRMED incident %s recorded for the keyholder's review.",
                        r["review_id"], r["lock_id"], r["incident_id"])
        return registered

    def _incident_exists(self, incident_id: str) -> bool:
        with self._core.transaction() as tx:
            return tx.fetch_one("SELECT 1 FROM incidents WHERE id = ?", (incident_id,)) is not None

    # ------------------------------------------------------------------ owner notification

    def pending_notifications(self) -> list[PendingNotice]:
        with self._core.transaction() as tx:
            rows = tx.fetch_all("SELECT * FROM peer_reviews WHERE incident_registered_at IS NOT NULL AND notified_at IS NULL "
                                "AND owner_decision IS NULL ORDER BY first_seen_at")
        return [PendingNotice(lock_id=r["lock_id"], review_id=r["review_id"], incident_id=r["incident_id"],
                              text=notice_text(lock_id=r["lock_id"], source=r["source"], incident_id=r["incident_id"],
                                               verified=r["verified_votes"], rejected=r["rejected_votes"]),
                              source=r["source"], verified_votes=r["verified_votes"], rejected_votes=r["rejected_votes"]) for r in rows]

    def mark_notified(self, incident_id: str, now: datetime) -> None:
        def write(tx: Transaction, _s: object) -> None:
            tx.execute("UPDATE peer_reviews SET notified_at = ? WHERE incident_id = ? AND notified_at IS NULL", (_iso(now), incident_id))

        apply_transition(self._core, write=write)


# ---------------------------------------------------------------------- poll health (alerting)

class PollHealth:
    """Counts CONSECUTIVE failed polls. A poll fails when it raised or reported any problem (network error, Chaster 4xx/5xx, rate limit,
    expired or missing connection). At the threshold ONE alert is queued for the keyholder; the first clean poll after an alert queues
    one recovery note. Undelivered messages stay queued (`pending`) and are retried with the next poll; if the watch recovers before the
    alert was delivered, both are dropped. In memory only: a restart starts a fresh count."""

    def __init__(self, threshold: int = FAILURE_THRESHOLD) -> None:
        self.threshold = threshold
        self.failures = 0
        self.alerted = False
        self.pending: str | None = None
        self._pending_is_alert = False

    def observe(self, *, failed: bool, detail: str = "") -> None:
        if failed:
            self.failures += 1
            if self.failures >= self.threshold and not self.alerted:
                self.alerted, self._pending_is_alert = True, True
                self.pending = (f"Community review watch: the last {self.failures} polls failed ({detail[:300] or 'see the bot log'}). "
                                f"Rejected photos are NOT being seen until this is fixed. For 'reauthorization_required' or "
                                f"'not_connected', reconnect Chaster with `chaster connect`; other errors usually clear by themselves.")
                logger.warning("Community review watch: %d consecutive failed polls; alerting the keyholder.", self.failures)
            return
        if self.alerted:
            if self._pending_is_alert and self.pending is not None:
                self.pending = None                                   # recovered before the alert went out: say nothing
            else:
                self.pending = f"Community review watch: polling works again after {self.failures} failed attempts."
            self._pending_is_alert = False
            logger.info("Community review watch recovered after %d failed polls.", self.failures)
        self.failures, self.alerted = 0, False

    @property
    def pending_is_alert(self) -> bool:
        return self._pending_is_alert

    def delivered(self) -> None:
        self.pending, self._pending_is_alert = None, False


# ---------------------------------------------------------------------- the owner's manual decision

class DecisionOutcome(StrEnum):
    CONFIRMED = "confirmed"
    DISMISSED = "dismissed"
    OWNER_NOT_CONFIGURED = "owner_not_configured"
    NOT_OWNER = "not_owner"
    NOT_CONFIRMED = "not_confirmed"
    NOT_FOUND = "not_found"                    # no registered incident from a community review has that id
    ALREADY_DECIDED = "already_decided"
    FAILED = "failed"


class PeerReviewDecisions:
    def __init__(self, db_path: str | Path, *, core: CoreDatabase | None = None, owner_user_id: str | None,
                 confirm_incident: Callable[[str, str, datetime], None]) -> None:
        """`confirm_incident(incident_id, evidence_description, now)` advances the Incident to CONFIRMED (source KEYHOLDER_REVIEW)."""
        self._core = core if core is not None else CoreDatabase(Path(db_path))
        self._owner = owner_user_id.strip() if owner_user_id and owner_user_id.strip() else None
        self._confirm = confirm_incident

    def rows(self, *, include_all: bool = False) -> list[ReviewRow]:
        where = "" if include_all else "WHERE incident_id IS NOT NULL"
        with self._core.transaction() as tx:
            return [_row(r) for r in tx.fetch_all(f"SELECT * FROM peer_reviews {where} ORDER BY first_seen_at DESC, rowid DESC")]

    def authorize(self, actor: str) -> DecisionOutcome | None:
        """None when `actor` is the owner (constant-time compare), otherwise the refusal."""
        if self._owner is None:
            return DecisionOutcome.OWNER_NOT_CONFIGURED
        if not isinstance(actor, str) or not hmac.compare_digest(actor.encode("utf-8"), self._owner.encode("utf-8")):
            return DecisionOutcome.NOT_OWNER
        return None

    def decide(self, *, actor: str, incident_id: str, confirm: bool, confirmed: bool, now: datetime) -> DecisionOutcome:
        refusal = self.authorize(actor)
        if refusal is not None:
            return refusal
        if confirmed is not True:
            return DecisionOutcome.NOT_CONFIRMED
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT * FROM peer_reviews WHERE incident_id = ? AND incident_registered_at IS NOT NULL", (incident_id,))
        if row is None:
            return DecisionOutcome.NOT_FOUND
        if row["owner_decision"] is not None:
            return DecisionOutcome.ALREADY_DECIDED
        decision = DecisionOutcome.CONFIRMED if confirm else DecisionOutcome.DISMISSED
        if confirm:
            try:
                self._confirm(incident_id, f"The keyholder reviewed the community rejection of review {row['review_id']} on lock "
                                           f"{row['lock_id']} and confirmed it.", now)
            except Exception:
                logger.exception("Confirming incident %s failed.", incident_id)
                return DecisionOutcome.FAILED

        def write(tx: Transaction, _s: object) -> None:
            tx.execute("UPDATE peer_reviews SET owner_decision = ?, decided_at = ? WHERE incident_id = ? AND owner_decision IS NULL",
                       (decision.value, _iso(now), incident_id))

        apply_transition(self._core, write=write)
        logger.info("Peer review incident %s: %s by the keyholder.", incident_id, decision.value)
        return decision
