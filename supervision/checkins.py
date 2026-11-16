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

The RELEASE check-in (Phase J2, slot RELEASE_SLOT): opened on demand by the Communication-First release flow (system/release_notice.py). The
same lifecycle, except that it can never cost anything: submitting a reply is what counts (a verdict is recorded but cannot fail it), and
left unanswered it expires as `skipped`, never `missed`, so ignoring a release has no Trust effect.

Never a penalty: UNCERTAIN / NOT_EVALUATED verdicts, undeliverable or skipped prompts, a
crash while sending (needs_review). Events carry ids and the Trust domain only, never the
user's text. The reply text is stored locally for evaluation; with the coach and Coaching Memory on,
a filtered, dated copy of it also goes to Coaching Memory (system/coach_dm.py).
"""

from __future__ import annotations

import logging
import random
import uuid
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Callable, Protocol

from infrastructure.database import Database as CoreDatabase
from infrastructure.decision_log import decision
from infrastructure.database import Transaction, apply_transition
from infrastructure.outbox import DomainEvent, write_event
from infrastructure.time_format import iso as _iso
from infrastructure.time_format import parse_iso as _parse_iso
from notifications.queue import enqueue_in_transaction
from schedule_windows.windows import WindowConfig, WindowKind, classify, is_non_working_day
from supervision.checkin_evaluator import RUBRIC_VERSION, Verdict
from supervision.checkin_schedule import slots_for_day
from supervision.escalation import CheckInEscalation
from supervision.models import TrustLevel, check_ins_per_day, trust_level
from supervision.photo_check_ins import (
    PHOTO_CHECK_INS_PER_WEEK,
    PHOTO_CLOCK_SKEW,
    PHOTO_POLL,
    PHOTO_RECEIVED,
    PHOTO_RESPONSE_TIMEOUT,
    PHOTO_SLOT,
    PHOTO_UNKNOWN_LIMIT,
    PhotoCheckInConsent,
    photo_prompt,
)

logger = logging.getLogger("ai_coach_keyholder.supervision.checkins")

__all__ = [
    "CheckInService", "CheckInSender", "SendOutcome", "RESPONSE_TIMEOUT", "FOLLOW_UP_WINDOW", "MAX_LATE", "PROMPTS",
    "STRICT_RESPONSE_TIMEOUT", "STRICT_FOLLOW_UP_WINDOW", "RELEASE_SLOT", "RELEASE_CHECK_IN_VALIDITY",
]

RESPONSE_TIMEOUT = timedelta(minutes=60)     # CHECKIN_RESPONSE_TIMEOUT_MINUTES
FOLLOW_UP_WINDOW = timedelta(minutes=30)     # after an INSUFFICIENT verdict
STRICT_RESPONSE_TIMEOUT = timedelta(minutes=30)   # strict Supervision Mode (Trust < 0.40)
STRICT_FOLLOW_UP_WINDOW = timedelta(minutes=15)
QUIET_AFTER_ANSWER = timedelta(hours=3)       # owner, 2026-11-10: an hour after answering, the same question came again
# Owner, 2026-11-16: "while in Supervision Mode, 3 check-ins a day (Level 1 cadence) regardless of your level". Decided by Claude: the quiet
# time after an answer is shorter then, or the 3-hour gap would silently skip one of the three in an evening window.
SUPERVISION_CHECK_INS = 3
SUPERVISION_QUIET_AFTER_ANSWER = timedelta(minutes=90)
MAX_LATE = timedelta(minutes=30)             # a slot that is this overdue is skipped, not sent
_STALE_SENDING = timedelta(minutes=5)
MORNING_SLOT = 90                            # owner, 2026-11-15: the morning report (one a local day; below the release slot)
MORNING_DUE = (time(12, 0), time(13, 0))     # due by 12:00 on a workday, 13:00 on a weekend or public holiday
MORNING_AFTER_START = timedelta(hours=2)     # ...or 2 hours after the bot came back that morning, whichever is later
MORNING_ASK = ("Good morning. Your morning report: tell me your plan for today, in your own words. Any answer counts. "
               "Due by {due}; none by then counts as a missed check-in.")
DUTY_SLOTS = (91, 92, 93, 94, 95)                # protocol duties (owner, 2026-11-16; protocol/): thank-yous and the weekly reflection
RELEASE_SLOT = 100                           # above any scheduled slot index: one release check-in per local day
RELEASE_CHECK_IN_VALIDITY = timedelta(hours=12)
_MAX_RESPONSE_CHARS = 4000

# Owner, 2026-11-16: "make the check-ins so they're not the same and so boring". A pool per level and part of the day (before 17:00 local:
# the day so far; later: the day as a whole), picked at random without the last RECENT_PROMPTS ones. Each still asks for a real account
# (the rubric judges the answer against the question it was given), and the stricter the level, the more it demands.
PROMPTS: dict[TrustLevel, dict[str, tuple[str, ...]]] = {
    TrustLevel.LEVEL_1: {
        "day": (
            "Report in. What have you done since this morning, and how is the cage treating you? A real answer, not a one-liner.",
            "Stop and tell me: what have you actually done today so far, and what's still left? Be specific.",
            "Status, now. Where are you, what are you doing, and how are you holding up locked? Details.",
            "Midday check. What have you finished, what have you put off, and why? I want the honest version.",
            "Tell me what you've obeyed today and what you've dodged. Both, and properly.",
            "How hard is the cage on you today, and what have you done with your day despite it? A few real sentences.",
        ),
        "evening": (
            "Report in. What did you actually get done today, and how are you holding up? A real answer, not a one-liner.",
            "Your day, in full: what you did, what you skipped, and how the cage felt. No shortcuts.",
            "Evening report. What are you proud of today, and what would I be disappointed by? Be honest.",
            "Tell me about today: one thing you did well, one thing you didn't, and how locked you feel right now.",
            "Account for your day. What did you do, and did you do it the way you were told?",
            "How did denial treat you today? And what did you get done anyway? Details, please.",
        ),
    },
    TrustLevel.LEVEL_2: {
        "day": (
            "Hey you. How's the day going so far, and what have you done?",
            "Quick check: what are you up to right now, and how's the cage?",
            "How's your day been so far? Tell me one thing you've done and one you still have to do.",
            "What's on your mind right now, locked boy? And what have you got done today?",
            "Halfway through the day: how are you doing, and what's next?",
        ),
        "evening": (
            "Hey you. What did you get done today, and how are you doing?",
            "How did today go? What went well, and what didn't?",
            "Tell me about your day, and how being locked felt today.",
            "What was the best part of today, and the hardest?",
            "End of the day: what did you do, and how are you feeling now?",
        ),
    },
    TrustLevel.LEVEL_3: {
        "day": ("So, how's the day going?", "What are you up to?", "How's the cage today?", "Anything worth telling me so far today?"),
        "evening": ("So, how did today go?", "How was your day?", "Anything good happen today?", "How are you feeling tonight?"),
    },
}
RECENT_PROMPTS = 3          # the last few questions are never asked again straight away
EVENING_FROM = time(17, 0)


def pick_prompt(level: TrustLevel, local_now: datetime, recent: list[str], seed: str) -> str | None:
    """The question for a check-in (None at a level that asks nothing). Pure: `recent` = the last prompts sent (newest first)."""
    pool = PROMPTS.get(level)
    if pool is None:
        return None
    options = pool["evening" if local_now.time() >= EVENING_FROM else "day"]
    fresh = [p for p in options if not any(r and r.startswith(p) for r in recent[:RECENT_PROMPTS])] or list(options)
    return random.Random(seed).choice(fresh)


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
        # Phase J3: `prompt_suffix(now)`, set by the composition when the coach is on: today's commitment and the invitation to make the next one. Scheduled
        # check-ins only; the release check-in keeps the owner's wording. A failure leaves the plain question.
        self.prompt_suffix: Callable[[datetime], str] | None = None
        # Photo check-ins (owner, 2026-11-13; supervision/photo_check_ins.py): `photo_times(now)` returns the submission times of the wearer's
        # verification pictures, or None when they cannot be read. Without it, or without the wearer's consent, there are none.
        self.photo_times: Callable[[datetime], list[datetime] | None] | None = None
        self.photo_consent = PhotoCheckInConsent(self.db_path, core=self._core)
        self._photo_checked: dict[str, datetime] = {}
        # The morning report (owner, 2026-11-15; roadmap item 5, protocol rung 1). Off unless the wiring switches it on. `morning_suspended(now)`:
        # a wear exemption or the runtime halt (a vacation pauses everything anyway). `online_since`: when this process started.
        self.morning_on = False
        self.morning_suspended: Callable[[datetime], bool] = lambda now: False
        self.morning_enabled: Callable[[], bool] = lambda: True      # owner, 2026-11-16 (features/): off = no report, never a miss
        # Bug fix 2026-11-15: a photo check-in was sent while the cage was off for a wear exemption and then counted as missed. While
        # `photo_suspended(now)` (a wear exemption in force) none is sent, and an open one is skipped instead of missed.
        self.photo_suspended: Callable[[datetime], bool] = lambda now: False
        # Owner, 2026-11-16 (6e, A5): how many days the current lock has run, or None; +1 photo check-in a week from 7 days, +2 from 21.
        self.lock_days: Callable[[datetime], float | None] = lambda now: None
        self.online_since: datetime | None = None

    # ------------------------------------------------------------------ driver

    def tick(self, now: datetime) -> None:
        self._recover_orphans(now)
        self._evaluate_responses(now)
        if self._is_paused(now):
            self._close_open_for_pause(now)
            return
        self._check_photos(now)
        if self.morning_on:
            try:
                self._morning(now)
            except Exception:
                logger.exception("The morning report pass failed; it continues.")
        self._apply_deadlines(now)
        self._schedule_today(now)
        self._send_due(now)
        self._schedule_photo(now)       # after the sends, so an overdue one from an earlier day is already skipped and does not count
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
        """Owner, 2026-11-13: the time to answer is the normal one at every level. Strict Supervision Mode keeps more check-ins a day, but no longer
        halves the time to answer (60 -> 30 minutes): misses from being in the shower or driving deepened the very supervision answering ends.
        `strict_response_timeout` (CHECKIN_STRICT_RESPONSE_TIMEOUT_MINUTES) is therefore no longer used."""
        return self._timeouts[0]

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
        if self.morning_on and self._ask_morning(now):
            return False                # his first message of the day is not the report: the report is what he sends after the question

        def write(tx: Transaction, _s: object) -> bool:
            row = tx.fetch_one(
                "SELECT * FROM check_ins WHERE user_id = ? AND kind IS NULL AND slot_index NOT IN (91, 92, 93, 94, 95) AND ("
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

    def release_check_in_required(self, now: datetime) -> bool:
        """A check-in stands between an eligible user and a release only on a day check-ins apply at all."""
        if self._is_paused(now):
            return False
        level = self._level()
        return level is not None and PROMPTS.get(level) is not None

    def daily_check_in_complete(self, now: datetime) -> bool:
        """Today's check-in is done: a scheduled one was answered, or the release check-in got a reply."""
        with self._core.transaction() as tx:
            return tx.fetch_one(
                "SELECT 1 FROM check_ins WHERE user_id = ? AND local_date = ? AND (status IN ('answered', 'answered_uncertain') "
                "OR (slot_index = ? AND responded_at IS NOT NULL)) LIMIT 1",
                (self._user, self._cfg.local(now).date().isoformat(), RELEASE_SLOT)) is not None

    def open_release_check_in(self, text: str, now: datetime) -> SendOutcome:
        """Asks for today's check-in with `text`, at most once a day, claimed before the send. The reply lands here because it is the newest
        open check-in. UNKNOWN (a delivery that may have worked) stays open like a delivered one: a late reply still counts."""
        day = self._cfg.local(now).date().isoformat()
        with self._core.transaction() as tx:
            existing = tx.fetch_one("SELECT status FROM check_ins WHERE user_id = ? AND local_date = ? AND slot_index = ?",
                                    (self._user, day, RELEASE_SLOT))
        if existing is not None:
            return SendOutcome.UNDELIVERABLE if existing["status"] == "undeliverable" else SendOutcome.DELIVERED
        check_in_id = str(uuid.uuid4())
        level = self._level()

        def write(tx: Transaction, _s: object) -> None:
            tx.execute("INSERT INTO check_ins (id, user_id, local_date, slot_index, level, due_at, status, prompt_text, created_at, updated_at) "
                       "VALUES (?, ?, ?, ?, ?, ?, 'sending', ?, ?, ?)",
                       (check_in_id, self._user, day, RELEASE_SLOT, int(level) if level is not None else 3, _iso(now), text, _iso(now), _iso(now)))

        apply_transition(self._core, write=write)
        try:
            outcome = self._sender.send(user_id=self._user, text=text)
        except Exception:
            logger.exception("Release check-in delivery raised; outcome unknown.")
            outcome = SendOutcome.UNKNOWN
        if outcome is SendOutcome.UNDELIVERABLE:
            self._cas(check_in_id, ("sending",), "undeliverable", now)
        else:
            self._cas(check_in_id, ("sending",), "sent", now, sent_at=_iso(now), deadline_at=_iso(now + RELEASE_CHECK_IN_VALIDITY))
        return outcome

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
        if self._strict():
            n = max(n, SUPERVISION_CHECK_INS)          # owner, 2026-11-16: Supervision Mode asks three times a day
        local_date = self._cfg.local(now).date()

        def write(tx: Transaction, _s: object) -> int:
            # Level improved (or is now 4): unsent slots beyond the new count are skipped.
            tx.execute("UPDATE check_ins SET status = 'skipped', updated_at = ? WHERE user_id = ? AND local_date = ? "
                       "AND status = 'scheduled' AND kind IS NULL AND slot_index >= ? AND slot_index < ?",
                       (_iso(now), self._user, local_date.isoformat(), n, MORNING_SLOT))
            made = 0
            window = max(self._timeouts[0], self._strict_timeouts[0])      # the whole time to answer lies before quiet hours
            for index, due in enumerate(slots_for_day(self._cfg, local_date, n, self._rand, answer_window=window)):
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
            due = tx.fetch_all("SELECT * FROM check_ins WHERE user_id = ? AND status = 'scheduled' AND due_at <= ? AND slot_index != ? "
                               "ORDER BY due_at", (self._user, _iso(now), MORNING_SLOT))
            # A photo check-in is answered in Chaster, not by a text reply, so it neither waits for nor holds back a text check-in.
            awaiting = tx.fetch_one("SELECT 1 FROM check_ins WHERE user_id = ? AND kind IS NULL AND status IN "
                                    "('sent', 'responded', 'insufficient_pending', 'sending')", (self._user,))
            answered = tx.fetch_one("SELECT MAX(responded_at) AS at FROM check_ins WHERE user_id = ? AND responded_at IS NOT NULL", (self._user,))["at"]
            today = self._cfg.local(now).date().isoformat()
            later_today = tx.fetch_one("SELECT 1 FROM check_ins WHERE user_id = ? AND status = 'scheduled' AND local_date = ? AND due_at > ?",
                                       (self._user, today, _iso(now))) is not None
        quiet = SUPERVISION_QUIET_AFTER_ANSWER if self._strict() else QUIET_AFTER_ANSWER
        recently_answered = answered is not None and now - _parse_iso(answered) < quiet
        # Owner, 2026-11-10: a check-in that fell into a time the PC was off is not simply lost. The latest overdue one of today is sent late, once,
        # when nothing else is planned for the rest of the day; older ones are skipped as before.
        late_ok = None if later_today else next((r["id"] for r in reversed(due) if r["local_date"] == today), None)
        for row in due:
            too_late = now - _parse_iso(row["due_at"]) > MAX_LATE and row["id"] != late_ok
            photo = row["kind"] == "photo"
            if too_late or classify(self._cfg, now) is not WindowKind.DISCRETIONARY or (recently_answered and not photo) \
                    or (photo and (not self._photos_on() or self._photo_held(now))):
                self._cas(row["id"], ("scheduled",), "skipped", now)
                continue
            if photo:
                self._send_photo(row, now)
                continue
            if awaiting is not None:            # one open text check-in at a time; this one waits (or is skipped later)
                continue
            level = self._level() or TrustLevel(int(row["level"]))
            prompt = pick_prompt(level, self._cfg.local(now), self._recent_prompts(), row["id"])
            if prompt is None:                  # level 4 now: nothing to ask
                self._cas(row["id"], ("scheduled",), "skipped", now)
                continue
            prompt += self._suffix(now)
            if not self._cas(row["id"], ("scheduled",), "sending", now, prompt_text=prompt, event="check_in.prompt_due"):
                continue
            awaiting = True
            try:
                outcome = self._sender.send(user_id=self._user, text=prompt)
            except Exception:
                logger.exception("Check-in delivery raised; outcome unknown.")
                outcome = SendOutcome.UNKNOWN
            decision("check_ins", f"Check-in sent: {outcome.value}.", check_in_id=row["id"], level=int(level),
                     answer_by=_iso(now + self._response_timeout()) if outcome is SendOutcome.DELIVERED else None)
            if outcome is SendOutcome.DELIVERED:
                self._cas(row["id"], ("sending",), "sent", now, sent_at=_iso(now), deadline_at=_iso(now + self._response_timeout()))
            elif outcome is SendOutcome.UNDELIVERABLE:
                self._cas(row["id"], ("sending",), "undeliverable", now)
            else:
                self._cas(row["id"], ("sending",), "needs_review", now)

    # ------------------------------------------------------------------ protocol duties (owner, 2026-11-16; protocol/)

    def open_duty(self, slot: int, local_date: str, deadline: datetime, now: datetime) -> str | None:
        """A protocol duty owed by `deadline`: a check-in that is already 'sent' (protocol sends its own message). None when that slot of the
        day is taken. A miss is decided by the usual deadline pass, with the usual consequence."""
        assert slot in DUTY_SLOTS
        row_id = str(uuid.uuid4())

        def write(tx: Transaction, _s: object) -> str | None:
            if not tx.execute("INSERT OR IGNORE INTO check_ins (id, user_id, local_date, slot_index, level, due_at, status, sent_at, deadline_at, "
                              "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'sent', ?, ?, ?, ?)",
                              (row_id, self._user, local_date, slot, int(self._level() or TrustLevel(4)), _iso(now), _iso(now), _iso(deadline),
                               _iso(now), _iso(now))).rowcount:
                return None
            write_event(tx, DomainEvent(event_type="check_in.prompt_due", source_module="supervision", occurred_at=now,
                                        payload={"check_in_id": row_id, "duty": slot}))
            return row_id

        return apply_transition(self._core, write=write)

    def skip_duties(self, slots, now: datetime) -> int:
        """Open protocol duties in `slots` are skipped (never a miss): e.g. the kneel could not go on Chaster."""
        marks = ",".join("?" for _ in slots)
        with self._core.transaction() as tx:
            rows = tx.fetch_all(f"SELECT id FROM check_ins WHERE user_id = ? AND status = 'sent' AND slot_index IN ({marks})", (self._user, *slots))
        return sum(1 for r in rows if self._cas(r["id"], ("sent",), "skipped", now))

    def answer_duties(self, slots, now: datetime) -> int:
        """Open protocol duties in `slots` still before their deadline are answered."""
        marks = ",".join("?" for _ in slots)
        with self._core.transaction() as tx:
            rows = tx.fetch_all(f"SELECT id FROM check_ins WHERE user_id = ? AND status = 'sent' AND slot_index IN ({marks}) AND deadline_at >= ?",
                                (self._user, *slots, _iso(now)))
        return sum(1 for r in rows if self._cas(r["id"], ("sent",), "answered", now, event="check_in.answered", responded_at=_iso(now)))

    # ------------------------------------------------------------------ the morning report (owner, 2026-11-15)

    def _long_lock_extra(self, now: datetime) -> int:
        try:
            days = self.lock_days(now)
        except Exception:
            logger.exception("The lock's length could not be read; no extra photo check-ins.")
            return 0
        return 0 if days is None else (2 if days >= 21 else 1 if days >= 7 else 0)

    def _photo_held(self, now: datetime) -> bool:
        try:
            return bool(self.photo_suspended(now))
        except Exception:
            logger.exception("Could not read whether photo check-ins are suspended; they are (never a miss by accident).")
            return True

    def _morning_held(self, now: datetime) -> bool:
        try:
            return bool(self.morning_suspended(now)) or not self.morning_enabled()
        except Exception:
            logger.exception("Could not read whether the morning report is suspended; it is (never a miss by accident).")
            return True

    def _morning_base(self, day) -> datetime:
        return self._cfg.to_utc(day, MORNING_DUE[1] if is_non_working_day(self._cfg, day) else MORNING_DUE[0])

    def _morning_due(self, day) -> datetime:
        # Bug fix 2026-11-16: only a bot that came back BEFORE the normal time moves it (every restart used to push it 2 hours later).
        base = self._morning_base(day)
        if self.online_since is not None and self._cfg.local(self.online_since).date() == day and self.online_since < base:
            base = max(base, self.online_since + MORNING_AFTER_START)
        return base

    def _morning(self, now: datetime) -> None:
        """Plans today's report once quiet hours are over, moves its time when the bot came back late, and decides an unasked one at its time."""
        local = self._cfg.local(now)
        day = local.date()
        with self._core.transaction() as tx:
            rows = [dict(r) for r in tx.fetch_all("SELECT * FROM check_ins WHERE user_id = ? AND slot_index = ? AND status IN ('scheduled', 'sent')",
                                                  (self._user, MORNING_SLOT))]
            exists = tx.fetch_one("SELECT 1 FROM check_ins WHERE user_id = ? AND local_date = ? AND slot_index = ?",
                                  (self._user, day.isoformat(), MORNING_SLOT)) is not None
        for row in rows:
            due = _parse_iso(row["due_at"])
            later = self._morning_due(day) if row["local_date"] == day.isoformat() else due
            # Moved at most once (bug fix 2026-11-16): only a row still at its normal time, and its text says the new time.
            if later > due and now < due and row["local_date"] == day.isoformat() and due == self._morning_base(day):
                def move(tx: Transaction, _s: object, r=row, at=later) -> None:
                    text = MORNING_ASK.format(due=self._cfg.local(at).strftime("%H:%M")) if r["prompt_text"] else r["prompt_text"]
                    tx.execute("UPDATE check_ins SET due_at = ?, deadline_at = CASE WHEN status = 'sent' THEN ? ELSE deadline_at END, prompt_text = ?, "
                               "updated_at = ? WHERE id = ?", (_iso(at), _iso(at), text, _iso(now), r["id"]))
                apply_transition(self._core, write=move)
            elif row["status"] == "scheduled" and now >= due:
                if self._morning_held(now):
                    self._cas(row["id"], ("scheduled",), "skipped", now)
                else:
                    streak = self._miss_streak(row["id"], row["due_at"])
                    if self._cas(row["id"], ("scheduled",), "missed", now, event="check_in.missed", payload={"streak": streak}):
                        decision("check_ins", "Morning report missed: none by its time.", check_in_id=row["id"], streak=streak)
        if exists or classify(self._cfg, now) is WindowKind.QUIET or local.time() < self._cfg.quiet_end:
            return
        due = self._morning_due(day)
        if now >= due or self._morning_held(now):
            return
        level = self._level() or TrustLevel(4)
        prompt = MORNING_ASK.format(due=self._cfg.local(due).strftime("%H:%M"))

        def write(tx: Transaction, _s: object) -> None:
            tx.execute("INSERT OR IGNORE INTO check_ins (id, user_id, local_date, slot_index, level, due_at, status, prompt_text, created_at, updated_at) "
                       "VALUES (?, ?, ?, ?, ?, ?, 'scheduled', ?, ?, ?)",
                       (str(uuid.uuid4()), self._user, day.isoformat(), MORNING_SLOT, int(level), _iso(due), prompt, _iso(now), _iso(now)))

        apply_transition(self._core, write=write)

    def _ask_morning(self, now: datetime) -> bool:
        """His first message of the day: the question goes out, due at the report's time. True when it was asked now."""
        day = self._cfg.local(now).date().isoformat()
        if self._morning_held(now):
            return False

        def write(tx: Transaction, _s: object) -> bool:
            row = tx.fetch_one("SELECT * FROM check_ins WHERE user_id = ? AND local_date = ? AND slot_index = ? AND status = 'scheduled' AND due_at > ?",
                               (self._user, day, MORNING_SLOT, _iso(now)))
            if row is None:
                return False
            tx.execute("UPDATE check_ins SET status = 'sent', sent_at = ?, deadline_at = due_at, updated_at = ? WHERE id = ? AND status = 'scheduled'",
                       (_iso(now), _iso(now), row["id"]))
            write_event(tx, DomainEvent(event_type="check_in.prompt_due", source_module="supervision", occurred_at=now,
                                        payload={"check_in_id": row["id"], "trust_domain": self._domain}))
            enqueue_in_transaction(tx, user_id=self._user, kind="check_in", dedupe_key=f"check_in:morning:{row['id']}", now=now, immediate=True,
                                   text=row["prompt_text"])
            return True

        asked = apply_transition(self._core, write=write)
        if asked:
            decision("check_ins", "Morning report asked on his first message of the day.")
        return bool(asked)

    # ------------------------------------------------------------------ photo check-ins (supervision/photo_check_ins.py)

    @property
    def user_id(self) -> str:
        return self._user

    def _photos_on(self) -> bool:
        return self.photo_times is not None and self.photo_consent.is_on(self._user)

    def _schedule_photo(self, now: datetime) -> None:
        """Once per local day: whether today gets a photo check-in. The week's remaining number is spread over its remaining days, so a week
        ends with about PHOTO_CHECK_INS_PER_WEEK for the current level. A skipped one does not count, so it can come back on a later day."""
        if not self._photos_on():
            return
        level = self._level()
        if level is None:
            return
        local_day = self._cfg.local(now).date()
        day = local_day.isoformat()
        if self.photo_consent.drawn_on(self._user, day):
            return
        week_start = (local_day - timedelta(days=local_day.weekday())).isoformat()
        days_left = 7 - local_day.weekday()
        extra = self._long_lock_extra(now)          # read before the write: it opens its own transaction (live, 2026-11-17 00:00: nested)

        def write(tx: Transaction, _s: object) -> str | None:
            if tx.fetch_one("SELECT last_drawn_date FROM photo_check_in_consent WHERE user_id = ?", (self._user,))["last_drawn_date"] == day:
                return None
            done = tx.fetch_one("SELECT COUNT(*) AS n FROM check_ins WHERE user_id = ? AND kind = 'photo' AND local_date >= ? AND local_date < ? "
                                "AND status != 'skipped'", (self._user, week_start, day))["n"]
            remaining = PHOTO_CHECK_INS_PER_WEEK[level] + extra - int(done)
            due = None
            if remaining > 0 and self._rand() < remaining / days_left:
                for _ in range(10):
                    slots = slots_for_day(self._cfg, local_day, 2, self._rand)
                    if not slots:
                        break
                    candidate = slots[min(len(slots) - 1, int(self._rand() * len(slots)))]
                    if candidate > now + timedelta(minutes=1) and classify(self._cfg, candidate + PHOTO_RESPONSE_TIMEOUT) is WindowKind.DISCRETIONARY:
                        due = candidate
                        break
            if due is not None and not tx.fetch_one("SELECT 1 FROM check_ins WHERE user_id = ? AND local_date = ? AND slot_index = ?",
                                                    (self._user, day, PHOTO_SLOT)):
                tx.execute("INSERT INTO check_ins (id, user_id, local_date, slot_index, level, due_at, status, kind, created_at, updated_at) "
                           "VALUES (?, ?, ?, ?, ?, ?, 'scheduled', 'photo', ?, ?)",
                           (str(uuid.uuid4()), self._user, day, PHOTO_SLOT, int(level), _iso(due), _iso(now), _iso(now)))
            self.photo_consent.mark_drawn(tx, self._user, day, now)
            return _iso(due) if due is not None else "none"

        drawn = apply_transition(self._core, write=write)
        if drawn is not None:       # the decision log says that a day was drawn, never when the picture will be asked for
            decision("check_ins", "Photo check-in drawn for today." if drawn != "none" else "No photo check-in today.", level=int(level))

    def _send_photo(self, row, now: datetime) -> None:
        deadline = now + PHOTO_RESPONSE_TIMEOUT
        prompt = photo_prompt(self._cfg.local(deadline).strftime("%H:%M"))
        if not self._cas(row["id"], ("scheduled",), "sending", now, prompt_text=prompt, event="check_in.prompt_due"):
            return
        try:
            outcome = self._sender.send(user_id=self._user, text=prompt)
        except Exception:
            logger.exception("Photo check-in delivery raised; outcome unknown.")
            outcome = SendOutcome.UNKNOWN
        decision("check_ins", f"Photo check-in sent: {outcome.value}.", check_in_id=row["id"],
                 answer_by=_iso(deadline) if outcome is SendOutcome.DELIVERED else None)
        if outcome is SendOutcome.DELIVERED:
            self._cas(row["id"], ("sending",), "sent", now, sent_at=_iso(now), deadline_at=_iso(deadline))
        elif outcome is SendOutcome.UNDELIVERABLE:
            self._cas(row["id"], ("sending",), "undeliverable", now)
        else:
            self._cas(row["id"], ("sending",), "needs_review", now)

    def _read_photo_times(self, now: datetime) -> list[datetime] | None:
        if self.photo_times is None:
            return None
        try:
            return self.photo_times(now)
        except Exception:
            logger.exception("Reading the verification pictures failed; the photo check-in stays open.")
            return None

    def _check_photos(self, now: datetime) -> None:
        """An open photo check-in is answered by a picture submitted between the prompt and the deadline. Past the deadline with none it is
        missed; when the pictures cannot be read it is never a miss (skipped after PHOTO_UNKNOWN_LIMIT)."""
        with self._core.transaction() as tx:
            rows = tx.fetch_all("SELECT id, due_at, sent_at, deadline_at FROM check_ins WHERE user_id = ? AND kind = 'photo' AND status = 'sent'",
                                (self._user,))
        for row in rows:
            sent, deadline = _parse_iso(row["sent_at"]), _parse_iso(row["deadline_at"])
            past = now > deadline
            last = self._photo_checked.get(row["id"])
            if last is not None and now - last < PHOTO_POLL and not (past and last <= deadline):    # always one read right after the deadline
                continue
            times = self._read_photo_times(now)
            self._photo_checked[row["id"]] = now
            if times is not None:
                hits = sorted(t for t in times if sent - PHOTO_CLOCK_SKEW <= t <= deadline + PHOTO_CLOCK_SKEW)
                if hits:
                    if self._cas(row["id"], ("sent",), "answered", now, event="check_in.answered", responded_at=_iso(hits[0])):
                        decision("check_ins", "Photo check-in answered: a verification picture arrived in time.", check_in_id=row["id"])
                        self._photo_checked.pop(row["id"], None)
                        try:
                            self._sender.send(user_id=self._user, text=PHOTO_RECEIVED)
                        except Exception:
                            logger.exception("The photo check-in thanks could not be sent.")
                    continue
            if not past:
                continue
            if times is None:
                if now - deadline > PHOTO_UNKNOWN_LIMIT and self._cas(row["id"], ("sent",), "skipped", now):
                    decision("check_ins", "Photo check-in skipped: the pictures could not be read, so it is never a miss.", check_in_id=row["id"])
                    self._photo_checked.pop(row["id"], None)
                continue
            if self._photo_held(now):
                if self._cas(row["id"], ("sent",), "skipped", now):
                    decision("check_ins", "Photo check-in skipped: a wear exemption was in force, so it is never a miss.", check_in_id=row["id"])
                    self._photo_checked.pop(row["id"], None)
                continue
            streak = self._miss_streak(row["id"], row["due_at"])
            if self._cas(row["id"], ("sent",), "missed", now, event="check_in.missed", payload={"streak": streak}):
                decision("check_ins", "Photo check-in missed: no verification picture by the deadline.", check_in_id=row["id"], streak=streak)
                self._photo_checked.pop(row["id"], None)

    def _suffix(self, now: datetime) -> str:
        if self.prompt_suffix is None:
            return ""
        try:
            return self.prompt_suffix(now) or ""
        except Exception:
            logger.exception("The check-in addition failed; the plain question goes out.")
            return ""

    def _evaluate_responses(self, now: datetime) -> None:
        with self._core.transaction() as tx:
            rows = tx.fetch_all("SELECT * FROM check_ins WHERE user_id = ? AND status = 'responded'", (self._user,))
        for row in rows:
            if row["slot_index"] == MORNING_SLOT:     # owner, 2026-11-15: any content counts; code checks only that it arrived, no model
                self._cas(row["id"], ("responded",), "answered", now, event="check_in.answered")
                continue
            try:
                verdict = self._evaluate(row["prompt_text"] or "", row["response_text"] or "")
            except Exception:
                verdict = Verdict.NOT_EVALUATED
            common = dict(verdict=verdict.value, rubric_version=RUBRIC_VERSION)
            if row["slot_index"] == RELEASE_SLOT and verdict is not Verdict.SUFFICIENT:      # submitting is what counts: it cannot fail
                self._cas(row["id"], ("responded",), "answered_uncertain", now, **common)
            elif verdict is Verdict.SUFFICIENT:      # owner, 2026-11-13: a good answer earns Trust (check_in.answered); the release check-in never counts
                self._cas(row["id"], ("responded",), "answered", now, event=None if row["slot_index"] == RELEASE_SLOT else "check_in.answered", **common)
            elif verdict is Verdict.INSUFFICIENT:
                follow_up = row["follow_up_until"] or _iso(now + self._follow_up_window())
                # Bug fix 2026-11-16 (owner: "How do I realise my answer was too vague? Does it tell me?"): it did not, so the follow-up
                # window to add more was invisible. He is told at once, with the time left.
                minutes = max(1, int((_parse_iso(follow_up) - now).total_seconds() // 60))
                self._cas(row["id"], ("responded",), "insufficient_pending", now, follow_up_until=follow_up,
                          notice=(f"check_in:insufficient:{row['id']}",
                                  f"That answer is too thin for me to count it. Add a sentence or two within {minutes} minutes: "
                                  "what you actually did and how it's going. Otherwise it counts against you."), **common)
            else:     # UNCERTAIN / NOT_EVALUATED: zero consequence (fail closed towards no penalty)
                self._cas(row["id"], ("responded",), "answered_uncertain", now, **common)

    def _apply_deadlines(self, now: datetime) -> None:
        with self._core.transaction() as tx:
            missed = tx.fetch_all("SELECT id, due_at, slot_index FROM check_ins WHERE user_id = ? AND status = 'sent' AND kind IS NULL "
                                  "AND deadline_at < ?",
                                  (self._user, _iso(now)))
            insufficient = tx.fetch_all("SELECT id FROM check_ins WHERE user_id = ? AND status = 'insufficient_pending' "
                                        "AND follow_up_until < ?", (self._user, _iso(now)))
        for r in sorted(missed, key=lambda row: row["due_at"]):
            if r["slot_index"] == RELEASE_SLOT:           # ignoring a release offer costs nothing
                self._cas(r["id"], ("sent",), "skipped", now)
                continue
            if r["slot_index"] in (MORNING_SLOT, *DUTY_SLOTS) and self._morning_held(now):
                self._cas(r["id"], ("sent",), "skipped", now)
                continue
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

    def _recent_prompts(self) -> list[str]:
        with self._core.transaction() as tx:
            rows = tx.fetch_all("SELECT prompt_text FROM check_ins WHERE user_id = ? AND prompt_text IS NOT NULL AND kind IS NULL "
                                "AND slot_index < ? ORDER BY COALESCE(sent_at, updated_at) DESC LIMIT ?", (self._user, MORNING_SLOT, RECENT_PROMPTS))
        return [r["prompt_text"] for r in rows]

    def _cas(self, check_in_id: str, from_statuses: tuple[str, ...], to_status: str, now: datetime,
             event: str | None = None, payload: dict | None = None, notice: tuple[str, str] | None = None, **columns: str | None) -> bool:
        allowed = {"prompt_text", "sent_at", "deadline_at", "verdict", "rubric_version", "follow_up_until", "responded_at"}
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
            if notice is not None:
                enqueue_in_transaction(tx, user_id=self._user, kind="check_in", dedupe_key=notice[0], now=now, immediate=True, text=notice[1])
            return True

        return apply_transition(self._core, write=write)


def get_duty_misses(core: CoreDatabase, user_id: str, since: str) -> int:
    """Read-only (protocol ladder): missed morning reports and protocol duties since an ISO time."""
    with core.transaction() as tx:
        return int(tx.fetch_one("SELECT COUNT(*) AS n FROM check_ins WHERE user_id = ? AND status = 'missed' AND slot_index IN (90, 91, 92, 93, 94, 95) "
                                "AND due_at >= ?", (user_id, since))["n"])
