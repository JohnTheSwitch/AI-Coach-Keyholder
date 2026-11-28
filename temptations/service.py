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
