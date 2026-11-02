"""
supervision/checkins.py

Proactive check-ins (v0.39 Phase 2): scheduling, at-most-once delivery, response capture,
LLM-rubric evaluation (as a classifier only) and the deadline rules.

Cadence: check-ins apply whenever the Trust level is below Level 4 (score < 0.85):
Level 3 -> 1 per day, Level 2 -> 2, Level 1 -> 3. They are scheduled ONLY in the discretionary
window (never quiet hours, never work hours), and re-checked at send time.

Lifecycle (compare-and-set transitions, every one idempotent):
  scheduled -> sending (claimed BEFORE the send) -> sent | undeliverable | needs_review
  sent --reply--> responded --evaluate--> answered | answered_uncertain | insufficient_pending
  sent --60 min, no reply--> missed                       (event check_in.missed)
  insufficient_pending --follow-up reply--> responded ... (30 minute follow-up window)
  insufficient_pending --30 min, still not sufficient--> insufficient   (event check_in.insufficient)
  (strict Supervision Mode, Trust < 0.40: the deadline is 30 minutes and the follow-up window 15)
  scheduled --too late / wrong window / level now 4--> skipped          (never a penalty)

Never a penalty: UNCERTAIN / NOT_EVALUATED verdicts, undeliverable or skipped prompts, a
crash while sending (needs_review). Events carry ids and the Trust domain only, never the
user's text. The reply text is stored locally for evaluation only.
"""

from __future__ import annotations

import logging
import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Callable, Protocol

from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.outbox import DomainEvent, write_event
from infrastructure.time_format import iso as _iso
from infrastructure.time_format import parse_iso as _parse_iso
from schedule_windows.windows import WindowConfig, WindowKind, classify
from supervision.checkin_evaluator import RUBRIC_VERSION, Verdict
from supervision.checkin_schedule import slots_for_day
from supervision.escalation import CheckInEscalation
from supervision.models import TrustLevel, check_ins_per_day, trust_level

logger = logging.getLogger("ai_coach_keyholder.supervision.checkins")

__all__ = [
    "CheckInService", "CheckInSender", "SendOutcome", "RESPONSE_TIMEOUT", "FOLLOW_UP_WINDOW", "MAX_LATE", "PROMPTS",
    "STRICT_RESPONSE_TIMEOUT", "STRICT_FOLLOW_UP_WINDOW",
]

RESPONSE_TIMEOUT = timedelta(minutes=60)     # CHECKIN_RESPONSE_TIMEOUT_MINUTES
FOLLOW_UP_WINDOW = timedelta(minutes=30)     # after an INSUFFICIENT verdict
STRICT_RESPONSE_TIMEOUT = timedelta(minutes=30)   # strict Supervision Mode (Trust < 0.40)
STRICT_FOLLOW_UP_WINDOW = timedelta(minutes=15)
MAX_LATE = timedelta(minutes=30)             # a slot that is this overdue is skipped, not sent
_STALE_SENDING = timedelta(minutes=5)
_MAX_RESPONSE_CHARS = 4000

PROMPTS = {
    TrustLevel.LEVEL_1: "Check-in time. Tell me plainly what you've done today toward your goals and how you're "
                        "holding up. I expect a real answer.",
    TrustLevel.LEVEL_2: "Check-in. What have you done toward your goals today, and how are you doing?",
    TrustLevel.LEVEL_3: "Evening check-in: how did today go with your goals?",
}


class SendOutcome(StrEnum):
    DELIVERED = "delivered"
    UNDELIVERABLE = "undeliverable"     # e.g. direct messages closed: provably not delivered
    UNKNOWN = "unknown"                 # ambiguous: may or may not have arrived


class CheckInSender(Protocol):
    def send(self, *, user_id: str, text: str) -> SendOutcome: ...


@dataclass(frozen=True, kw_only=True)
class CheckInRow:
    id: str
    status: str
    due_at: datetime
    verdict: str | None


class CheckInService:
    def __init__(
        self,
        db_path: str | Path,
        *,
        core: CoreDatabase | None = None,
        window_config: WindowConfig,
        user_id: str,
        trust_domain: str,
        get_trust_score: Callable[[], float | None],
        sender: CheckInSender,
        evaluate: Callable[[str, str], Verdict],
        rand_fraction: Callable[[], float] = random.SystemRandom().random,
        is_strict: Callable[[], bool] = lambda: False,
        response_timeout: timedelta = RESPONSE_TIMEOUT,
        follow_up_window: timedelta = FOLLOW_UP_WINDOW,
        strict_response_timeout: timedelta = STRICT_RESPONSE_TIMEOUT,
        strict_follow_up_window: timedelta = STRICT_FOLLOW_UP_WINDOW,
        escalation: "CheckInEscalation | None" = None,
        paused: Callable[[datetime], bool] = lambda _now: False,
    ) -> None:
        """`paused(now)` (Phase G6, iron_contract.vacation): while it is true no check-in is scheduled or sent, and open ones are closed as
        `skipped`, so no deadline can turn into a miss. Replies already received are still evaluated."""
        self.db_path = Path(db_path)
        self._core = core if core is not None else CoreDatabase(self.db_path)
        self._cfg = window_config
        self._user = user_id
        self._domain = trust_domain
        self._score = get_trust_score
        self._sender = sender
        self._evaluate = evaluate
        self._rand = rand_fraction
        self._is_strict = is_strict
        self._timeouts = (response_timeout, follow_up_window)
        self._strict_timeouts = (strict_response_timeout, strict_follow_up_window)
        self._escalation = escalation
        self._paused = paused

    # ------------------------------------------------------------------ driver

    def tick(self, now: datetime) -> None:
        self._recover_orphans(now)
        self._evaluate_responses(now)
        if self._is_paused(now):
            self._close_open_for_pause(now)
            return
        self._apply_deadlines(now)
        self._schedule_today(now)
        self._send_due(now)
        if self._escalation is not None:
            try:
                self._escalation.run(now)
            except Exception:
                logger.exception("Check-in escalation failed; will retry next cycle.")

    def _is_paused(self, now: datetime) -> bool:
        try:
            return bool(self._paused(now))
        except Exception:
            logger.exception("Could not read the vacation state; check-ins continue.")
            return False

    def _close_open_for_pause(self, now: datetime) -> None:
        """A vacation is in force: every scheduled, sent or follow-up check-in becomes `skipped` (never a miss, never a penalty)."""
        def write(tx: Transaction, _s: object) -> int:
            return tx.execute("UPDATE check_ins SET status = 'skipped', updated_at = ? WHERE user_id = ? AND status IN "
                              "('scheduled', 'sent', 'insufficient_pending')", (_iso(now), self._user)).rowcount

        apply_transition(self._core, write=write)

    def _response_timeout(self) -> timedelta:
        """Strict Supervision Mode (Trust < 0.40) shortens the response deadline (60 -> 30 minutes)."""
        return (self._strict_timeouts if self._strict() else self._timeouts)[0]

    def _follow_up_window(self) -> timedelta:
        return (self._strict_timeouts if self._strict() else self._timeouts)[1]

    def _strict(self) -> bool:
        try:
            return bool(self._is_strict())
        except Exception:
            logger.exception("Could not read the supervision state; using the normal deadlines.")
            return False

    # ------------------------------------------------------------------ user replies

    def on_user_message(self, user_id: str, text: str, now: datetime) -> bool:
        """Called for a free-text message (never a command) from the user. If a check-in is waiting
        for an answer, records the reply for evaluation. Returns True if it was consumed as a reply
        (the normal conversation still proceeds)."""
        if user_id != self._user or not isinstance(text, str) or not text.strip():
            return False
        text = text.strip()[:_MAX_RESPONSE_CHARS]

        def write(tx: Transaction, _s: object) -> bool:
            row = tx.fetch_one(
                "SELECT * FROM check_ins WHERE user_id = ? AND ("
                " (status = 'sent' AND deadline_at >= ?) OR (status = 'insufficient_pending' AND follow_up_until >= ?)"
                " OR status = 'responded') ORDER BY due_at DESC LIMIT 1",
                (self._user, _iso(now), _iso(now)),
            )
            if row is None:
                return False
            if row["status"] == "responded":
                merged = (row["response_text"] or "") + "\n" + text
                tx.execute("UPDATE check_ins SET response_text = ?, updated_at = ? WHERE id = ?",
                           (merged[:_MAX_RESPONSE_CHARS], _iso(now), row["id"]))
            else:
                tx.execute("UPDATE check_ins SET status = 'responded', response_text = ?, responded_at = ?, "
                           "updated_at = ? WHERE id = ? AND status = ?", (text, _iso(now), _iso(now), row["id"], row["status"]))
            return True

        return apply_transition(self._core, write=write)

    # ------------------------------------------------------------------ reads

    def last_check_in_missed(self) -> bool:
        """True when the most recent concluded check-in was missed (cleared by the next concluded one)."""
        with self._core.transaction() as tx:
            row = tx.fetch_one(
                "SELECT status FROM check_ins WHERE user_id = ? AND status IN "
                "('answered', 'answered_uncertain', 'missed', 'insufficient') ORDER BY due_at DESC LIMIT 1",
                (self._user,),
            )
        return row is not None and row["status"] == "missed"

    def list_recent(self, limit: int = 20) -> list[CheckInRow]:
        with self._core.transaction() as tx:
            rows = tx.fetch_all("SELECT id, status, due_at, verdict FROM check_ins WHERE user_id = ? "
                                "ORDER BY due_at DESC LIMIT ?", (self._user, limit))
        return [CheckInRow(id=r["id"], status=r["status"], due_at=_parse_iso(r["due_at"]), verdict=r["verdict"]) for r in rows]

    # ------------------------------------------------------------------ steps

    def _level(self) -> TrustLevel | None:
        score = self._score()
        return None if score is None else trust_level(float(score))

    def _schedule_today(self, now: datetime) -> None:
        level = self._level()
        if level is None:
            return
        n = check_ins_per_day(level)
        local_date = self._cfg.local(now).date()

        def write(tx: Transaction, _s: object) -> int:
            # Level improved (or is now 4): unsent slots beyond the new count are skipped.
            tx.execute("UPDATE check_ins SET status = 'skipped', updated_at = ? WHERE user_id = ? AND local_date = ? "
                       "AND status = 'scheduled' AND slot_index >= ?", (_iso(now), self._user, local_date.isoformat(), n))
            made = 0
            for index, due in enumerate(slots_for_day(self._cfg, local_date, n, self._rand)):
                if due <= now + timedelta(minutes=1):
                    continue
                if tx.fetch_one("SELECT 1 FROM check_ins WHERE user_id = ? AND local_date = ? AND slot_index = ?",
                                (self._user, local_date.isoformat(), index)):
                    continue
                tx.execute(
                    "INSERT INTO check_ins (id, user_id, local_date, slot_index, level, due_at, status, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'scheduled', ?, ?)",
                    (str(uuid.uuid4()), self._user, local_date.isoformat(), index, int(level), _iso(due), _iso(now), _iso(now)),
                )
                made += 1
            return made

        apply_transition(self._core, write=write)

    def _send_due(self, now: datetime) -> None:
        with self._core.transaction() as tx:
            due = tx.fetch_all("SELECT * FROM check_ins WHERE user_id = ? AND status = 'scheduled' AND due_at <= ? "
                               "ORDER BY due_at", (self._user, _iso(now)))
            awaiting = tx.fetch_one("SELECT 1 FROM check_ins WHERE user_id = ? AND status IN "
                                    "('sent', 'responded', 'insufficient_pending', 'sending')", (self._user,))
        for row in due:
            if now - _parse_iso(row["due_at"]) > MAX_LATE or classify(self._cfg, now) is not WindowKind.DISCRETIONARY:
                self._cas(row["id"], ("scheduled",), "skipped", now)
                continue
            if awaiting is not None:            # one open check-in at a time; this one waits (or is skipped later)
                continue
            level = self._level() or TrustLevel(int(row["level"]))
            prompt = PROMPTS.get(level)
            if prompt is None:                  # level 4 now: nothing to ask
                self._cas(row["id"], ("scheduled",), "skipped", now)
                continue
            if not self._cas(row["id"], ("scheduled",), "sending", now, prompt_text=prompt, event="check_in.prompt_due"):
                continue
            awaiting = True
            try:
                outcome = self._sender.send(user_id=self._user, text=prompt)
            except Exception:
                logger.exception("Check-in delivery raised; outcome unknown.")
                outcome = SendOutcome.UNKNOWN
            if outcome is SendOutcome.DELIVERED:
                self._cas(row["id"], ("sending",), "sent", now, sent_at=_iso(now), deadline_at=_iso(now + self._response_timeout()))
            elif outcome is SendOutcome.UNDELIVERABLE:
                self._cas(row["id"], ("sending",), "undeliverable", now)
            else:
                self._cas(row["id"], ("sending",), "needs_review", now)

    def _evaluate_responses(self, now: datetime) -> None:
        with self._core.transaction() as tx:
            rows = tx.fetch_all("SELECT * FROM check_ins WHERE user_id = ? AND status = 'responded'", (self._user,))
        for row in rows:
            try:
                verdict = self._evaluate(row["prompt_text"] or "", row["response_text"] or "")
            except Exception:
                verdict = Verdict.NOT_EVALUATED
            common = dict(verdict=verdict.value, rubric_version=RUBRIC_VERSION)
            if verdict is Verdict.SUFFICIENT:
                self._cas(row["id"], ("responded",), "answered", now, **common)
            elif verdict is Verdict.INSUFFICIENT:
                follow_up = row["follow_up_until"] or _iso(now + self._follow_up_window())
                self._cas(row["id"], ("responded",), "insufficient_pending", now, follow_up_until=follow_up, **common)
            else:     # UNCERTAIN / NOT_EVALUATED: zero consequence (fail closed towards no penalty)
                self._cas(row["id"], ("responded",), "answered_uncertain", now, **common)

    def _apply_deadlines(self, now: datetime) -> None:
        with self._core.transaction() as tx:
            missed = tx.fetch_all("SELECT id, due_at FROM check_ins WHERE user_id = ? AND status = 'sent' AND deadline_at < ?",
                                  (self._user, _iso(now)))
            insufficient = tx.fetch_all("SELECT id FROM check_ins WHERE user_id = ? AND status = 'insufficient_pending' "
                                        "AND follow_up_until < ?", (self._user, _iso(now)))
        for r in sorted(missed, key=lambda row: row["due_at"]):
            streak = self._miss_streak(r["id"], r["due_at"])
            self._cas(r["id"], ("sent",), "missed", now, event="check_in.missed", payload={"streak": streak})
        for r in insufficient:
            self._cas(r["id"], ("insufficient_pending",), "insufficient", now, event="check_in.insufficient")

    def _miss_streak(self, check_in_id: str, due_at: str) -> int:
        """Position of this miss in a run of consecutive misses: 1 for the first, 2 for the second, ... Only
        concluded check-ins count (skipped / undeliverable prompts neither extend nor break a run)."""
        with self._core.transaction() as tx:
            rows = tx.fetch_all(
                "SELECT status FROM check_ins WHERE user_id = ? AND due_at < ? AND status IN "
                "('answered', 'answered_uncertain', 'insufficient', 'missed') ORDER BY due_at DESC LIMIT 50",
                (self._user, due_at))
        run = 0
        for r in rows:
            if r["status"] != "missed":
                break
            run += 1
        return run + 1

    def _recover_orphans(self, now: datetime) -> None:
        with self._core.transaction() as tx:
            rows = tx.fetch_all("SELECT id FROM check_ins WHERE user_id = ? AND status = 'sending' AND updated_at < ?",
                                (self._user, _iso(now - _STALE_SENDING)))
        for r in rows:
            self._cas(r["id"], ("sending",), "needs_review", now)

    # ------------------------------------------------------------------ CAS

    def _cas(self, check_in_id: str, from_statuses: tuple[str, ...], to_status: str, now: datetime,
             event: str | None = None, payload: dict | None = None, **columns: str | None) -> bool:
        allowed = {"prompt_text", "sent_at", "deadline_at", "verdict", "rubric_version", "follow_up_until"}
        assert set(columns) <= allowed, set(columns) - allowed
        assignments = ", ".join(f"{c} = ?" for c in columns)
        marks = ",".join("?" for _ in from_statuses)

        def write(tx: Transaction, _s: object) -> bool:
            row = tx.fetch_one(f"SELECT status FROM check_ins WHERE id = ? AND status IN ({marks})", (check_in_id, *from_statuses))
            if row is None:
                return False
            tx.execute(
                f"UPDATE check_ins SET status = ?, updated_at = ?{', ' + assignments if assignments else ''} WHERE id = ?",
                (to_status, _iso(now), *columns.values(), check_in_id),
            )
            if event is not None:
                write_event(tx, DomainEvent(
                    event_type=event, source_module="supervision",
                    payload={"check_in_id": check_in_id, "trust_domain": self._domain, **(payload or {})}, occurred_at=now,
                ))
            return True

        return apply_transition(self._core, write=write)
