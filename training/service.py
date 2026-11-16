"""
training/service.py

His training plan in the bot (owner, 2026-11-15; work queue Q4c; migration 071). The plan (training/plan.py) decides the day's session, and the
session becomes that day's obedience task (obedience/service.py asks `day_task`):

- Saturday: full leg day, A and B alternating (whichever he did not do last).  Sunday: full upper body.
- One short session on Monday or Thursday: legs in an odd ISO week, upper body in an even one. On Monday morning the bot asks which day;
  no answer means Thursday. Never legs on Friday or Sunday (the schedule never puts them there).
- Full sessions are mandatory (a missed one goes the obedience way: asked to explain, unexplained = a Warning and 20 lines). The short
  session is optional ("skip it and continue at the weekend"): missing it costs nothing.
- Weights: his current weight per exercise (`training_weights`, the plan's start weight until he logs). The first two leg days run the
  plan's return-from-break rule (squat 95 kg, the rest -10 %, one set fewer); every 7th week is a deload week (half the sets).
- `log squat 105 4x9` or `log squat 105 10,10,9,10` (pull-ups and other body-weight work: `log pullups 8,8,7`): when every set reaches the
  top of the range, the next session gets +step (2.5 kg on the bar, one plate on the machines).

Proof: a Chaster photo of the gym after the session (owner, 2026-11-15; Chaster's code shown as usual). Nothing here changes lock time or acts on the lock.
"""

from __future__ import annotations

import logging
import math
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable

from application.models import ActionSpec, OutgoingMessage
from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.decision_log import decision
from infrastructure.outbox import DomainEvent, write_event
from infrastructure.time_format import iso as _iso
from notifications.queue import enqueue_in_transaction
from training.plan import ALIASES, EXERCISES, SESSIONS, WARM_UP, Exercise

logger = logging.getLogger("ai_coach_keyholder.training")

__all__ = ["TrainingPlan", "TrainingDay", "DELOAD_EVERY_WEEKS", "RETURN_LEG_DAYS"]

DELOAD_EVERY_WEEKS = 7
RETURN_LEG_DAYS = 2
EXTRA_WORDS = frozenset({"extra training", "extra workout", "extra session", "extra training session", "fourth session",
                         "trénink navíc", "trenink navic", "extra trénink", "extra trenink"})
_LOG = re.compile(r"^\s*log\s+(?P<name>[^\d]+?)\s+(?:(?P<weight>\d+(?:[.,]\d+)?)\s*(?:kg)?\s+)?(?P<reps>\d+\s*[x×]\s*\d+|\d+(?:\s*[,;/ ]\s*\d+)*)\s*$",
                  re.IGNORECASE)


@dataclass(frozen=True)
class TrainingDay:
    key: str                # "training:legs_a" ... ("training:short:..." when optional)
    text: str               # the Chaster task (proof)
    details: str            # the session in the DM
    optional: bool


def get_planned_sessions(core, user_id: str, start: str, end: str) -> dict[str, str]:
    """Read-only: local date -> session key of the sessions the plan gave from `start` to `end` (inclusive local dates)."""
    with core.transaction() as tx:
        rows = tx.fetch_all("SELECT local_date, session_key FROM training_sessions WHERE user_id = ? AND local_date BETWEEN ? AND ? "
                            "AND local_date NOT IN (SELECT extra_date FROM training_weeks WHERE user_id = ? AND extra_date IS NOT NULL)",
                            (user_id, start, end, user_id))
    return {r["local_date"]: r["session_key"] for r in rows}


def get_raises(core, user_id: str, lo: str, hi: str) -> int:
    """Read-only: how many exercises earned a weight increase between the UTC instants `lo` and `hi` (owner, 2026-11-15: getting stronger
    pays). One per exercise, and only at a weight no lower than any earlier log of that exercise (a lighter weight with top reps is not
    progress)."""
    with core.transaction() as tx:
        row = tx.fetch_one("SELECT COUNT(DISTINCT l.exercise) AS n FROM training_logs l WHERE l.user_id = ? AND l.raised_to IS NOT NULL "
                           "AND l.logged_at >= ? AND l.logged_at < ? AND NOT EXISTS (SELECT 1 FROM training_logs e WHERE e.user_id = l.user_id "
                           "AND e.exercise = l.exercise AND e.logged_at < l.logged_at AND e.weight > l.weight)", (user_id, lo, hi))
    return int(row["n"])


class TrainingPlan:
    def __init__(self, core: CoreDatabase, *, user_id: str, free_time: Callable[[datetime], bool], today: Callable[[datetime], date]) -> None:
        self._core = core
        self.user_id = user_id
        self._free_time = free_time
        self.after_today: Callable[[datetime], object] | None = None   # obedience: switch today's planned task to the short session
        self.today_open: Callable[[datetime], bool] | None = None      # obedience: today's task is not out yet (none, or only planned)
        self._today = today

    # ------------------------------------------------------------------ the day's session (asked by the obedience planner)

    def day_task(self, day: date) -> TrainingDay | None:
        session = self._session_for(day)
        if session is None:
            return None
        optional = session.endswith("_short")
        self._record(day, session)
        title, keys = SESSIONS[session]
        deload = self._deload_week(day)
        returning = session.startswith("legs") and self._leg_days_before(day) < RETURN_LEG_DAYS
        lines = [f"**{title}**" + (" (deload week: half the sets)" if deload else "") + (" (back after a break: lighter)" if returning else "")]
        if session in WARM_UP:
            lines.append(WARM_UP[session])
        for key in keys:
            lines.append("- " + self._line(EXERCISES[key], deload=deload, returning=returning))
        lines.append("Keep 1-2 reps in reserve on squats, deadlifts and bench; isolation work close to failure. "
                     "Log your sets here: `log squat 105 4x9`.")
        if optional:
            lines.append("This short session is optional: if it doesn't fit today, skip it and go again at the weekend.")
        name = title.split(":")[0]
        return TrainingDay(key=f"training:{'short:' if optional else ''}{session}",
                           text=f"Workout done: {name}. Photo of the gym only.",           # owner, 2026-11-15: voters see this; brief, no code mention
                           details="\n".join(lines), optional=optional)

    def is_training_day_now(self, now: datetime) -> bool:
        """Read-only: today (local) has a session (earn the release: the session is then the release task)."""
        return self.is_training_day(self._today(now))

    def is_training_day(self, day: date) -> bool:
        """Read-only: the plan has a session that day (owner, 2026-11-15: the coach then proposes no separate workout)."""
        return self._session_for(day) is not None

    def _session_for(self, day: date) -> str | None:
        weekday = day.weekday()
        if weekday == 5:
            return self._next_legs(day)
        if weekday == 6:
            return "upper"
        if weekday in (0, 3) and self._short_day(day) == ("mon" if weekday == 0 else "thu"):
            legs_week = day.isocalendar()[1] % 2 == 1
            return f"{self._next_legs(day)}_short" if legs_week else "upper_short"
        kind = self._moved_on(day) or self._extra_on(day)
        if kind is not None:
            return f"{self._next_legs(day)}_short" if kind == "legs" else "upper_short"
        return None

    # ------------------------------------------------------------------ an extra session on demand

    def _moved_on(self, day: date) -> str | None:
        """A short session moved to this day (owner, 2026-11-16). It stays one of the week's planned sessions (week bonus as before)."""
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT moved_kind FROM training_weeks WHERE user_id = ? AND moved_date = ?", (self.user_id, day.isoformat()))
        return row["moved_kind"] if row is not None else None

    def _extra_on(self, day: date) -> str | None:
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT extra_kind FROM training_weeks WHERE user_id = ? AND extra_date = ?", (self.user_id, day.isoformat()))
        return row["extra_kind"] if row is not None else None

    def extra(self, user_id: str, text: str, now: datetime) -> OutgoingMessage | None:
        """`extra training` (owner, 2026-11-16: "I've already done the 3 this week but I have time today, so I can do the fourth"): one more
        short session today, at most one a week. It balances the week as in his workout chat's four-a-week version (legs when the week's
        short session is upper body, and the other way round), but never legs within 2 days of another leg day, on a Friday (Saturday is
        the full leg day) or on a Sunday. Optional like every short session: it earns the short-session bonus when verified, skipping it
        costs nothing, and it is never part of the week bonus. None when the message is not this request."""
        if user_id != self.user_id or " ".join((text or "").lower().split()).strip(" .!?") not in EXTRA_WORDS:
            return None
        day = self._today(now)
        if self._session_for(day) is not None:
            return OutgoingMessage(text="You already have a session today. Ask for an extra one on a day without one.")
        if self.today_open is not None and not self.today_open(now):
            return OutgoingMessage(text="Today's task is already out, so the extra session can't take its place. Ask earlier in the day next time.")
        week = f"{day.isocalendar()[0]}-W{day.isocalendar()[1]:02d}"
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT extra_date FROM training_weeks WHERE user_id = ? AND iso_week = ?", (self.user_id, week))
            last_legs = tx.fetch_one("SELECT MAX(local_date) AS d FROM training_sessions WHERE user_id = ? AND local_date < ? "
                                     "AND session_key LIKE 'legs_%'", (self.user_id, day.isoformat()))["d"]
        if row is not None and row["extra_date"]:
            return OutgoingMessage(text="You've had your extra session this week already. One a week.")
        legs_ok = day.weekday() not in (4, 6) and (last_legs is None or (day - date.fromisoformat(last_legs)).days >= 2)
        kind = "upper" if day.isocalendar()[1] % 2 == 1 else "legs"        # the other kind than the week's short session
        if kind == "legs" and not legs_ok:
            kind = "upper"

        def write(tx: Transaction, _s: object) -> bool:
            tx.execute("INSERT OR IGNORE INTO training_weeks (id, user_id, iso_week, short_day, asked_at) VALUES (?, ?, ?, NULL, NULL)",
                       (str(uuid.uuid4()), self.user_id, week))
            done = tx.execute("UPDATE training_weeks SET extra_date = ?, extra_kind = ? WHERE user_id = ? AND iso_week = ? AND extra_date IS NULL",
                              (day.isoformat(), kind, self.user_id, week)).rowcount
            if done:
                write_event(tx, DomainEvent(event_type="training.extra_requested", source_module="training", occurred_at=now,
                                            payload={"local_date": day.isoformat(), "kind": kind}))
            return bool(done)

        if not apply_transition(self._core, write=write):
            return OutgoingMessage(text="You've had your extra session this week already. One a week.")
        decision("training", "An extra short session was asked for today.", kind=kind)
        if self.after_today is not None:
            try:
                self.after_today(now)
            except Exception:
                logger.exception("Today's task could not be switched to the extra session.")
        what = "legs" if kind == "legs" else "upper body"
        return OutgoingMessage(text=f"Good. An extra short session today: {what}. It comes as today's task with the details. "
                                    "Verified in Chaster, it earns like any short session.")

    def _next_legs(self, day: date) -> str:
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT session_key FROM training_sessions WHERE user_id = ? AND local_date < ? AND session_key LIKE 'legs_%' "
                               "ORDER BY local_date DESC LIMIT 1", (self.user_id, day.isoformat()))
        return "legs_b" if row is not None and row["session_key"].startswith("legs_a") else "legs_a"

    def _short_day(self, day: date) -> str:
        week = f"{day.isocalendar()[0]}-W{day.isocalendar()[1]:02d}"
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT short_day FROM training_weeks WHERE user_id = ? AND iso_week = ?", (self.user_id, week))
        return (row["short_day"] if row is not None and row["short_day"] else "thu")

    def _record(self, day: date, session: str) -> None:
        def write(tx: Transaction, _s: object) -> None:
            if tx.execute("INSERT OR IGNORE INTO training_sessions (user_id, local_date, session_key, created_at) VALUES (?, ?, ?, ?)",
                          (self.user_id, day.isoformat(), session, _iso(datetime.combine(day, datetime.min.time())))).rowcount:
                write_event(tx, DomainEvent(event_type="training.session_planned", source_module="training",
                                            occurred_at=datetime.combine(day, datetime.min.time()),
                                            payload={"local_date": day.isoformat(), "session": session}))

        apply_transition(self._core, write=write)

    def _deload_week(self, day: date) -> bool:
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT MIN(local_date) AS first FROM training_sessions WHERE user_id = ?", (self.user_id,))
        if row is None or row["first"] is None:
            return False
        weeks = (day - date.fromisoformat(row["first"])).days // 7
        return weeks % DELOAD_EVERY_WEEKS == DELOAD_EVERY_WEEKS - 1

    def _leg_days_before(self, day: date) -> int:
        with self._core.transaction() as tx:
            return tx.fetch_one("SELECT COUNT(*) AS n FROM training_sessions WHERE user_id = ? AND local_date < ? AND session_key LIKE 'legs_%'",
                                (self.user_id, day.isoformat()))["n"]

    def weight(self, key: str) -> float:
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT weight FROM training_weights WHERE user_id = ? AND exercise = ?", (self.user_id, key))
        return float(row["weight"]) if row is not None else float(EXERCISES[key].start)

    def _line(self, ex: Exercise, *, deload: bool, returning: bool) -> str:
        sets = ex.sets
        load = self.weight(ex.key)
        if returning:
            sets -= 1
            load = 95.0 if ex.key == "squat" else (math.floor(load * 0.9 / ex.step) * ex.step if ex.step and ex.unit in ("kg", "kg/hand") else load)
        if deload:
            sets = max(1, math.ceil(sets / 2))
        what = {"kg": f"{load:g} kg", "kg/hand": f"{load:g} kg per hand", "plates": f"{load:g} large plates", "bw": "body weight"}[ex.unit]
        note = f" ({ex.note})" if ex.note else ""
        return f"{ex.name}: {sets} x {ex.low}-{ex.high}, {what}, rest {ex.rest}{note}"

    # ------------------------------------------------------------------ Monday: which day is the short session?

    def tick(self, now: datetime) -> None:
        day = self._today(now)
        if day.weekday() != 0 or not self._free_time(now):
            return
        week = f"{day.isocalendar()[0]}-W{day.isocalendar()[1]:02d}"
        row_id = str(uuid.uuid4())
        kind = "legs" if day.isocalendar()[1] % 2 == 1 else "upper body"

        def write(tx: Transaction, _s: object) -> bool:
            if not tx.execute("INSERT OR IGNORE INTO training_weeks (id, user_id, iso_week, short_day, asked_at) VALUES (?, ?, ?, NULL, ?)",
                              (row_id, self.user_id, week, _iso(now))).rowcount:
                return False
            enqueue_in_transaction(tx, user_id=self.user_id, kind="training", dedupe_key=f"training:week:{week}", now=now,
                                   text=(f"This week's short session is {kind}. Today or Thursday? No answer means Thursday. "
                                         "Time for one more this week? Write `extra training` on that day."),
                                   actions=(ActionSpec(action_id=f"training:mon:{row_id}", label="Today"),
                                            ActionSpec(action_id=f"training:thu:{row_id}", label="Thursday")))
            return True

        if apply_transition(self._core, write=write):
            decision("training", "Asked which day this week's short session is.", week=week)

    def press(self, *, user_id: str, row_id: str, action: str, now: datetime) -> str:
        if user_id != self.user_id or action not in ("mon", "thu"):
            return "That isn't one of the options."

        def write(tx: Transaction, _s: object) -> bool:
            done = tx.execute("UPDATE training_weeks SET short_day = ? WHERE id = ? AND user_id = ? AND short_day IS NULL",
                              (action, row_id, user_id)).rowcount
            if done:
                write_event(tx, DomainEvent(event_type="training.short_day_chosen", source_module="training", occurred_at=now,
                                            payload={"short_day": action}))
            return bool(done)

        if not apply_transition(self._core, write=write):
            return "That's already decided for this week."
        if action == "mon" and self.after_today is not None:
            try:                    # bug fix 2026-11-16: today's task was already planned (an erotic one) when he chose "Today"
                self.after_today(now)
            except Exception:
                logger.exception("Today's task could not be switched to the short session.")
        return "Today, then: it comes in your free time." if action == "mon" else "Thursday, then."

    # ------------------------------------------------------------------ logging sets

    def log(self, user_id: str, text: str, now: datetime) -> OutgoingMessage | None:
        """`log <exercise> [<kg>] <sets>x<reps> | <r1>,<r2>,...`. None when the message is not a log line."""
        if user_id != self.user_id or not text or not text.strip().lower().startswith("log "):
            return None
        match = _LOG.match(text.strip())
        name = match.group("name").strip().lower() if match else ""
        # Bug fix 2026-11-16: `log cable_row` (the key itself, with an underscore) was not understood.
        key = ALIASES.get(name) or ALIASES.get(name.replace("_", " ")) or (name if name in EXERCISES else None)
        if match is None or key is None:
            return OutgoingMessage(text="I couldn't read that. Write it like `log squat 105 4x9` or `log pullups 8,8,7`.")
        ex = EXERCISES[key]
        raw = match.group("reps").lower().replace("×", "x")
        if "x" in raw:
            sets, reps = (int(p) for p in raw.split("x"))
            all_reps = [reps] * sets
        else:
            all_reps = [int(p) for p in re.split(r"[,;/ ]+", raw) if p]
        weight = float(match.group("weight").replace(",", ".")) if match.group("weight") else None
        if ex.unit != "bw" and weight is None:
            return OutgoingMessage(text=f"How much weight? Write it like `log {name} {self.weight(key):g} 4x9`.")
        all_top = bool(all_reps) and all(r >= ex.high for r in all_reps)
        raised = (weight + ex.step) if all_top and ex.step and weight is not None else None

        def write(tx: Transaction, _s: object) -> None:
            tx.execute("INSERT INTO training_logs (id, user_id, logged_at, exercise, weight, reps, all_top, raised_to) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       (str(uuid.uuid4()), user_id, _iso(now), key, weight, ",".join(map(str, all_reps)), 1 if all_top else 0, raised))
            if weight is not None:
                tx.execute("INSERT INTO training_weights (user_id, exercise, weight, updated_at) VALUES (?, ?, ?, ?) "
                           "ON CONFLICT(user_id, exercise) DO UPDATE SET weight = excluded.weight, updated_at = excluded.updated_at",
                           (user_id, key, raised if raised is not None else weight, _iso(now)))
            write_event(tx, DomainEvent(event_type="training.sets_logged", source_module="training", occurred_at=now,
                                        payload={"exercise": key, "weight": weight, "reps": all_reps, "raised_to": raised}))

        apply_transition(self._core, write=write)
        unit = " per hand" if ex.unit == "kg/hand" else " plates" if ex.unit == "plates" else " kg"
        if raised is not None:
            return OutgoingMessage(text=f"Logged: {ex.name}. Every set at the top of the range: next time {raised:g}{unit}.")
        if all_top and ex.unit == "bw":
            return OutgoingMessage(text=f"Logged: {ex.name}. Every set at the top of the range: make it harder next time (slower, or the vest).")
        return OutgoingMessage(text=f"Logged: {ex.name}. Stay at this weight until every set reaches {ex.high}.")
