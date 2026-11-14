"""
obedience/service.py

Daily obedience tasks (owner, 2026-11-14; work queue Q4b; migration 069): "a strict keyholder that would make me an obedient slave".

One task a day at a random moment in free time (never quiet hours or work hours), from a closed catalogue (`CATALOG`) that uses only the gear
the owner listed: metal plugs, the vibrating plug, two suction-cup dildos, two ropes, cuffs; about one in four days a workout task from his home
gym. It is shown as a Chaster task: a photo with Chaster's code, judged by the community. The photo shows what the text names and only that:
never his face or anything he could be recognised by.

  plan     on the day's first pass in free time, a random moment later that day is chosen (none left: no task that day)
  start    at that moment, when Chaster's one task slot is free (a spot check goes first), the task is assigned; PHOTO_DEADLINE to submit;
           the DM gets the task with one button: "I can't now (health or safety)"
  poll     verified -> passed; rejected, or nothing in time -> failed: the wearer is asked to explain (infractions path, the same judge);
           unexplained = OBEDIENCE_TASK_MISSED, a Warning-level incident (Trust and 20 lines)
  button   "I can't now (health or safety)" -> cancelled, no consequence (a stated need is believed, philosophy 3.9)

Plug ladder (owner, 2026-11-14: "Yes" to training up to his Wolf Rimfire S): about one erotic day in three is a plug task on the ladder
`PLUG_LADDER`, sizes from the shop pages. He moves up one rung only after LADDER_STEP verified wears on the current one, and only to a plug
he owns (`owned`); wearing time grows with each verified wear at a rung. Pain ends it with no consequence.

No task during a wear exemption (owner, 2026-11-14): nothing is planned or started while one is in force, and an open task is cancelled
without consequence when one begins.

Never: during a vacation, while the runtime is halted, or when the slot stays taken until the free time ends (the day is then skipped, no
consequence). Never anything public or outdoors, never strong pain; restraints only ones he can free himself from; times are capped.
"""

from __future__ import annotations

import logging
import random
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

logger = logging.getLogger("ai_coach_keyholder.obedience")

__all__ = ["ObedienceTasks", "Ports", "CATALOG", "Task", "PHOTO_DEADLINE", "WORKOUT_SHARE"]

PHOTO_DEADLINE = timedelta(minutes=60)
VOTE_LIMIT = timedelta(hours=12)
CLOCK_SKEW = timedelta(minutes=2)
WORKOUT_SHARE = 0.25                     # about one day in four is a workout task (owner, 2026-11-14)
PLAN_STEP = timedelta(minutes=15)
POSTPONE = timedelta(hours=2)            # "Not alone right now": once per task (owner, 2026-11-14: he does not live alone)
DISCRETION = "Do it in your room with the door closed, keep it quiet, and leave nothing lying around or visible afterwards."
SAFETY = ("Stop at once if anything hurts, bleeds, goes numb or feels wrong; that never counts against you. Never numbing lube, never "
          "overnight.")


@dataclass(frozen=True)
class Task:
    key: str
    text: str                           # the Chaster task: what to do and exactly what the photo shows (plain, like "Show your lockbox")
    kind: str                           # erotic | workout


PHOTO_BEHIND = "Photo from behind, waist down only, code beside it."

# Owner, 2026-11-14: "make the tasks so that it's not repetitive". Most erotic days are COMPOSED from a pose and one or two pieces of his gear,
# so there are dozens of combinations; a combination used in the last REPEAT_DAYS days is not used again, and the main gear never repeats
# two days in a row. The photo is always from behind, waist down: never his face.
POSES: tuple[tuple[str, str, int], ...] = (                # (key, words, max minutes)
    ("kneel", "kneeling upright, hands on thighs", 20),
    ("fours", "on all fours", 15),
    ("forehead", "kneeling, forehead to the floor", 15),
    ("wall", "facing a wall, hands behind your head", 15),
    ("corner", "kneeling in a corner, hands behind your back", 15),
    ("facedown", "lying face down, legs apart", 20),
)
ADDONS: dict[str, str] = {
    "vibe": "Deep Diver in and vibrating",
    "blind": "blindfolded",
    "cuffs": "wrists cuffed in front, key within reach",
    "thighs": "thighs tied with the red rope, shears within reach",
    "word": "'locked' written on your thigh",
}
_ADDON_BAN = {("blind", "cuffs"), ("blind", "thighs"), ("cuffs", "thighs")}            # never blindfolded in restraints; one restraint at a time
_POSE_BAN = {("cuffs", "wall"), ("cuffs", "corner"), ("thighs", "wall"), ("thighs", "fours")}   # hands are behind / legs must move
REPEAT_DAYS = 14

EXTRAS: tuple[Task, ...] = (
    Task("dildo_kneel", "Show: kneeling with the purple RealRock dildo stuck to the floor in front of you. Photo of the dildo and your knees only, code beside it.", "erotic"),
    Task("dildo_ride", "Show: the purple RealRock dildo stuck to the floor and you riding it, with water-based lube. " + PHOTO_BEHIND, "erotic"),
    Task("dildo_size", "Show: the big Lovetoy dildo next to your cage, for size. Photo of the two only, code beside it.", "erotic"),
    Task("rope_harness", "Show: a chest harness in the red rope. Photo from the neck down, no face, code beside it. Keep it 20 minutes, shears within reach.", "erotic"),
    Task("rope_ankles", "Show: your ankles tied with the hemp rope. Photo of your ankles only, code beside it. Keep it 20 minutes, shears within reach.", "erotic"),
)
WORKOUTS: tuple[Task, ...] = (
    Task("workout_squats", "Show: the bar in your rack after 5 sets of squats. Photo of the bar and your legs only, code beside it.", "workout"),
    Task("workout_pullups", "Show: 5 sets of pull-ups done, the last one with your vest. Photo of the bar and your hands only, code beside it.", "workout"),
    Task("workout_pushups", "Show: 50 push-ups on your handles with the gray Deep Diver in. Photo from the side, neck down only, code beside it.", "workout"),
    Task("workout_plank", "Show: a 3-minute plank, cage visible. Photo from the side, neck down only, code beside it.", "workout"),
    Task("workout_bike", "Show: 10 rounds of 20 s hard / 40 s easy on the air bike. Photo of the bike's display only, code beside it.", "workout"),
    Task("workout_incline", "Show: 20 minutes of incline walking on the treadmill. Photo of the treadmill display only, code beside it.", "workout"),
    Task("workout_calves", "Show: 4 sets of calf raises with the bar. Photo of the bar and your calves only, code beside it.", "workout"),
    Task("workout_cable", "Show: 4 sets of cable rows on the Laubr. Photo of the stack and the handle only, code beside it.", "workout"),
    Task("workout_log", "Show: today's workout written down, set by set. Photo of the page only, code beside it.", "workout"),
)
CATALOG: tuple[Task, ...] = EXTRAS + WORKOUTS                # the fixed tasks (composed and ladder tasks are built when picked)
_BY_KEY = {t.key: t for t in CATALOG}


def compose(rng, banned: set[str], yesterday_gear: set[str]) -> Task | None:
    """A pose plus one or two add-ons, never a combination in `banned` and never yesterday's main gear. None when nothing new is left."""
    combos = []
    for pose, words, minutes in POSES:
        names = list(ADDONS)
        options = [(a,) for a in names] + [(a, b) for i, a in enumerate(names) for b in names[i + 1:]]
        for addons in options:
            if any((a, b) in _ADDON_BAN or (b, a) in _ADDON_BAN for a in addons for b in addons if a != b):
                continue
            if any((a, pose) in _POSE_BAN for a in addons) or set(addons) & yesterday_gear:
                continue
            key = "pose:" + pose + "+" + "+".join(addons)
            if key not in banned:
                combos.append((key, words, minutes, addons))
    if not combos:
        return None
    key, words, minutes, addons = rng.choice(combos)
    minutes = rng.choice([m for m in (10, 15, 20) if m <= minutes])
    parts = ", ".join(ADDONS[a] for a in addons)
    return Task(key, f"Show: {words}, {parts}. {PHOTO_BEHIND} {minutes} minutes.", "erotic")


def gear_of(key: str) -> set[str]:
    """The main gear a task used (for "never two days in a row")."""
    if key.startswith("pose:"):
        return set(key.split("+")[1:])
    if key.startswith("plug:"):
        return {"plug"}
    return {key.split("_")[0]}


@dataclass(frozen=True)
class Plug:
    key: str
    name: str
    diameter_cm: float
    owned: bool
    lube: str                                # what the plug's material takes


# Sizes from sexshop51.cz product pages and retailer specs (2026-11-14). Mark a plug owned when the owner says he has it.
# Research (2026-11-14): warm up on the size below, short sessions growing slowly, rest days between sessions, water-based lube on silicone
# and PVC, never numbing lube, never overnight, stop at pain or bleeding.
PLUG_LADDER: tuple[Plug, ...] = (
    Plug("deep_diver", "gray Satisfyer Deep Diver", 3.3, True, "water-based lube"),
    Plug("bijou_large", "clear Crushious Bijou", 4.1, True, "plenty of lube"),
    Plug("gildo_27", "glass Gildo No. 27", 4.5, False, "plenty of lube"),
    Plug("plug_joy_medium", "Plug & Joy medium", 5.5, False, "water-based lube"),
    Plug("rimfire_s", "Wolf Rimfire S", 6.0, True, "water-based lube"),
)
LADDER_STEP = 3                          # verified wears on a rung before the next one
LADDER_SHARE = 1 / 3                     # of erotic days
LADDER_MINUTES = (20, 30, 45, 60)        # wearing time by verified wears at the current rung


@dataclass(frozen=True, kw_only=True)
class Ports:
    lock_id: Callable[[datetime], str | None]
    halted: Callable[[], bool]
    paused: Callable[[datetime], bool]
    free_time: Callable[[datetime], bool]                  # the moment is in free time (not quiet, not work hours)
    today: Callable[[datetime], date]                      # the wearer's local date
    local_clock: Callable[[datetime], str]
    slot_taken: Callable[[], bool]                         # an open spot check holds Chaster's one task slot
    raise_failure: Callable[[dict, str, datetime], None]   # (row, reason, now): the infractions path
    in_exemption: Callable[[], bool] = lambda: False       # a wear exemption is in force: no tasks (owner, 2026-11-14)
    training: Callable[[date], object] | None = None       # Q4c: training.service.TrainingPlan.day_task (the plan's session, or None)
    training_tick: Callable[[datetime], None] | None = None  # Q4c: the Monday question about the short session


class ObedienceTasks:
    def __init__(self, core: CoreDatabase, *, user_id: str, chaster, ports: Ports, rng: random.Random | None = None) -> None:
        self._core = core
        self.user_id = user_id
        self._chaster = chaster
        self._ports = ports
        self._rng = rng or random.SystemRandom()

    # ------------------------------------------------------------------ plan and start

    def tick(self, now: datetime) -> None:
        if self._ports.in_exemption():
            self._cancel_open(now, "exemption")
            return
        if self._ports.training_tick is not None:
            try:
                self._ports.training_tick(now)
            except Exception:
                logger.exception("The training plan's pass failed; tasks go on.")
        self._plan(now)
        self._start_due(now)
        self.poll(now)

    def _plan(self, now: datetime) -> None:
        day = self._ports.today(now)
        if self._row_for(day) is not None or self._ports.paused(now) or not self._ports.free_time(now):
            return
        moments, at = [], now + PLAN_STEP
        while self._ports.today(at) == day:
            if self._ports.free_time(at) and self._ports.free_time(at + PHOTO_DEADLINE):
                moments.append(at)
            at += PLAN_STEP
        if not moments:
            return
        training = self._ports.training(day) if self._ports.training is not None else None
        if training is not None:
            # Work queue Q4c: a training day's task is the plan's session, given at the start of free time so the whole evening is his.
            planned, task, details = moments[0], Task(training.key, training.text, "workout"), training.details
        else:
            planned, task, details = self._rng.choice(moments), self._pick(day), None
        row_id = str(uuid.uuid4())

        def write(tx: Transaction, _s: object) -> None:
            tx.execute("INSERT OR IGNORE INTO obedience_tasks (id, user_id, local_date, catalog_key, text, planned_at, status, created_at, details) "
                       "VALUES (?, ?, ?, ?, ?, ?, 'planned', ?, ?)",
                       (row_id, self.user_id, day.isoformat(), task.key, task.text, _iso(planned), _iso(now), details))

        apply_transition(self._core, write=write)
        decision("obedience", "Today's obedience task was planned.", day=day.isoformat(), task=task.key, planned_at=_iso(planned))

    def _pick(self, day: date) -> Task:
        """Workout about one day in four; otherwise the plug ladder (never two days running: a rest day between plug sessions), a composed
        pose task, or one of the extras. Nothing used in the last REPEAT_DAYS days, and never yesterday's main gear."""
        since = (day - timedelta(days=REPEAT_DAYS)).isoformat()
        with self._core.transaction() as tx:
            rows = [dict(r) for r in tx.fetch_all("SELECT local_date, catalog_key FROM obedience_tasks WHERE user_id = ? AND local_date >= ? "
                                                  "AND local_date < ?", (self.user_id, since, day.isoformat()))]
        banned = {r["catalog_key"] for r in rows}
        yesterday = (day - timedelta(days=1)).isoformat()
        yesterday_gear = set().union(*(gear_of(r["catalog_key"]) for r in rows if r["local_date"] == yesterday)) if rows else set()
        if self._rng.random() < WORKOUT_SHARE:
            options = [t for t in WORKOUTS if t.key not in banned] or list(WORKOUTS)
            return self._rng.choice(options)
        roll = self._rng.random()
        if roll < LADDER_SHARE and "plug" not in yesterday_gear:
            return self.ladder_task()
        if roll < 0.8:
            composed = compose(self._rng, banned, yesterday_gear)
            if composed is not None:
                return composed
        options = [t for t in EXTRAS if t.key not in banned and not gear_of(t.key) & yesterday_gear] or list(EXTRAS)
        return self._rng.choice(options)

    def ladder_task(self) -> Task:
        """The next plug task on the ladder: the current rung's plug, a warm-up on the size below, the right lube, and a wearing time that grows
        with verified wears there. A plug task cancelled for health or safety sends the next one a rung back down."""
        with self._core.transaction() as tx:
            passed = {r["catalog_key"]: r["n"] for r in tx.fetch_all(
                "SELECT catalog_key, COUNT(*) AS n FROM obedience_tasks WHERE user_id = ? AND status = 'passed' AND outcome = 'verified' "
                "AND catalog_key LIKE 'plug:%' GROUP BY catalog_key", (self.user_id,))}
            last = tx.fetch_one("SELECT catalog_key, outcome FROM obedience_tasks WHERE user_id = ? AND catalog_key LIKE 'plug:%' AND decided_at IS NOT NULL "
                                "ORDER BY decided_at DESC LIMIT 1", (self.user_id,))
        owned = [p for p in PLUG_LADDER if p.owned]
        index = 0
        for i, plug in enumerate(owned):
            index = i
            if passed.get(f"plug:{plug.key}", 0) < LADDER_STEP:
                break
        if last is not None and last["outcome"] == "health_or_safety" and last["catalog_key"] == f"plug:{owned[index].key}" and index > 0:
            index -= 1                                               # it hurt last time: one rung back
        rung = owned[index]
        wears = passed.get(f"plug:{rung.key}", 0)
        minutes = LADDER_MINUTES[min(wears, len(LADDER_MINUTES) - 1)]
        warm = f" Warm up with the {owned[index - 1].name} for 10 minutes first." if index > 0 else ""
        return Task(f"plug:{rung.key}", f"Show: your {rung.name} in, with {rung.lube}.{warm} Photo of the plug base only, code beside it. "
                                         f"Keep it in {minutes} minutes.", "erotic")

    def _cancel_open(self, now: datetime, outcome: str) -> None:
        with self._core.transaction() as tx:
            rows = [dict(r) for r in tx.fetch_all("SELECT * FROM obedience_tasks WHERE user_id = ? AND status IN ('assigned', 'submitted')",
                                                  (self.user_id,))]
        for row in rows:
            try:
                current = self._chaster.current_task(row["lock_id"]) if row["task_id"] and not self._ports.halted() else None
                if current is not None and current.get("id") == row["task_id"]:
                    self._chaster.unassign(row["lock_id"])
            except Exception:
                logger.exception("An obedience task could not be taken back in Chaster.")
            if self._close(row, "cancelled", outcome, now, text="Your task is cancelled: a wear exemption is in force. No consequence."):
                decision("obedience", "An obedience task was cancelled by a wear exemption; no consequence.", task=row["catalog_key"])

    def _start_due(self, now: datetime) -> None:
        with self._core.transaction() as tx:
            rows = [dict(r) for r in tx.fetch_all("SELECT * FROM obedience_tasks WHERE user_id = ? AND status = 'planned' AND planned_at <= ?",
                                                  (self.user_id, _iso(now)))]
        for row in rows:
            try:
                self._start(row, now)
            except Exception:
                logger.exception("An obedience task could not start; it is retried.")

    def _start(self, row: dict, now: datetime) -> None:
        if self._ports.today(now).isoformat() != row["local_date"] or not self._ports.free_time(now + PHOTO_DEADLINE) or self._ports.paused(now):
            self._close(row, "skipped", "no_free_slot", now)            # the free time is over: no task today, no consequence
            return
        lock_id = self._ports.lock_id(now)
        if lock_id is None or self._ports.halted() or self._ports.slot_taken() or self._chaster.current_task(lock_id) is not None:
            return                                                      # tried again next pass
        task_id = self._chaster.assign(lock_id, row["text"][:200])
        deadline = now + PHOTO_DEADLINE
        if row.get("details"):                                          # a training session: the log photo is due by the end of free time
            at = now + PHOTO_DEADLINE
            while self._ports.today(at + PLAN_STEP).isoformat() == row["local_date"] and self._ports.free_time(at + PLAN_STEP):
                at += PLAN_STEP
            deadline = at
        if row.get("details"):
            message = (f"Today's training:\n{row['details']}\nWhen you're done, in Chaster: {row['text']}\nDue by {self._ports.local_clock(deadline)}. "
                       f"{SAFETY}")
            actions = (ActionSpec(action_id=f"obedience:cannot:{row['id']}", label="I can't now (health or safety)"),)
        else:
            message = (f"Your task for today is in Chaster now:\n{row['text']}\nPhoto with Chaster's code by "
                       f"{self._ports.local_clock(deadline)}. {DISCRETION} {SAFETY}")
            actions = (ActionSpec(action_id=f"obedience:later:{row['id']}", label="Not alone right now (+2 h, once)"),
                       ActionSpec(action_id=f"obedience:cannot:{row['id']}", label="I can't now (health or safety)"))

        def write(tx: Transaction, _s: object) -> bool:
            done = tx.execute("UPDATE obedience_tasks SET status = 'assigned', lock_id = ?, task_id = ?, deadline = ? WHERE id = ? AND status = 'planned'",
                              (lock_id, task_id, _iso(deadline), row["id"])).rowcount
            if done:
                write_event(tx, DomainEvent(event_type="obedience.task_assigned", source_module="obedience", occurred_at=now,
                                            payload={"obedience_task_id": row["id"], "catalog_key": row["catalog_key"]}))
                enqueue_in_transaction(tx, user_id=self.user_id, kind="obedience_task", dedupe_key=f"obedience:task:{row['id']}", now=now, immediate=True,
                                       text=message, actions=actions)
            return bool(done)

        if apply_transition(self._core, write=write):
            decision("obedience", "Today's obedience task was assigned in Chaster.", task=row["catalog_key"], deadline=_iso(deadline))

    # ------------------------------------------------------------------ poll

    def poll(self, now: datetime) -> int:
        with self._core.transaction() as tx:
            rows = [dict(r) for r in tx.fetch_all("SELECT * FROM obedience_tasks WHERE user_id = ? AND status IN ('assigned', 'submitted')",
                                                  (self.user_id,))]
        decided = 0
        for row in rows:
            try:
                decided += self._poll_one(row, now)
            except Exception:
                logger.exception("Polling an obedience task failed; it is retried.")
        return decided

    def _poll_one(self, row: dict, now: datetime) -> int:
        deadline = _parse_iso(row["deadline"])
        entry = self._chaster.task_entry(row["lock_id"], row["task_id"])
        if entry is None or entry.get("status") == "removed":
            if now <= deadline + CLOCK_SKEW:
                return 0
            try:
                current = self._chaster.current_task(row["lock_id"])
                if current is not None and current.get("id") == row["task_id"] and not self._ports.halted():
                    self._chaster.unassign(row["lock_id"])
            except Exception:
                logger.exception("A missed obedience task could not be taken back; it is still decided.")
            return self._fail(row, "missed", now)
        submitted = _parse_iso(entry["createdAt"]) if isinstance(entry.get("createdAt"), str) else None
        if submitted is not None and submitted > deadline + CLOCK_SKEW:
            return self._fail(row, "late", now)
        review = entry.get("peerVerification") if isinstance(entry.get("peerVerification"), dict) else {}
        status = review.get("status")
        if row["status"] == "assigned" or row["review_status"] != status:
            self._update(row["id"], status="submitted", submitted_at=_iso(submitted) if submitted else None, review_status=status)
        if status == "verified":
            self._close(row, "passed", "verified", now, text="Your task was verified. Good.")
            return 1
        if status == "rejected":
            return self._fail(row, "rejected", now)
        if submitted is not None and now > submitted + VOTE_LIMIT:
            self._close(row, "passed", "unknown", now)                  # a vote that never ends never counts against him
            return 1
        return 0

    def _fail(self, row: dict, reason: str, now: datetime) -> int:
        if row["catalog_key"].startswith("training:short:"):             # Q4c: the short session is optional, missing it costs nothing
            if self._close(row, "skipped", reason, now):
                decision("obedience", "The optional short training session was skipped; no consequence.", task=row["catalog_key"])
            return 1
        if self._close(row, "failed", reason, now):
            self._ports.raise_failure(row, reason, now)
            decision("obedience", "An obedience task failed; the wearer is asked to explain.", task=row["catalog_key"], reason=reason)
        return 1

    # ------------------------------------------------------------------ the wearer's button

    def press(self, *, user_id: str, row_id: str, action: str, now: datetime) -> str:
        row = self._row(row_id)
        if row is None or row["user_id"] != user_id:
            return "That isn't yours to decide."
        if action not in ("cannot", "later"):
            return "That isn't one of the options."
        if row["status"] not in ("planned", "assigned", "submitted"):
            return "That task is already decided."
        if action == "later":
            return self._postpone(row, now)
        try:
            if row["task_id"] and not self._ports.halted():
                current = self._chaster.current_task(row["lock_id"])
                if current is not None and current.get("id") == row["task_id"]:
                    self._chaster.unassign(row["lock_id"])
        except Exception:
            logger.exception("A cancelled obedience task could not be taken back in Chaster.")
        self._close(row, "cancelled", "health_or_safety", now)
        decision("obedience", "An obedience task was cancelled for health or safety; no consequence.", task=row["catalog_key"])
        return "Cancelled, no consequence. Look after yourself first."

    def _postpone(self, row: dict, now: datetime) -> str:
        """Once per task: the photo deadline moves to POSTPONE from now. A second press, or one after a photo was sent, changes nothing."""
        if row["postponed_at"]:
            return "You already postponed this one. Do it now, or it counts as missed."
        if row["status"] != "assigned" or not row["deadline"]:
            return "This task can't be postponed now."
        deadline = now + POSTPONE
        if not self._ports.free_time(deadline):
            return "Postponing would push it past your free time. Do it now, or it counts as missed."

        def write(tx: Transaction, _s: object) -> bool:
            done = tx.execute("UPDATE obedience_tasks SET deadline = ?, postponed_at = ? WHERE id = ? AND status = 'assigned' AND postponed_at IS NULL",
                              (_iso(deadline), _iso(now), row["id"])).rowcount
            if done:
                write_event(tx, DomainEvent(event_type="obedience.task_postponed", source_module="obedience", occurred_at=now,
                                            payload={"obedience_task_id": row["id"], "deadline": _iso(deadline)}))
            return bool(done)

        if not apply_transition(self._core, write=write):
            return "You already postponed this one. Do it now, or it counts as missed."
        decision("obedience", "An obedience task was postponed once (not alone).", task=row["catalog_key"], deadline=_iso(deadline))
        return f"Postponed once. The photo is now due by {self._ports.local_clock(deadline)}, and there's no second postponement."

    # ------------------------------------------------------------------ reads

    def is_obedience_task(self, task_id: str | None) -> bool:
        if not task_id:
            return False
        with self._core.transaction() as tx:
            return tx.fetch_one("SELECT 1 FROM obedience_tasks WHERE task_id = ?", (task_id,)) is not None

    def has_open_task(self) -> bool:
        with self._core.transaction() as tx:
            return tx.fetch_one("SELECT 1 FROM obedience_tasks WHERE user_id = ? AND status IN ('assigned', 'submitted')", (self.user_id,)) is not None

    def due_now(self, now: datetime) -> bool:
        """A planned task whose moment has come: lines and recovery offers wait for it (the slot order after a spot check)."""
        with self._core.transaction() as tx:
            return tx.fetch_one("SELECT 1 FROM obedience_tasks WHERE user_id = ? AND status = 'planned' AND planned_at <= ?",
                                (self.user_id, _iso(now))) is not None

    # ------------------------------------------------------------------ internals

    def _row_for(self, day: date) -> dict | None:
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT * FROM obedience_tasks WHERE user_id = ? AND local_date = ?", (self.user_id, day.isoformat()))
        return None if row is None else dict(row)

    def _row(self, row_id: str) -> dict | None:
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT * FROM obedience_tasks WHERE id = ?", (row_id,))
        return None if row is None else dict(row)

    def _close(self, row: dict, status: str, outcome: str, now: datetime, *, text: str | None = None) -> bool:
        def write(tx: Transaction, _s: object) -> bool:
            done = tx.execute("UPDATE obedience_tasks SET status = ?, outcome = ?, decided_at = ? WHERE id = ? AND status IN ('planned', 'assigned', 'submitted')",
                              (status, outcome, _iso(now), row["id"])).rowcount
            if done:
                write_event(tx, DomainEvent(event_type="obedience.task_decided", source_module="obedience", occurred_at=now,
                                            payload={"obedience_task_id": row["id"], "status": status, "outcome": outcome}))
                if text:
                    enqueue_in_transaction(tx, user_id=self.user_id, kind="obedience_task", dedupe_key=f"obedience:decided:{row['id']}", now=now,
                                           immediate=True, text=text)
            return bool(done)

        return apply_transition(self._core, write=write)

    def _update(self, row_id: str, **fields) -> None:
        def write(tx: Transaction, _s: object) -> None:
            sets = ", ".join(f"{k} = ?" for k in fields)
            tx.execute(f"UPDATE obedience_tasks SET {sets} WHERE id = ?", (*fields.values(), row_id))

        apply_transition(self._core, write=write)

    async def loop(self, clock, *, interval_seconds: float = 120.0) -> None:
        import asyncio
        while True:
            try:
                await asyncio.to_thread(self.tick, clock.now())
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("The obedience-task pass failed; it continues.")
            await asyncio.sleep(interval_seconds)
