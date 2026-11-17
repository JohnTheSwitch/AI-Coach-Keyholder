"""
task_board/service.py

P1, the task board (owner, 2026-11-15, from "I Bought a Smart Chastity Cage": chores posted with a deadline and a reward). Every Monday, at the
first moment of free time, the DM shows OFFER_COUNT tasks for the week, each with a deadline and a reward in token progress. He picks:

    offered --"Take it"--> taken --"Doing it now"--> started (an obedience task in Chaster: photo, community vote, PHOTO_DEADLINE)
       |                     |                          verified -> passed (the reward joins that day's token pool)
       v                     v                          rejected / missed -> failed (the obedience path asks him; unexplained = Warning)
    expired (no trace)     deadline passed without a start -> missed (asked to explain; unexplained = Warning, OBEDIENCE_TASK_MISSED)

One taken task at a time; the rewards of a week's taken tasks are capped at WEEK_CAP. A task he never takes simply expires. Only gear he owns.
Health or safety cancels without consequence (the obedience task's own button), as everywhere.

Table `board_tasks` (migration 079), written only here. Events: `task_board.offered`, `task_board.taken`, `task_board.decided`.
The Chaster part is the obedience module's (slot 2 of the day, `obedience.service.plan_board_task`); this module never calls Chaster.
"""

from __future__ import annotations

import logging
import random
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Callable

from application.models import ActionSpec
from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.decision_log import decision
from infrastructure.outbox import DomainEvent, write_event
from infrastructure.time_format import iso as _iso
from infrastructure.time_format import parse_iso as _parse_iso
from notifications.queue import enqueue_in_transaction

logger = logging.getLogger("ai_coach_keyholder.task_board")

__all__ = ["TaskBoard", "BoardTask", "BOARD", "GENERIC_BOARD", "OFFER_COUNT", "WEEK_CAP", "EROTIC_SHARE", "erotic_reward", "get_board_rewards"]

OFFER_COUNT = 3
WEEK_CAP = Decimal("0.10")
EROTIC_SHARE = 0.6           # how often one of the week's three is an erotic task (never more than one)


@dataclass(frozen=True)
class BoardTask:
    key: str
    text: str            # what the Chaster voters see: brief, the photo names what it shows and only that
    reward: Decimal
    days: int            # deadline: this many days after the board is posted (at the end of that day's free time)
    kind: str            # chore | fitness | discipline | erotic
    needs: frozenset = frozenset()


def _t(key, text, reward, days, kind, *needs) -> BoardTask:
    return BoardTask(key, text, Decimal(reward), days, kind, frozenset(needs))


# Owner, 2026-11-15: the approved list (no study tasks: he doesn't study). Erotic board tasks come from the obedience catalogue (`erotic_pick`).
BOARD: tuple[BoardTask, ...] = (
    _t("sink", "Bathroom sink and mirror, cleaned. Photo of the sink only.", "0.03", 3, "chore"),
    _t("desk", "Desk cleared and tidy. Photo of the desk only.", "0.03", 3, "chore"),
    _t("laundry", "Laundry folded and put away. Photo of the folded stack only.", "0.03", 4, "chore"),
    _t("vacuum", "Room vacuumed. Photo of the floor only.", "0.03", 4, "chore"),
    _t("sheets", "Bed sheets changed, bed made neatly. Photo of the bed only.", "0.03", 4, "chore"),
    _t("fridge", "Fridge cleaned out and wiped. Photo of the open fridge only.", "0.04", 4, "chore"),
    _t("shower", "Shower scrubbed clean. Photo of the shower only.", "0.04", 4, "chore"),
    _t("windows", "Windows in your room cleaned. Photo of the window only.", "0.04", 4, "chore"),
    _t("wardrobe", "Wardrobe sorted. Photo of the open wardrobe only.", "0.04", 4, "chore"),
    _t("gym_tidy", "Gym corner tidied, weights put back. Photo of the gym only.", "0.03", 3, "chore"),
    _t("meal_prep", "3 healthy portions prepared for the next days. Photo of the boxes only.", "0.04", 4, "chore"),
    _t("steps", "10,000 steps today. Screenshot of the step count only.", "0.04", 4, "fitness"),
    _t("cardio", "20 minutes on the air bike or treadmill. Photo of the display only.", "0.04", 3, "fitness"),
    _t("intervals", "10 rounds of 20 s hard / 40 s easy on the air bike. Photo of the display only.", "0.04", 3, "fitness"),
    _t("incline", "20 minutes of incline walking. Photo of the treadmill display only.", "0.04", 3, "fitness"),
    _t("pushups100", "100 push-ups today, any sets. Photo of the handles only.", "0.04", 4, "fitness"),
    _t("plank", "A 3-minute plank, cage visible. Photo from the side, neck down only.", "0.04", 3, "fitness"),
    _t("pushups_diver", "50 push-ups with the Deep Diver in. Photo from the side, neck down only.", "0.05", 3, "fitness", "deep_diver"),
    _t("calves", "4 sets of calf raises with the bar. Photo of the bar and your calves only.", "0.03", 3, "fitness"),
    _t("rows", "4 sets of cable rows. Photo of the stack and the handle only.", "0.03", 3, "fitness"),
    _t("stretch", "15 minutes of stretching. Photo of the mat only.", "0.03", 3, "fitness"),
    _t("lines30", "30 handwritten lines: \"I obey the rules my keyholder sets for me.\" Photo of the sheet only.", "0.04", 3, "discipline"),
    _t("kneel20", "20 minutes kneeling, hands behind your back. Photo from behind, waist down only.", "0.04", 3, "discipline"),
    _t("no_social", "No social media for a whole day. Screenshot of the screen time only.", "0.04", 4, "discipline"),
    _t("collar_evening", "Collar on for 2 hours while you game or watch something. Photo from the collar down, no face.", "0.05", 4, "discipline",
       "collar"),
)
EROTIC_DAYS = 4
_STRONG = frozenset({"deep_diver", "leather_cuffs", "padlocks", "bondage_harness", "blindfold", "toy_cuffs", "dildo_purple", "clamps"})


def erotic_reward(key: str, needs: frozenset) -> Decimal:
    """Owner-approved scale: the stronger tasks (plug ladder, restraints, the vibrating Deep Diver, blindfold, clamps, riding) +0.05, the rest
    +0.04, the size photo +0.03."""
    if key == "dildo_size":
        return Decimal("0.03")
    return Decimal("0.05") if key.startswith("plug:") or needs & _STRONG else Decimal("0.04")


_BY_KEY = {t.key: t for t in BOARD}

# Owner, 2026-11-17 ("make all the things available for other users, if they choose so"): another wearer's board draws only from these,
# which name nothing of the owner's (his gym corner, air bike, treadmill, cable stack, handles, Deep Diver); their erotic task comes from
# their own approved tasks.
GENERIC_KEYS = frozenset({"sink", "desk", "laundry", "vacuum", "sheets", "fridge", "shower", "windows", "wardrobe", "meal_prep", "steps",
                          "stretch", "lines30", "kneel20", "no_social", "collar_evening"})
GENERIC_BOARD: tuple[BoardTask, ...] = tuple(t for t in BOARD if t.key in GENERIC_KEYS)


def get_board_rewards(core: CoreDatabase, user_id: str, lo: str, hi: str) -> Decimal:
    """Read-only: the rewards of board tasks verified in [lo, hi) (the daily evaluation adds them to the token pool)."""
    with core.transaction() as tx:
        rows = tx.fetch_all("SELECT reward FROM board_tasks WHERE user_id = ? AND status = 'passed' AND decided_at >= ? AND decided_at < ?",
                            (user_id, lo, hi))
    return sum((Decimal(r["reward"]) for r in rows), Decimal(0))


class TaskBoard:
    def __init__(self, core: CoreDatabase, *, user_id: str, owned: Callable[[], frozenset], free_time: Callable[[datetime], bool],
                 today: Callable[[datetime], date], day_end: Callable[[date], datetime], local_clock: Callable[[datetime], str],
                 start_task: Callable[[dict, datetime], str | None], task_state: Callable[[str], tuple[str, str | None] | None],
                 raise_missed: Callable[[dict, datetime], None], paused: Callable[[datetime], bool] = lambda now: False,
                 erotic_pick: Callable[[date], BoardTask | None] | None = None, training_today: Callable[[datetime], bool] = lambda now: False,
                 rng: random.Random | None = None, pool: tuple[BoardTask, ...] = BOARD, enabled: Callable[[], bool] = lambda: True) -> None:
        """`start_task(row, now)` plans the Chaster part (returns the obedience task id, None when today's board slot is used);
        `task_state(obedience_id)` -> (status, outcome); `day_end(d)` = the end of that day's free time; `raise_missed(row, now)` asks him."""
        self._core = core
        self.user_id = user_id
        self._owned = owned
        self._free = free_time
        self._today = today
        self._day_end = day_end
        self._clock = local_clock
        self._start = start_task
        self._state = task_state
        self._missed = raise_missed
        self._paused = paused
        self._erotic = erotic_pick
        self._training_today = training_today
        self._rng = rng or random.SystemRandom()
        self._pool = pool                     # owner, 2026-11-17: GENERIC_BOARD for another wearer
        self._enabled = enabled               # the wearer's `task board` switch: off, no new board (taken tasks are still followed)

    # ------------------------------------------------------------------ the weekly board

    def tick(self, now: datetime) -> None:
        try:
            self._post(now)
        except Exception:
            logger.exception("The task board could not be posted; retried next pass.")
        self._follow(now)

    def _post(self, now: datetime) -> None:
        day = self._today(now)
        monday = day - timedelta(days=day.weekday())
        if day != monday or self._paused(now) or not self._free(now) or not self._enabled():
            return
        with self._core.transaction() as tx:
            if tx.fetch_one("SELECT 1 FROM board_tasks WHERE user_id = ? AND week = ?", (self.user_id, monday.isoformat())) is not None:
                return
        owned = self._owned()
        pool = [t for t in self._pool if t.needs <= owned]
        picks = self._rng.sample(pool, min(OFFER_COUNT, len(pool)))
        if self._erotic is not None and picks and self._rng.random() < EROTIC_SHARE:
            erotic = self._erotic(monday)
            if erotic is not None:
                picks[-1] = erotic
        rows = [(str(uuid.uuid4()), t, self._day_end(monday + timedelta(days=t.days))) for t in picks]

        def write(tx: Transaction, _s: object) -> None:
            for ident, t, deadline in rows:
                tx.execute("INSERT INTO board_tasks (id, user_id, week, task_key, kind, text, reward, deadline, status, created_at) "
                           "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'offered', ?)", (ident, self.user_id, monday.isoformat(), t.key, t.kind, t.text,
                                                                             str(t.reward), _iso(deadline), _iso(now)))
            write_event(tx, DomainEvent(event_type="task_board.offered", source_module="task_board", occurred_at=now,
                                        payload={"week": monday.isoformat(), "tasks": [r[0] for r in rows]}))
            lines = [f"**{i}.** {t.text} By {self._clock(deadline)}. Reward: +{t.reward} toward your next token."
                     for i, (_ident, t, deadline) in enumerate(rows, 1)]
            actions = tuple(ActionSpec(action_id=f"board:take:{ident}", label=f"Take {i}") for i, (ident, _t, _d) in enumerate(rows, 1))
            enqueue_in_transaction(tx, user_id=self.user_id, kind="task_board", dedupe_key=f"board:week:{monday.isoformat()}", now=now,
                                   text=("This week's task board. Take one if you want it: a taken task is a promise, and missing it counts. "
                                         "One at a time; press \"Doing it now\" when you start, and it goes to Chaster.\n" + "\n".join(lines)),
                                   actions=actions)

        apply_transition(self._core, write=write)
        decision("task_board", "This week's task board was posted.", tasks=[t.key for _i, t, _d in rows])

    # ------------------------------------------------------------------ his buttons

    def press(self, *, user_id: str, row_id: str, action: str, now: datetime) -> str:
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT * FROM board_tasks WHERE id = ?", (row_id,))
        if row is None or row["user_id"] != user_id:
            return "That isn't yours."
        row = dict(row)
        if action == "take":
            return self._take(row, now)
        if action == "now":
            return self._begin(row, now)
        return "That isn't one of the options."

    def _take(self, row: dict, now: datetime) -> str:
        if row["status"] != "offered":
            return "That one isn't on offer any more."
        if now >= _parse_iso(row["deadline"]):
            return "That one has run out."
        with self._core.transaction() as tx:
            busy = tx.fetch_one("SELECT 1 FROM board_tasks WHERE user_id = ? AND status IN ('taken', 'started')", (self.user_id,))
            earned = tx.fetch_one("SELECT COALESCE(SUM(CAST(reward AS REAL)), 0) AS r FROM board_tasks WHERE user_id = ? AND week = ? "
                                  "AND status IN ('taken', 'started', 'passed')", (self.user_id, row["week"]))["r"]
        if busy is not None:
            return "Finish the task you took first. One at a time."
        if Decimal(str(earned)) + Decimal(row["reward"]) > WEEK_CAP + Decimal("0.0001"):
            return f"That would take this week's board past +{WEEK_CAP}. Next week brings a new one."

        def write(tx: Transaction, _s: object) -> bool:
            done = tx.execute("UPDATE board_tasks SET status = 'taken', taken_at = ? WHERE id = ? AND status = 'offered'", (_iso(now), row["id"])).rowcount
            if done:
                write_event(tx, DomainEvent(event_type="task_board.taken", source_module="task_board", occurred_at=now,
                                            payload={"board_task_id": row["id"], "task_key": row["task_key"]}))
                enqueue_in_transaction(tx, user_id=self.user_id, kind="task_board", dedupe_key=f"board:taken:{row['id']}", now=now,
                                       text=f"Taken: {row['text']} By {self._clock(_parse_iso(row['deadline']))}. Press when you start.",
                                       actions=(ActionSpec(action_id=f"board:now:{row['id']}", label="Doing it now", style="primary"),))
            return bool(done)

        if not apply_transition(self._core, write=write):
            return "That one isn't on offer any more."
        decision("task_board", "A board task was taken.", task=row["task_key"])
        return "Taken. It's a promise now: press \"Doing it now\" when you start."

    def _begin(self, row: dict, now: datetime) -> str:
        if row["status"] != "taken":
            return "That one is already under way." if row["status"] == "started" else "That one isn't yours to start now."
        if not self._free(now):
            return "Not now: start it in your free time."
        if row.get("kind") == "fitness" and self._training_today(now):
            return "Not on a training day: your session is today's workout. Start this one another day."
        ident = self._start(row, now)
        if ident is None:
            return "You already did a board task today. Start this one another day."

        def write(tx: Transaction, _s: object) -> None:
            tx.execute("UPDATE board_tasks SET status = 'started', obedience_task_id = ?, started_at = ? WHERE id = ? AND status = 'taken'",
                       (ident, _iso(now), row["id"]))

        apply_transition(self._core, write=write)
        return "Go. It will be in Chaster in a moment: photo with Chaster's code within the hour."

    # ------------------------------------------------------------------ outcomes

    def _follow(self, now: datetime) -> None:
        with self._core.transaction() as tx:
            rows = [dict(r) for r in tx.fetch_all("SELECT * FROM board_tasks WHERE user_id = ? AND status IN ('offered', 'taken', 'started')",
                                                  (self.user_id,))]
        for row in rows:
            try:
                self._follow_one(row, now)
            except Exception:
                logger.exception("A board task could not be followed up; retried next pass.")

    def _follow_one(self, row: dict, now: datetime) -> None:
        past = now >= _parse_iso(row["deadline"])
        if row["status"] == "offered":
            if past:
                self._decide(row, "expired", None, now)
            return
        if row["status"] == "taken":
            if past:
                if self._decide(row, "missed", "missed", now):
                    self._missed(row, now)
            return
        state = self._state(row["obedience_task_id"]) if row["obedience_task_id"] else None
        if state is None:
            return
        status, outcome = state
        if status == "passed":
            self._decide(row, "passed", outcome, now, text=f"Board task verified: +{row['reward']} toward your next token. Good boy.")
        elif status == "failed":
            self._decide(row, "failed", outcome, now)                # the obedience path already asks him about it
        elif status in ("cancelled", "skipped"):
            if outcome == "health_or_safety":
                self._decide(row, "cancelled", outcome, now)         # never a consequence
            else:
                def back(tx: Transaction, _s: object) -> None:       # it couldn't run (no free slot, an exemption): it's his to start again
                    tx.execute("UPDATE board_tasks SET status = 'taken', obedience_task_id = NULL WHERE id = ? AND status = 'started'", (row["id"],))

                apply_transition(self._core, write=back)

    def _decide(self, row: dict, status: str, outcome: str | None, now: datetime, *, text: str | None = None) -> bool:
        def write(tx: Transaction, _s: object) -> bool:
            done = tx.execute("UPDATE board_tasks SET status = ?, outcome = ?, decided_at = ? WHERE id = ? AND status IN ('offered', 'taken', 'started')",
                              (status, outcome, _iso(now), row["id"])).rowcount
            if done:
                write_event(tx, DomainEvent(event_type="task_board.decided", source_module="task_board", occurred_at=now,
                                            payload={"board_task_id": row["id"], "status": status}))
                if text:
                    enqueue_in_transaction(tx, user_id=self.user_id, kind="task_board", dedupe_key=f"board:decided:{row['id']}", now=now, text=text)
            return bool(done)

        done = apply_transition(self._core, write=write)
        if done and status != "expired":
            decision("task_board", f"A board task ended: {status}.", task=row["task_key"])
        return bool(done)

    async def loop(self, clock, *, interval_seconds: float = 120.0) -> None:
        import asyncio
        while True:
            try:
                await asyncio.to_thread(self.tick, clock.now())
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("The task board pass failed; it continues.")
            await asyncio.sleep(interval_seconds)
