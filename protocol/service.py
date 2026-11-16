"""
protocol/service.py

The protocol ladder, no debate with brat lines, how the bot addresses him, and the wellbeing check (owner, 2026-11-15/16; roadmap 6b, 6c,
6d, P2; autonomous queue A2, A3, A4). See README.md.

Nothing here touches a lock, a token or Trust directly. A duty rung's miss is a missed check-in, written by supervision (`open_duty` /
`answer_duties`); this module only decides when a duty is owed and when it was done. Every check is code; no model decides anything here.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Callable

from infrastructure.database import Database as CoreDatabase, Transaction, apply_transition
from infrastructure.outbox import DomainEvent, write_event
from infrastructure.time_format import iso as _iso
from infrastructure.time_format import parse_iso as _parse_iso
from notifications.queue import enqueue_in_transaction

__all__ = ["ProtocolLadder", "Ports", "RUNGS", "HOLD_DAYS", "MAX_MISSES", "TOP_RUNG", "ADDRESS_ME", "ASK_PROPERLY", "RULES_STAND",
           "WINDOW_OVER", "CALM_CHECK", "is_pushback", "is_hard", "is_request", "get_protocol_state"]

logger = logging.getLogger("ai_coach_keyholder.protocol")

HOLD_DAYS = 14                      # a rung is held this long with at most MAX_MISSES misses before the next one is announced
MAX_MISSES = 1
ANNOUNCE_AHEAD = timedelta(days=1)  # announced a day ahead
THANKS_WITHIN = timedelta(hours=2)  # rung 3
TOP_RUNG = 6
KNEEL_BEFORE_QUIET = timedelta(minutes=10)   # rung 6: the kneel task goes on Chaster 10 minutes before quiet hours (5 minutes kneeling, then the photo)
KNEEL_DUE_AFTER = timedelta(minutes=65)      # ...and the photo is due this long after (the obedience photo deadline + the 5 minutes)
REFLECTION_MIN_WORDS = 10           # rung 5: any content of at least this many words counts (code counts words only)
REVIEW_AT = time(19, 0)             # the Sunday review's time (weekly_review)

RUNGS = {
    1: "Your morning report: your plan for the day, every morning.",
    2: "Ask properly: when you ask for a hygiene opening, an exemption or a release, say please and why.",
    3: "Thank me within 2 hours of every verified task. A missing thank-you counts as a missed check-in.",
    4: "Address me by my title at the start of every message. Commands and buttons don't need it.",
    5: "Every Sunday after the review, your weekly reflection: what you obeyed, where you pushed back, and one aim for the week. Due Monday "
       "12:00; none counts as a missed check-in.",
    6: "Every evening, 5 minutes before quiet hours, you kneel for 5 minutes and send a photo from behind in the Chaster task. None counts as "
       "a missed check-in; it's skipped when the task slot is taken, during an exemption or a vacation.",
}
TITLE_ASK = ("Choose the title you'll use for me: send `title Sir`, `title Keyholder`, `title Master`, `title Mistress`, or `title` and your "
             "own word.")
ADDRESS_ME = "Address me properly."
ASK_PROPERLY = "Ask properly: please, and why."
RULES_STAND = "The rules stand. Arguing changes nothing."
WINDOW_OVER = "Window's over. Good boy for getting through it."
CALM_CHECK = ("That sounds hard. Nothing you write here ever counts against you. If your body needs a break, ask for a health exemption and "
              "it's granted; if you need everything paused, `vacation` stops it all. Tell me what's going on.")
REFLECTION_ASK = ("Your weekly reflection: what you obeyed this week, where you pushed back, and one aim for next week. Due Monday 12:00.")

_PUSHBACK = re.compile(r"\b(unfair|why do i have to|why should i|i won'?t do (it|that|this)|that'?s stupid|this is stupid|you can'?t make me|not fair|"
                       r"nespravedliv\w*|proč musím|nebudu to dělat|to je blbost|to je hloupost)\b", re.I)
_HARD = re.compile(r"\b(bad|awful|terrible|struggling|depressed|anxious|hurts?|exhausted|burn(ed|t)? out|hopeless|"
                   r"špatně|nezvládám|smutn\w*|vyčerpan\w*|bolí)\b", re.I)
_REQUEST = re.compile(r"\b(unlock|open (the |my )?(lock|cage)|hygiene|exemption|release|let me cum|can i cum|odemkn\w*|uvoln\w*)\b", re.I)
_PLEASE = re.compile(r"\b(please|pls|prosím|prosim)\b", re.I)
_BYPASS = re.compile(r"\b(health\w*|safe\w*|emergenc\w*|hurt\w*|pain\w*|bleed\w*|numb|doctor|hospital|sick|ill|injur\w*|swollen|zdrav\w*|bolest\w*|"
                     r"nouz\w*|nemocn\w*|doktor\w*|krv\w*)\b", re.I)
_THANKS = re.compile(r"\b(thank(s| you)?|ty|děkuj\w*|dekuj\w*|díky|diky)\b", re.I)
_TITLE_CMD = re.compile(r"^\s*title\s+(.{2,30})\s*$", re.I)


def is_pushback(text: str) -> bool:
    return bool(_PUSHBACK.search(text or ""))


def is_hard(text: str) -> bool:
    return bool(_HARD.search(text or ""))


_ASKING = re.compile(r"\b(can|may|could) i\b|\bi (want|need|would like)\b|\blet me\b|\bmůžu\b|\bmohl bych\b|\bchci\b|\bpotřebuj\w*", re.I)


def is_request(text: str) -> bool:
    """A request for an opening, an exemption or a release (not a question about one)."""
    return bool(_REQUEST.search(text or "")) and bool(_ASKING.search(text or ""))


def _bypass(text: str) -> bool:
    return bool(_BYPASS.search(text or ""))


@dataclass
class Ports:
    local: Callable[[datetime], datetime]                       # UTC -> local
    to_utc: Callable[..., datetime]                             # (local date, local time) -> UTC
    quiet_end: time
    quiet_start: time
    paused: Callable[[datetime], bool]                          # vacation / halt / wear exemption: nothing new is owed
    open_lines: Callable[[], int]
    todays_task_open: Callable[[datetime], bool]                # today's task is in Chaster and not done
    window_left_hours: Callable[[datetime], float | None]       # an active Penalty Window's remaining hours, or None
    verified_since: Callable[[str], list[str]]                  # obedience task ids verified since an ISO time
    open_duty: Callable[..., str | None]                        # supervision: (slot, local_date, deadline, now) -> check-in id
    answer_duties: Callable[..., int]                           # supervision: (slots, now) -> how many were answered
    duty_misses: Callable[[str], int]                           # supervision: duty misses since an ISO time
    skip_duties: Callable[..., int] = lambda slots, now: 0      # supervision: (slots, now)
    plan_kneel: Callable[..., str | None] | None = None         # obedience: (local date, planned_at, now) -> task id
    kneel_status: Callable[[str], str | None] = lambda day: None   # obedience: today's kneel task status


THANKS_SLOTS = (91, 92, 93)
REFLECTION_SLOT = 94
KNEEL_SLOT = 95


def get_protocol_state(core: CoreDatabase, user_id: str) -> dict | None:
    """Read-only: the ladder row (rung, title, ...), or None before the ladder started."""
    with core.transaction() as tx:
        row = tx.fetch_one("SELECT * FROM protocol_ladder WHERE user_id = ?", (user_id,))
    return None if row is None else dict(row)


class ProtocolLadder:
    def __init__(self, core: CoreDatabase, *, user_id: str, ports: Ports) -> None:
        self._core, self.user_id, self._ports = core, user_id, ports

    # ------------------------------------------------------------------ state

    def state(self) -> dict | None:
        return get_protocol_state(self._core, self.user_id)

    def _dm(self, tx: Transaction, key: str, text: str, now: datetime) -> None:
        enqueue_in_transaction(tx, user_id=self.user_id, kind="protocol", dedupe_key=key, now=now, text=text, immediate=True)

    def _event(self, tx: Transaction, verb: str, now: datetime, **payload) -> None:
        write_event(tx, DomainEvent(event_type=f"protocol.{verb}", source_module="protocol", occurred_at=now, payload=payload))

    # ------------------------------------------------------------------ the tick

    def tick(self, now: datetime) -> None:
        for step in (self._start, self._climb, self._thanks, self._reflection, self._kneel, self._window):
            try:
                step(now)
            except Exception:
                logger.exception("A protocol pass failed (%s); it continues.", step.__name__)

    def _start(self, now: datetime) -> None:
        def write(tx: Transaction, _s: object) -> None:
            if tx.execute("INSERT OR IGNORE INTO protocol_ladder (user_id, rung, rung_since, created_at) VALUES (?, 1, ?, ?)",
                          (self.user_id, _iso(now), _iso(now))).rowcount:
                self._event(tx, "started", now, rung=1)

        apply_transition(self._core, write=write)

    def _climb(self, now: datetime) -> None:
        s = self.state()
        if s is None:
            return
        if s["next_at"] is not None:
            if now >= _parse_iso(s["next_at"]):
                rung = int(s["rung"]) + 1

                def up(tx: Transaction, _s: object) -> None:
                    if tx.execute("UPDATE protocol_ladder SET rung = ?, rung_since = ?, next_at = NULL WHERE user_id = ? AND rung = ? "
                                  "AND next_at IS NOT NULL", (rung, _iso(now), self.user_id, rung - 1)).rowcount:
                        self._event(tx, "rung_started", now, rung=rung)
                        self._dm(tx, f"protocol:rung:{rung}", f"New protocol rule, from now on: {RUNGS[rung]}" +
                                 (f" {TITLE_ASK}" if rung == 4 else ""), now)

                apply_transition(self._core, write=up)
            return
        rung = int(s["rung"])
        if rung >= TOP_RUNG or self._ports.paused(now):
            return
        since = _parse_iso(s["rung_since"])
        if now - since < timedelta(days=HOLD_DAYS):
            return
        if self._ports.duty_misses(_iso(max(since, now - timedelta(days=HOLD_DAYS)))) > MAX_MISSES:
            return
        nxt = now + ANNOUNCE_AHEAD

        def announce(tx: Transaction, _s: object) -> None:
            if tx.execute("UPDATE protocol_ladder SET next_at = ? WHERE user_id = ? AND next_at IS NULL AND rung = ?",
                          (_iso(nxt), self.user_id, rung)).rowcount:
                self._event(tx, "rung_announced", now, rung=rung + 1)
                self._dm(tx, f"protocol:announce:{rung + 1}", f"You held the protocol for {HOLD_DAYS} days. Tomorrow a new rule starts: "
                         f"{RUNGS[rung + 1]}", now)

        apply_transition(self._core, write=announce)

    def _thanks(self, now: datetime) -> None:
        """Rung 3: every verified task opens a thank-you duty due in THANKS_WITHIN."""
        s = self.state()
        if s is None or int(s["rung"]) < 3:
            return
        since = s["thanks_scan"] or s["rung_since"]
        ids = self._ports.verified_since(since)
        if not self._ports.paused(now):
            day = self._ports.local(now).date().isoformat()
            for _ in ids:
                for slot in THANKS_SLOTS:
                    if self._ports.open_duty(slot, day, now + THANKS_WITHIN, now) is not None:
                        break

        def mark(tx: Transaction, _s: object) -> None:
            tx.execute("UPDATE protocol_ladder SET thanks_scan = ? WHERE user_id = ?", (_iso(now), self.user_id))

        apply_transition(self._core, write=mark)

    def _reflection(self, now: datetime) -> None:
        """Rung 5: on Sunday after the review, the weekly reflection is owed by Monday 12:00."""
        s = self.state()
        local = self._ports.local(now)
        if s is None or int(s["rung"]) < 5 or local.weekday() != 6 or local.time() < REVIEW_AT or self._ports.paused(now):
            return
        deadline = self._ports.to_utc(local.date() + timedelta(days=1), time(12, 0))
        if self._ports.open_duty(REFLECTION_SLOT, local.date().isoformat(), deadline, now) is not None:
            def dm(tx: Transaction, _s: object) -> None:
                self._dm(tx, f"protocol:reflection:{local.date().isoformat()}", REFLECTION_ASK, now)

            apply_transition(self._core, write=dm)

    def _kneel(self, now: datetime) -> None:
        """Rung 6: plans the evening kneel (a Chaster task) and its duty; a submitted photo answers the duty, a skipped task skips it."""
        s = self.state()
        if s is None or int(s["rung"]) < 6 or self._ports.plan_kneel is None:
            return
        local = self._ports.local(now)
        day = local.date()
        status = self._ports.kneel_status(day.isoformat())
        if status in ("submitted", "passed"):
            self._ports.answer_duties((KNEEL_SLOT,), now)
            return
        if status in ("skipped", "cancelled"):
            self._ports.skip_duties((KNEEL_SLOT,), now)
            return
        if status is not None or self._ports.paused(now):
            return
        at = self._ports.to_utc(day, self._ports.quiet_start) - KNEEL_BEFORE_QUIET
        if not (at - timedelta(minutes=5) <= now < at + timedelta(minutes=5)):
            return
        if self._ports.plan_kneel(day, at, now) is not None:
            self._ports.open_duty(KNEEL_SLOT, day.isoformat(), at + KNEEL_DUE_AFTER, now)

    def _window(self, now: datetime) -> None:
        """How he is addressed: the first DM after a Penalty Window ends brings the praise back."""
        s = self.state()
        if s is None:
            return
        active = self._ports.window_left_hours(now) is not None
        if active == bool(s["window_seen"]):
            return

        def write(tx: Transaction, _s: object) -> None:
            if tx.execute("UPDATE protocol_ladder SET window_seen = ? WHERE user_id = ? AND window_seen = ?",
                          (1 if active else 0, self.user_id, 0 if active else 1)).rowcount and not active:
                self._dm(tx, f"protocol:window_over:{_iso(now)[:16]}", WINDOW_OVER, now)
                self._event(tx, "window_over_praised", now)

        apply_transition(self._core, write=write)

    # ------------------------------------------------------------------ the message gate (before the model)

    def gate(self, user_id: str, text: str, now: datetime) -> str | None:
        """A fixed reply that ends the turn, or None (the message goes on as usual). Health, safety and emergencies always pass."""
        if user_id != self.user_id or not isinstance(text, str) or not text.strip():
            return None
        text = text.strip()
        try:
            return self._gate(text, now)
        except Exception:
            logger.exception("The protocol gate failed; the message goes on as usual.")
            return None

    def _gate(self, text: str, now: datetime) -> str | None:
        s = self.state()
        rung = int(s["rung"]) if s else 0
        title_cmd = _TITLE_CMD.match(text)
        if title_cmd and rung >= 4:
            return self._set_title(title_cmd.group(1).strip().strip(".!").title(), now)
        if _bypass(text):
            return None
        if self._ports_quiet_review(now) and is_hard(text):
            return CALM_CHECK
        title = (s or {}).get("title")
        if rung >= 4 and title and not re.match(rf"^\W*{re.escape(title)}\b", text, re.I):
            return ADDRESS_ME
        if rung >= 3 and _THANKS.search(text):
            self._ports.answer_duties(THANKS_SLOTS, now)
        if rung >= 5 and len(text.split()) >= REFLECTION_MIN_WORDS and self._ports.answer_duties((REFLECTION_SLOT,), now):
            return "Reflection received."
        if rung >= 2 and is_request(text) and not _PLEASE.search(text):
            return ASK_PROPERLY
        if is_pushback(text):
            return self._rules_stand(now)
        return None

    def _ports_quiet_review(self, now: datetime) -> bool:
        """The 24 hours after the Sunday review (the wellbeing question)."""
        local = self._ports.local(now)
        days_back = (local.weekday() - 6) % 7
        review = local.replace(hour=REVIEW_AT.hour, minute=0, second=0, microsecond=0) - timedelta(days=days_back)
        if review > local:
            review -= timedelta(days=7)
        return local - review < timedelta(hours=24)

    def _rules_stand(self, now: datetime) -> str:
        """6b: one short reply restating the open item and that arguing changes nothing; the model never sees the turn."""
        try:
            left = self._ports.window_left_hours(now)
            if left is not None:
                item = f"your Penalty Window, about {max(1, round(left))} hours left"
            elif self._ports.open_lines() > 0:
                item = f"the {self._ports.open_lines()} lines you owe"
            elif self._ports.todays_task_open(now):
                item = "today's task"
            else:
                item = None
        except Exception:
            logger.exception("The open item could not be read; the general reply goes out.")
            item = None

        def write(tx: Transaction, _s: object) -> None:
            self._event(tx, "pushback_answered", now)

        apply_transition(self._core, write=write)
        return f"The rule stands: {item}. Arguing changes nothing." if item else RULES_STAND

    def _set_title(self, title: str, now: datetime) -> str:
        def write(tx: Transaction, _s: object) -> None:
            tx.execute("UPDATE protocol_ladder SET title = ? WHERE user_id = ?", (title, self.user_id))
            self._event(tx, "title_set", now)

        apply_transition(self._core, write=write)
        return f"From now on, start every message with \"{title}\"."
