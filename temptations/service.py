"""
temptations/service.py

Temptations that cost (owner, 2026-11-15/16; story idea P5, autonomous queue B7). See README.md.
"""

from __future__ import annotations

import logging
import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Callable

from application.models import ActionSpec
from infrastructure.database import Database as CoreDatabase, Transaction, apply_transition
from infrastructure.outbox import DomainEvent, write_event
from infrastructure.time_format import iso as _iso
from infrastructure.time_format import parse_iso as _parse_iso
from notifications.queue import enqueue_in_transaction

__all__ = ["Temptations", "Ports", "COST", "FREE_SHARE", "DAILY_CHANCE", "OFFER_FOR", "get_temptation_costs", "delay_words"]

logger = logging.getLogger("ai_coach_keyholder.temptations")

COST = Decimal("0.05")
FREE_SHARE = 0.25
DAILY_CHANCE = 1 / 7
OFFER_FOR = timedelta(hours=1)
STEP = timedelta(minutes=15)
# Owner, 2026-11-25: "how am I supposed to edge myself with it on?" -> caged edging said plainly; and "I want it to be controlled by the system in
# every single task having it" -> once he accepts, the bot can run the Deep Diver for the 15 minutes (a teasing pattern, Pause and Stop).
OFFER = ("Temptation: 15 minutes of caged edging. The Deep Diver in, let it work you up, hands only around the cage, right to the edge as often "
         "as you like, no cumming. {price} Offer ends at {until}.")
ACCEPTED = ("Accepted. 15 minutes of caged edging: the Deep Diver in, hands only around the cage, right to the edge as often as you like, and "
            "you do not cum. If you slip, you tell me.")
TOY_MINUTES = 15
TOY_OFFER = " Press \"It's in: run it\" once it's in and I'll run it."
TOY_STARTED = "Connected: you should have felt a short buzz. I'll take you up and let you drop, again and again. Don't cum."
TOY_DONE = "That's your 15 minutes. If you slipped, tell me."
TOY_PROBLEM = ("I couldn't reach your Deep Diver (Intiface on the PC, the toy on and not connected to your phone?). Press Try again, or do it "
               "yourself as accepted.")


def delay_words(cost: Decimal, avg_daily: Decimal | None) -> str:
    """What the cost probably means for his next token, never the cost itself."""
    if cost <= 0:
        return "This one is free."
    if not avg_daily or avg_daily <= 0:
        return "It would push your next token back a little."
    hours = float(cost / avg_daily) * 24
    if hours < 1.5:
        when = "about an hour"
    elif hours < 10:
        when = f"about {round(hours)} hours"
    elif hours < 18:
        when = "about half a day"
    elif hours < 36:
        when = "about a day"
    else:
        when = f"about {round(hours / 24)} days"
    return f"At your pace this would push your next token back by {when}."


# Owner, 2026-11-28: edging during a release session ("Edging while released ... should add tokens instead of costing them") -> "+0.05, bot
# runs the toy". Offered once per confirmed release, while its session lasts and at most EDGE_OFFER_FOR after it; it counts only when the
# bot's 15-minute session really ran to the end and he then says he edged and didn't cum before it ended.
EDGE_REWARD = Decimal("0.05")
EDGE_MINUTES = 15
EDGE_OFFER_FOR = timedelta(hours=2)
EDGE_OFFER = ("Before you cum: 15 minutes of edging first. The Deep Diver in, I run it, your hand on yourself, right to the edge as often as "
              "you can, and you don't cum until the 15 minutes are over. Done properly, it earns +0.05 toward your next token. Press "
              "\"It's in: run it\" when you're ready.")
EDGE_STARTED = "Connected: you should have felt a short buzz. 15 minutes. Edge, and don't cum before I'm done."
EDGE_ASK = "That's 15 minutes. Did you edge the whole time and not cum before it ended?"
EDGE_SHORT = "That session didn't run the full 15 minutes, so it doesn't count. Press \"It's in: run it\" to go again."


def get_release_edge_rewards(core: CoreDatabase, user_id: str, lo: str, hi: str) -> Decimal:
    """Read-only: the release-edging rewards earned in [lo, hi) (owner, 2026-11-28)."""
    with core.transaction() as tx:
        rows = tx.fetch_all("SELECT reward FROM release_edges WHERE user_id = ? AND status = 'earned' AND decided_at >= ? AND decided_at < ?",
                            (user_id, lo, hi))
    return sum((Decimal(r["reward"]) for r in rows), Decimal(0))


def get_temptation_costs(core: CoreDatabase, user_id: str, lo: str, hi: str) -> Decimal:
    """Read-only: the cost of temptations accepted in [lo, hi)."""
    with core.transaction() as tx:
        rows = tx.fetch_all("SELECT cost FROM temptation_offers WHERE user_id = ? AND status = 'accepted' AND decided_at >= ? AND decided_at < ?",
                            (user_id, lo, hi))
    return sum((Decimal(r["cost"]) for r in rows), Decimal(0))


@dataclass
class Ports:
    today: Callable[[datetime], object]                     # local date
    free_time: Callable[[datetime], bool]
    blocked: Callable[[datetime], bool]                     # a wear exemption, a vacation, the halt or a Penalty Window
    avg_daily: Callable[[], Decimal | None]                 # average daily pool progress over the last 14 days
    local_clock: Callable[[datetime], str]
    # Owner, 2026-11-17: another wearer's temptation is one the owner wrote for them and they approved (custom_tasks/), fitting their gear;
    # None here means the owner's own fixed offer. The callable returning None means nothing fits: no offer that day.
    offer_text: Callable[[], str | None] | None = None


class Temptations:
    def __init__(self, core: CoreDatabase, *, user_id: str, ports: Ports, rng: random.Random | None = None) -> None:
        self._core, self.user_id, self._ports = core, user_id, ports
        self._rng = rng or random.SystemRandom()

    def tick(self, now: datetime) -> None:
        try:
            self._plan(now)
            self._offer(now)
            self._expire(now)
        except Exception:
            logger.exception("The temptation pass failed; it continues.")
        try:
            self._release_edge(now)
        except Exception:
            logger.exception("The release edging pass failed; it continues.")

    # ------------------------------------------------------------------ edging during a release (owner, 2026-11-28)
    # Wired in bot/discord_bot.py for his own bot: `last_release()` -> (authorization id, confirmed at) | None, `release_session_over(at, now)`,
    # `clock_now()` for the toy's callbacks. None: no release edging.
    last_release: Callable[[], tuple[str, datetime] | None] | None = None
    release_session_over: Callable[[datetime, datetime], bool] | None = None
    clock_now: Callable[[], datetime] | None = None

    def _release_edge(self, now: datetime) -> None:
        if self.last_release is None or self.toy is None:
            return
        with self._core.transaction() as tx:
            stale = tx.fetch_all("SELECT id, offered_at FROM release_edges WHERE user_id = ? AND status = 'offered'", (self.user_id,))
        for r in stale:
            if now > _parse_iso(r["offered_at"]) + EDGE_OFFER_FOR:
                self._edge_set(r["id"], "expired", now, only_from=("offered",))
        release = self.last_release()
        if release is None:
            return
        auth_id, confirmed_at = release
        if now - confirmed_at > EDGE_OFFER_FOR or now < confirmed_at:
            return
        if self.release_session_over is not None and self.release_session_over(confirmed_at, now):
            return
        edge_id = str(uuid.uuid4())

        def write(tx: Transaction, _s: object) -> bool:
            if not tx.execute("INSERT OR IGNORE INTO release_edges (id, user_id, authorization_id, offered_at, status) VALUES (?, ?, ?, ?, 'offered')",
                              (edge_id, self.user_id, auth_id, _iso(now))).rowcount:
                return False
            write_event(tx, DomainEvent(event_type="temptations.release_edge_offered", source_module="temptations", occurred_at=now,
                                        payload={"release_edge_id": edge_id, "authorization_id": auth_id}))
            enqueue_in_transaction(tx, user_id=self.user_id, kind="temptation", dedupe_key=f"temptation:edge:{edge_id}", now=now, immediate=True,
                                   text=EDGE_OFFER, actions=(ActionSpec(action_id=f"temptation:edge:{edge_id}", label="It's in: run it",
                                                                        style="success"),))
            return True

        if apply_transition(self._core, write=write):
            logger.info("Release edging offered.")

    def _edge_set(self, edge_id: str, status: str, now: datetime, *, only_from: tuple, reward: Decimal | None = None,
                  played: float | None = None) -> bool:
        marks = ",".join("?" for _ in only_from)

        def write(tx: Transaction, _s: object) -> bool:
            done = tx.execute(f"UPDATE release_edges SET status = ?, decided_at = ?, reward = COALESCE(?, reward), "
                              f"played_seconds = COALESCE(?, played_seconds) WHERE id = ? AND status IN ({marks})",
                              (status, _iso(now), None if reward is None else str(reward), played, edge_id, *only_from)).rowcount
            if done:
                write_event(tx, DomainEvent(event_type="temptations.release_edge_decided", source_module="temptations", occurred_at=now,
                                            payload={"release_edge_id": edge_id, "status": status,
                                                     "reward": str(reward) if reward is not None else "0"}))
            return bool(done)

        return bool(apply_transition(self._core, write=write))

    def _edge_buttons(self, edge_id: str, paused: bool = False) -> tuple:
        return (ActionSpec(action_id=f"temptation:edgego:{edge_id}", label="Continue") if paused
                else ActionSpec(action_id=f"temptation:edgepause:{edge_id}", label="Pause"),
                ActionSpec(action_id=f"temptation:edgestop:{edge_id}", label="Stop", style="danger"))

    def _edge_press(self, edge_id: str, action: str, now: datetime):
        from application.models import OutgoingMessage
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT * FROM release_edges WHERE id = ? AND user_id = ?", (edge_id, self.user_id))
        if row is None:
            return "That isn't yours."
        if action in ("edgeyes", "edgeno"):
            if row["status"] != "ran":
                return "Already noted." if row["status"] in ("earned", "declined") else "That session hasn't run its 15 minutes."
            if action == "edgeyes":
                self._edge_set(edge_id, "earned", now, only_from=("ran",), reward=EDGE_REWARD)
                return "Good boy. +0.05 toward your next token. Enjoy your release."
            self._edge_set(edge_id, "declined", now, only_from=("ran",))
            return "Thank you for being honest. No reward this time, no consequence: it's your release."
        if self.toy is None:
            return "I can't run the toy right now."
        if action == "edgestop":
            return "Stopped." if self.toy.stop() else "It isn't running."
        if action == "edgepause":
            return (OutgoingMessage(text="Paused: the toy is off and the time waits.", actions=self._edge_buttons(edge_id, paused=True))
                    if self.toy.pause() else "It isn't running.")
        if action == "edgego":
            return (OutgoingMessage(text="Going on.", actions=self._edge_buttons(edge_id)) if self.toy.resume() else "Nothing is paused.")
        if row["status"] != "offered" or now > _parse_iso(row["offered_at"]) + EDGE_OFFER_FOR:
            return "That offer is over."
        say = self.notify or (lambda key, text, actions: None)
        clock = self.clock_now or (lambda: now)
        level = self.toy_level() if self.toy_level is not None else 0.5
        toy = self.toy

        def done() -> None:
            played = float(getattr(toy, "played_seconds", lambda: 0.0)() or 0.0)
            if played >= EDGE_MINUTES * 60 - 30:
                if self._edge_set(edge_id, "ran", clock(), only_from=("offered",), played=played):
                    say(f"temptation:edge_ask:{edge_id}", EDGE_ASK,
                        (ActionSpec(action_id=f"temptation:edgeyes:{edge_id}", label="Yes, I edged", style="success"),
                         ActionSpec(action_id=f"temptation:edgeno:{edge_id}", label="No")))
            else:
                say(f"temptation:edge_short:{edge_id}:{uuid.uuid4()}", EDGE_SHORT,
                    (ActionSpec(action_id=f"temptation:edge:{edge_id}", label="It's in: run it"),))

        def problem(_text: str) -> None:
            say(f"temptation:edge_problem:{edge_id}:{uuid.uuid4()}", TOY_PROBLEM.replace("or do it yourself as accepted", "or skip it"),
                (ActionSpec(action_id=f"temptation:edge:{edge_id}", label="Try again"),))

        if not toy.start(EDGE_MINUTES, "alone", profile="tease", base=level, on_problem=problem, on_done=done,
                         on_started=lambda: say(f"temptation:edge_started:{edge_id}:{uuid.uuid4()}", EDGE_STARTED, self._edge_buttons(edge_id))):
            return "The toy is already running."
        logger.info("The bot runs the Deep Diver for release edging.")
        return OutgoingMessage(text=f"Running it for {EDGE_MINUTES} minutes. Pause or Stop if you need to; the toy's own button always works too.",
                               actions=self._edge_buttons(edge_id))

    def _plan(self, now: datetime) -> None:
        """Once a local day, in free time: about one day in seven gets an offer at a random free moment later that day."""
        day = self._ports.today(now)
        with self._core.transaction() as tx:
            if tx.fetch_one("SELECT 1 FROM temptation_offers WHERE user_id = ? AND local_date = ?", (self.user_id, day.isoformat())):
                return
        if not self._ports.free_time(now):
            return
        moments, at = [], now + STEP
        while self._ports.today(at) == day:
            if self._ports.free_time(at) and self._ports.free_time(at + OFFER_FOR):
                moments.append(at)
            at += STEP
        lucky = bool(moments) and self._rng.random() < DAILY_CHANCE
        cost = Decimal(0) if self._rng.random() < FREE_SHARE else COST

        def write(tx: Transaction, _s: object) -> None:
            status = "planned" if lucky else "skipped"
            tx.execute("INSERT OR IGNORE INTO temptation_offers (id, user_id, local_date, offer_at, cost, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (str(uuid.uuid4()), self.user_id, day.isoformat(), _iso(self._rng.choice(moments) if lucky else now), str(cost), status, _iso(now)))

        apply_transition(self._core, write=write)

    def _offer(self, now: datetime) -> None:
        with self._core.transaction() as tx:
            rows = [dict(r) for r in tx.fetch_all("SELECT * FROM temptation_offers WHERE user_id = ? AND status = 'planned' AND offer_at <= ?",
                                                  (self.user_id, _iso(now)))]
        for row in rows:
            if self._ports.blocked(now) or not self._ports.free_time(now):
                self._set(row["id"], "skipped", now)
                continue
            until = now + OFFER_FOR
            try:
                avg = self._ports.avg_daily()
            except Exception:
                logger.exception("The average progress could not be read; the offer says 'a little'.")
                avg = None
            own = None
            if self._ports.offer_text is not None:
                try:
                    own = self._ports.offer_text()
                except Exception:
                    logger.exception("The wearer's temptations could not be read; no offer.")
                if not own:
                    self._set(row["id"], "skipped", now)
                    continue
            price = delay_words(Decimal(row["cost"]), avg)
            text = (f"Temptation: {own} {price} Offer ends at {self._ports.local_clock(until)}." if own
                    else OFFER.format(price=price, until=self._ports.local_clock(until)))

            def write(tx: Transaction, _s: object, r=row, own=own) -> None:
                if tx.execute("UPDATE temptation_offers SET status = 'offered', expires_at = ?, text = ? WHERE id = ? AND status = 'planned'",
                              (_iso(until), own, r["id"])).rowcount:
                    write_event(tx, DomainEvent(event_type="temptations.offered", source_module="temptations", occurred_at=now,
                                                payload={"offer_id": r["id"], "free": Decimal(r["cost"]) == 0}))
                    enqueue_in_transaction(tx, user_id=self.user_id, kind="temptation", dedupe_key=f"temptation:{r['id']}", now=now, immediate=True,
                                           text=text, actions=(ActionSpec(action_id=f"temptation:accept:{r['id']}", label="Accept"),))

            apply_transition(self._core, write=write)

    def _expire(self, now: datetime) -> None:
        with self._core.transaction() as tx:
            rows = tx.fetch_all("SELECT id FROM temptation_offers WHERE user_id = ? AND status = 'offered' AND expires_at < ?", (self.user_id, _iso(now)))
        for r in rows:
            self._set(r["id"], "expired", now)

    def _set(self, offer_id: str, status: str, now: datetime) -> bool:
        def write(tx: Transaction, _s: object) -> bool:
            done = tx.execute("UPDATE temptation_offers SET status = ?, decided_at = ? WHERE id = ? AND status IN ('planned', 'offered')",
                              (status, _iso(now), offer_id)).rowcount
            if done:
                write_event(tx, DomainEvent(event_type=f"temptations.{status}", source_module="temptations", occurred_at=now,
                                            payload={"offer_id": offer_id}))
            return bool(done)

        return bool(apply_transition(self._core, write=write))

    # The bot runs the Deep Diver (owner, 2026-11-28): wired in bot/discord_bot.py. `toy` is toy_control.session.ToySessions, `toy_level()`
    # his level (toy_control.store), `notify(key, text, actions)` a DM.
    toy = None
    toy_level: Callable[[], float] | None = None
    notify: Callable[[str, str, tuple], None] | None = None

    def _toy_buttons(self, row_id: str, paused: bool = False) -> tuple:
        return (ActionSpec(action_id=f"temptation:toygo:{row_id}", label="Continue") if paused
                else ActionSpec(action_id=f"temptation:toypause:{row_id}", label="Pause"),
                ActionSpec(action_id=f"temptation:toystop:{row_id}", label="Stop", style="danger"))

    def _toy_press(self, row: dict, action: str, now: datetime):
        from application.models import OutgoingMessage
        if self.toy is None:
            return "I can't run the toy right now; do it yourself as accepted."
        if action == "toystop":
            return "Stopped." if self.toy.stop() else "It isn't running."
        if action == "toypause":
            return (OutgoingMessage(text="Paused: the toy is off and the time waits.", actions=self._toy_buttons(row["id"], paused=True))
                    if self.toy.pause() else "It isn't running.")
        if action == "toygo":
            return (OutgoingMessage(text="Going on.", actions=self._toy_buttons(row["id"])) if self.toy.resume() else "Nothing is paused.")
        if now > _parse_iso(row["decided_at"]) + timedelta(hours=1):
            return "That was a while ago; the offer's time is over."
        say = self.notify or (lambda key, text, actions: None)
        row_id = row["id"]
        level = self.toy_level() if self.toy_level is not None else 0.5

        def problem(_text: str) -> None:
            say(f"temptation:toy_problem:{row_id}:{uuid.uuid4()}", TOY_PROBLEM,
                (ActionSpec(action_id=f"temptation:toy:{row_id}", label="Try again"),))

        started = self.toy.start(TOY_MINUTES, "alone", profile="tease", base=level, on_problem=problem,
                                 on_done=lambda: say(f"temptation:toy_done:{row_id}:{uuid.uuid4()}", TOY_DONE, ()),
                                 on_started=lambda: say(f"temptation:toy_started:{row_id}:{uuid.uuid4()}", TOY_STARTED, self._toy_buttons(row_id)))
        if not started:
            return "The toy is already running."
        logger.info("The bot runs the Deep Diver for an accepted temptation.")
        return OutgoingMessage(text=f"Running it for {TOY_MINUTES} minutes. Pause or Stop if you need to; the toy's own button always works too.",
                               actions=self._toy_buttons(row_id))

    def press(self, *, user_id: str, row_id: str, action: str, now: datetime):
        if user_id == self.user_id and action in ("edge", "edgestop", "edgepause", "edgego", "edgeyes", "edgeno"):
            return self._edge_press(row_id, action, now)
        if user_id != self.user_id or action not in ("accept", "toy", "toystop", "toypause", "toygo"):
            return "That button isn't for you."
        if action != "accept":
            with self._core.transaction() as tx:
                row = tx.fetch_one("SELECT * FROM temptation_offers WHERE id = ? AND user_id = ?", (row_id, user_id))
            if row is None or row["status"] != "accepted":
                return "That offer isn't accepted."
            return self._toy_press(dict(row), action, now)
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT * FROM temptation_offers WHERE id = ? AND user_id = ?", (row_id, user_id))
        if row is None or row["status"] != "offered" or now > _parse_iso(row["expires_at"]):
            return "That offer is gone."
        if not self._set(row_id, "accepted", now):
            return "That offer is gone."
        if "text" in row.keys() and row["text"]:
            return f"Accepted: {row['text']} If you slip, you tell me."
        if self.toy is not None:
            from application.models import OutgoingMessage
            return OutgoingMessage(text=ACCEPTED + TOY_OFFER, actions=(ActionSpec(action_id=f"temptation:toy:{row_id}", label="It's in: run it",
                                                                                  style="success"),))
        return ACCEPTED

    async def loop(self, clock, *, interval_seconds: float = 120.0) -> None:
        import asyncio
        while True:
            try:
                await asyncio.to_thread(self.tick, clock.now())
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("The temptation pass failed; it continues.")
            await asyncio.sleep(interval_seconds)
