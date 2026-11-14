"""
proof_checks/service.py

Proof spot checks (owner, 2026-11-13; honesty and proof, slice 3; design: docs/architecture/honesty_and_proof_proposal.md).

A timely "Done" (coach/desk.py, report window) may be picked for a check, at random, more often the lower the Honesty level:

  select    the coach desk asks `select()` BEFORE it records the Kept; picked -> the Kept is recorded as `checking` and earns nothing yet
  start     the bot assigns a Chaster task "Show: <commitment>" (photo + Chaster's code, community review) and sends the challenge with
            "I can't show it now" and "I didn't actually do it"; PROOF_DEADLINE to submit
  poll      reads the task history: submitted in time and verified -> passed (the Kept counts, plus PROOF_VERIFIED in Honesty);
            rejected, or nothing submitted in time -> failed (the Kept earns nothing; the wearer is asked to explain: infractions path,
            Honesty domain, Trust and a Warning, never a Penalty Window)
  buttons   "I can't show it now" -> declined: earns nothing, FREE_PASSES a week, after that it counts as Not kept;
            "I didn't actually do it" -> retracted: Not kept, nothing in Honesty (owning up beats being caught)

Unknown stays unknown: a check that cannot start (halt, no lock, no tasks extension, Chaster unreachable) is cancelled and the Kept simply counts;
a vote that never ends, or a history that stays unreadable, never counts against the wearer.

Never: a check during a vacation, for a commitment that mentions health or safety, more than one a day, or while another is open (Chaster
allows one open task). It changes no time, freezes nothing and unlocks nothing; its two Chaster writes happen only while the runtime halt is clear.
"""

from __future__ import annotations

import logging
import random
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable

from application.models import ActionSpec
from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.decision_log import decision
from infrastructure.outbox import DomainEvent, write_event
from infrastructure.time_format import iso as _iso
from infrastructure.time_format import parse_iso as _parse_iso
from notifications.queue import enqueue_in_transaction
from supervision.checkin_evaluator import mentions_safety_issue
from supervision.models import TrustLevel, trust_level

logger = logging.getLogger("ai_coach_keyholder.proof_checks")

__all__ = ["ProofChecks", "CHECK_RATE", "PROOF_DEADLINE", "FREE_PASSES", "PASS_WINDOW", "VOTE_LIMIT", "UNKNOWN_LIMIT", "task_text", "Settle", "can_be_shown"]

# Owner, 2026-11-13: "10 / 20 / 35 / 50 % by level, at most one a day".
CHECK_RATE = {TrustLevel.LEVEL_4: 0.10, TrustLevel.LEVEL_3: 0.20, TrustLevel.LEVEL_2: 0.35, TrustLevel.LEVEL_1: 0.50}
PROOF_DEADLINE = timedelta(minutes=60)
CLOCK_SKEW = timedelta(minutes=2)
FREE_PASSES = 1                                  # "I can't show it now" without a cost, per PASS_WINDOW
PASS_WINDOW = timedelta(days=7)
VOTE_LIMIT = timedelta(hours=12)                 # a community vote still open this long after the submission: unknown, never a miss
UNKNOWN_LIMIT = timedelta(hours=6)               # past the deadline with an unreadable history this long: cancelled, never a miss
_OPEN = ("selected", "assigned", "submitted")
# Design section 10: what cannot be photographed is never challenged. Sleep and bedtime, and anything said as NOT doing something (abstinence,
# "less", "avoid") have no photo that proves them; they stay ordinary reports (contradictions come in slice 4). English and Czech, deliberately broad:
# a commitment wrongly left unchecked costs nothing, one wrongly checked would be unfair.
_CANNOT_BE_SHOWN = re.compile(
    r"\b(sleep\w*|slept|bed|beds|bedtime|nap\w*|wake|waking|woke|rest|resting|relax\w*|no|not|dont|don't|never|avoid\w*|stop\w*|quit\w*|skip\w*|"
    r"without|less|fewer|limit\w*|resist\w*|abstain\w*|refrain\w*|"
    r"spat|spim|spanek|vyspat|postel\w*|nebudu|nedam|nepit|nejist|bez|mene|vyhnout|prestat|omezit)\b")


def can_be_shown(commitment_text: str) -> bool:
    folded = unicodedata.normalize("NFKD", commitment_text.lower()).replace("’", "'").encode("ascii", "ignore").decode("ascii")
    return _CANNOT_BE_SHOWN.search(folded) is None


class Settle:
    KEPT = "kept"            # the Kept counts as reported
    UNPROVEN = "unproven"    # recorded as kept, earns nothing
    NOT_KEPT = "not_kept"    # counts as Not kept


def task_text(commitment_text: str) -> str:
    """Owner, 2026-11-13: short and plain, so the reviewers know what to look for ("Show your lockbox")."""
    text = " ".join(commitment_text.split())
    return f"Show: {text[:180]}"


def challenge_text(commitment_text: str, deadline_local: str) -> str:
    return (f"**Spot check.** Show me \"{commitment_text}\": in Chaster, complete the task with a photo that shows it and the code Chaster gives "
            f"you. You have until {deadline_local}. The community checks the photo; I never see it.")


@dataclass(frozen=True, kw_only=True)
class Ports:
    lock_id: Callable[[datetime], str | None]
    halted: Callable[[], bool]
    honesty_score: Callable[[], float | None]        # None: no Honesty domain, so no checks at all
    paused: Callable[[datetime], bool]
    today: Callable[[datetime], date]
    local_clock: Callable[[datetime], str]          # "HH:MM" in the wearer's time
    settle: Callable[[str, str, datetime], None]   # (commitment id, Settle.*, now): the coach desk records what the check decided
    raise_failure: Callable[[dict, str, datetime], None]    # (check row, reason, now): the infractions path
    quiet_between: Callable[[datetime, datetime], bool] = lambda _start, _end: False    # any quiet hours in [start, end]: no check then


class ProofChecks:
    def __init__(self, core: CoreDatabase, *, user_id: str, honesty_domain: str, chaster, ports: Ports,
                 rng: Callable[[], float] = random.SystemRandom().random) -> None:
        self._core = core
        self.user_id = user_id
        self._domain = honesty_domain
        self._chaster = chaster
        self._ports = ports
        self._rng = rng
        self.contradictions = None          # proof_checks.contradictions.BedtimeContradictions, set by the wiring (slice 4)

    # ------------------------------------------------------------------ select (from the coach desk)

    def select(self, commitment_id: str, text: str, now: datetime) -> bool:
        """Whether this timely Kept is checked. Picked: a `selected` row exists and the desk records the Kept as `checking`."""
        try:
            score = self._ports.honesty_score()
        except Exception:
            logger.exception("The Honesty score could not be read; no spot check.")
            return False
        if score is None or self._ports.paused(now) or mentions_safety_issue(text) or not can_be_shown(text):
            return False
        if self._ports.quiet_between(now, now + PROOF_DEADLINE):          # a photo demand never reaches into quiet hours
            return False
        rate = CHECK_RATE[trust_level(min(max(score, 0.0), 1.0))]
        local_day = self._ports.today(now).isoformat()

        def write(tx: Transaction, _s: object) -> bool:
            if tx.fetch_one(f"SELECT 1 FROM proof_checks WHERE user_id = ? AND (status IN {_OPEN} OR local_date = ?)", (self.user_id, local_day)):
                return False
            if self._rng() >= rate:
                return False
            tx.execute("INSERT OR IGNORE INTO proof_checks (id, user_id, commitment_id, commitment_text, local_date, status, created_at) "
                       "VALUES (?, ?, ?, ?, ?, 'selected', ?)", (str(uuid.uuid4()), self.user_id, commitment_id, text, local_day, _iso(now)))
            return True

        picked = apply_transition(self._core, write=write)
        if picked:
            decision("proof_checks", "A Done report was picked for a spot check.", commitment_id=commitment_id, rate=rate)
        return picked

    # ------------------------------------------------------------------ start

    def start_pending(self, now: datetime) -> int:
        """Assigns the Chaster task of every `selected` check and sends the challenge. A check that cannot start is cancelled: the Kept counts."""
        with self._core.transaction() as tx:
            rows = [dict(r) for r in tx.fetch_all("SELECT * FROM proof_checks WHERE user_id = ? AND status = 'selected'", (self.user_id,))]
        started = 0
        for row in rows:
            try:
                started += self._start(row, now)
            except Exception:
                logger.exception("A spot check could not start; it is cancelled and the report counts.")
                self._finish(row, "cancelled", "not_started", Settle.KEPT, now)
        return started

    def _start(self, row: dict, now: datetime) -> int:
        lock_id = self._ports.lock_id(now)
        if lock_id is None or self._ports.halted() or self._ports.paused(now):
            self._finish(row, "cancelled", "not_started", Settle.KEPT, now)
            return 0
        text = task_text(row["commitment_text"])
        if self._chaster.current_task(lock_id) is not None:          # Chaster allows one open task; never take the wearer's own away
            self._finish(row, "cancelled", "not_started", Settle.KEPT, now)
            return 0
        task_id = self._chaster.assign(lock_id, text)
        deadline = now + PROOF_DEADLINE

        def write(tx: Transaction, _s: object) -> bool:
            done = tx.execute("UPDATE proof_checks SET status = 'assigned', lock_id = ?, task_id = ?, deadline = ? WHERE id = ? AND status = 'selected'",
                              (lock_id, task_id, _iso(deadline), row["id"])).rowcount
            if done:
                write_event(tx, DomainEvent(event_type="proof_checks.check_started", source_module="proof_checks", occurred_at=now,
                                            payload={"check_id": row["id"], "commitment_id": row["commitment_id"]}))
                enqueue_in_transaction(tx, user_id=self.user_id, kind="proof_check", dedupe_key=f"proof:challenge:{row['id']}",
                                       text=challenge_text(row["commitment_text"], self._ports.local_clock(deadline)), now=now, immediate=True,
                                       actions=(ActionSpec(action_id=f"proof:pass:{row['id']}", label="I can't show it now"),
                                                ActionSpec(action_id=f"proof:retract:{row['id']}", label="I didn't actually do it", style="danger")))
            return bool(done)

        if apply_transition(self._core, write=write):
            decision("proof_checks", "A spot check started: a Chaster task was assigned.", check_id=row["id"], deadline=_iso(deadline))
            return 1
        return 0

    # ------------------------------------------------------------------ poll

    def poll(self, now: datetime) -> int:
        """Reads the open checks' tasks. Returns how many were decided."""
        with self._core.transaction() as tx:
            rows = [dict(r) for r in tx.fetch_all("SELECT * FROM proof_checks WHERE user_id = ? AND status IN ('assigned', 'submitted')", (self.user_id,))]
        decided = 0
        for row in rows:
            try:
                decided += self._poll_one(row, now)
            except Exception:
                logger.exception("Polling a spot check failed; it is retried.")
        return decided

    def _poll_one(self, row: dict, now: datetime) -> int:
        deadline = _parse_iso(row["deadline"])
        try:
            entry = self._chaster.task_entry(row["lock_id"], row["task_id"])
        except Exception:
            since = _parse_iso(row["unknown_since"]) if row["unknown_since"] else now
            if not row["unknown_since"]:
                self._update(row["id"], unknown_since=_iso(now))
            if now > max(deadline, since) + UNKNOWN_LIMIT:
                self._finish(row, "cancelled", "unknown", Settle.KEPT, now)
                return 1
            return 0
        if entry is None or entry.get("status") == "removed":
            if now <= deadline + CLOCK_SKEW:
                return 0
            try:
                current = self._chaster.current_task(row["lock_id"])
                if current is not None and current.get("id") == row["task_id"] and not self._ports.halted():
                    self._chaster.unassign(row["lock_id"])
            except Exception:
                logger.exception("A missed spot check's task could not be taken back; it is still decided.")
            return self._fail(row, "missed", now)
        submitted = _parse_iso(entry["createdAt"]) if isinstance(entry.get("createdAt"), str) else None
        if submitted is not None and submitted > deadline + CLOCK_SKEW:
            return self._fail(row, "late", now)
        review = entry.get("peerVerification") if isinstance(entry.get("peerVerification"), dict) else {}
        status = review.get("status")
        if row["status"] == "assigned" or row["review_status"] != status:
            self._update(row["id"], status="submitted", submitted_at=_iso(submitted) if submitted else None,
                         review_id=review.get("_id") if isinstance(review.get("_id"), str) else None, review_status=status)
        if status == "verified":
            self._finish(row, "passed", "verified", Settle.KEPT, now, bonus=True)
            return 1
        if status == "rejected":
            return self._fail(row, "rejected", now)
        if submitted is not None and now > submitted + VOTE_LIMIT:
            self._finish(row, "passed", "unknown", Settle.KEPT, now)
            return 1
        return 0

    def _fail(self, row: dict, reason: str, now: datetime) -> int:
        if self._finish(row, "failed", reason, Settle.UNPROVEN, now):
            self._ports.raise_failure(row, reason, now)
        return 1

    # ------------------------------------------------------------------ the wearer's buttons

    def press(self, *, user_id: str, check_id: str, action: str, now: datetime) -> str:
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT * FROM proof_checks WHERE id = ?", (check_id,))
        if row is None or row["user_id"] != user_id or user_id != self.user_id:
            return "That isn't yours to decide."
        row = dict(row)
        if row["status"] not in ("assigned",):
            return {"submitted": "Your photo is already in review.", "passed": "That check is already passed."}.get(row["status"], "That check is closed.")
        if action == "retract":
            self._take_back(row)
            self._finish(row, "retracted", "retracted", Settle.NOT_KEPT, now)
            return "Thank you for being straight about it. It counts as not done, and nothing more."
        if action == "pass":
            with self._core.transaction() as tx:
                used = tx.fetch_one("SELECT COUNT(*) AS n FROM proof_checks WHERE user_id = ? AND outcome = 'free_pass' AND decided_at >= ?",
                                    (user_id, _iso(now - PASS_WINDOW)))["n"]
            self._take_back(row)
            if used < FREE_PASSES:
                self._finish(row, "declined", "free_pass", Settle.UNPROVEN, now)
                return "Okay. It's recorded, but it doesn't count for you. That was your free pass for this week."
            self._finish(row, "declined", "pass_used_up", Settle.NOT_KEPT, now)
            return "Okay. You already used this week's free pass, so it counts as not done."
        return "That isn't one of the options."

    def _take_back(self, row: dict) -> None:
        try:
            if not self._ports.halted():
                current = self._chaster.current_task(row["lock_id"])
                if current is not None and current.get("id") == row["task_id"]:
                    self._chaster.unassign(row["lock_id"])
        except Exception:
            logger.exception("The spot check's task could not be taken back in Chaster.")

    # ------------------------------------------------------------------ reads

    def is_check_task(self, task_id: str | None) -> bool:
        """For the community review watch: a spot check's review is decided here, never a second time as a rejected photo."""
        if not task_id:
            return False
        with self._core.transaction() as tx:
            return tx.fetch_one("SELECT 1 FROM proof_checks WHERE task_id = ?", (task_id,)) is not None

    def get_open_check(self) -> dict | None:
        with self._core.transaction() as tx:
            row = tx.fetch_one(f"SELECT * FROM proof_checks WHERE user_id = ? AND status IN {_OPEN}", (self.user_id,))
        return None if row is None else dict(row)

    def get_checks_between(self, start: date, end: date) -> list[dict]:
        """Roadmap slice 5 (weekly review): the status of each spot check for `start`..`end` (local dates, inclusive). No commitment text."""
        with self._core.transaction() as tx:
            rows = tx.fetch_all("SELECT local_date, status, outcome FROM proof_checks WHERE user_id = ? AND local_date BETWEEN ? AND ?",
                                (self.user_id, start.isoformat(), end.isoformat()))
        return [dict(r) for r in rows]

    def forget_all(self, user_id: str) -> int:
        def write(tx: Transaction, _s: object) -> int:
            return tx.execute("UPDATE proof_checks SET commitment_text = '' WHERE user_id = ?", (user_id,)).rowcount

        return apply_transition(self._core, write=write)

    # ------------------------------------------------------------------ internals

    def _update(self, check_id: str, **fields) -> None:
        def write(tx: Transaction, _s: object) -> None:
            tx.execute(f"UPDATE proof_checks SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?", (*fields.values(), check_id))

        apply_transition(self._core, write=write)

    def _finish(self, row: dict, status: str, outcome: str, settle: str, now: datetime, *, bonus: bool = False) -> bool:
        def write(tx: Transaction, _s: object) -> bool:
            done = tx.execute(f"UPDATE proof_checks SET status = ?, outcome = ?, decided_at = ? WHERE id = ? AND status IN {_OPEN}",
                              (status, outcome, _iso(now), row["id"])).rowcount
            if done:
                write_event(tx, DomainEvent(event_type="proof_checks.check_decided", source_module="proof_checks", occurred_at=now,
                                            payload={"check_id": row["id"], "commitment_id": row["commitment_id"], "status": status, "outcome": outcome}))
                if bonus:
                    write_event(tx, DomainEvent(event_type="proof_checks.check_passed", source_module="proof_checks", occurred_at=now,
                                                payload={"check_id": row["id"], "trust_domain": self._domain}))
            return bool(done)

        finished = apply_transition(self._core, write=write)
        if finished:
            decision("proof_checks", "A spot check was decided.", check_id=row["id"], status=status, outcome=outcome)
            try:
                self._ports.settle(row["commitment_id"], settle, now)
            except Exception:
                logger.exception("The coach desk could not record a spot check's result.")
        return finished
