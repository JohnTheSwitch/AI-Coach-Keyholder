"""Prostate training, sensory deprivation, waiting at the door, temptations, the monthly review (owner, 2026-11-15/16; queue B4-B8)."""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from tests.lines.test_lines import core

U = "wearer"
NOW = datetime(2026, 11, 16, 17, 0, tzinfo=timezone.utc)


class TestCatalogue:
    def test_the_new_tasks_and_their_limits(self) -> None:
        from obedience.service import CATALOG, DEFAULT_OWNED, chaster_brief, suits
        by_key = {t.key: t for t in CATALOG}
        prostate = by_key["prostate_training"]
        assert prostate.modes == frozenset({"alone"}) and "never counted as a release" in prostate.dm_note
        assert chaster_brief(prostate.text) == "Prostate training: the vibrating plug in, lying down, hands off the cage. Photo of the plug base only."
        assert suits(by_key["sensory_kneel_eyes"], DEFAULT_OWNED, "alone") and "airpods" in DEFAULT_OWNED
        assert not suits(by_key["door_wait"], DEFAULT_OWNED, "alone")                 # needs the harness and a leash
        assert suits(by_key["door_wait"], DEFAULT_OWNED | {"bondage_harness", "leash"}, "alone")

    def test_sensory_deprivation_is_short_and_never_with_cuffs_behind(self) -> None:
        from obedience.service import ALL_GEAR, compose
        for seed in range(400):
            task = compose(random.Random(seed), set(), set(), ALL_GEAR, "alone", back_blind_ok=True)
            if task is not None and "+deaf" in task.key:
                assert "cuffs_back" not in task.key and "gag" not in task.key
                assert int(task.text.rsplit(" ", 2)[-2]) <= 15

    def test_the_leash_safety_note(self) -> None:
        from obedience.service import safety_notes
        assert "never the collar" in safety_notes("Kneeling at the door, the leash from your harness waist ring to the door handle.")


class TestTemptations:
    def _t(self, db, *, rolls, blocked=False, avg=Decimal("0.25")):
        from temptations.service import Ports, Temptations

        class Rng(random.Random):
            def __init__(self, values):
                super().__init__(1)
                self.values = list(values)

            def random(self):
                return self.values.pop(0) if self.values else 0.99

        return Temptations(db, user_id=U, rng=Rng(rolls), ports=Ports(
            today=lambda now: now.date(), free_time=lambda now: 16 <= now.hour < 22, blocked=lambda now: blocked,
            avg_daily=lambda: avg, local_clock=lambda t: t.strftime("%H:%M")))

    def _rows(self, db, sql):
        with db.transaction() as tx:
            return [dict(r) for r in tx.fetch_all(sql)]

    def test_offered_never_showing_the_cost_and_accepted(self, tmp_path) -> None:
        from temptations.service import get_temptation_costs
        db = core(tmp_path)
        t = self._t(db, rolls=[0.0, 0.9])                                       # offered today, not free
        t.tick(NOW)
        offer = self._rows(db, "SELECT * FROM temptation_offers")[0]
        at = datetime.fromisoformat(offer["offer_at"].replace("Z", "+00:00"))
        t.tick(at)
        note = self._rows(db, "SELECT text, actions_json FROM notifications WHERE kind = 'temptation'")[0]
        assert "no cumming" in note["text"] and "0.05" not in note["text"] and "about 5 hours" in note["text"] and "Accept" in note["actions_json"]
        assert "Accepted" in t.press(user_id=U, row_id=offer["id"], action="accept", now=at + timedelta(minutes=10))
        assert get_temptation_costs(db, U, (at - timedelta(days=1)).isoformat(), (at + timedelta(days=1)).isoformat()) == Decimal("0.05")

    def test_free_ones_say_so_and_expired_or_blocked_cost_nothing(self, tmp_path) -> None:
        db = core(tmp_path)
        t = self._t(db, rolls=[0.0, 0.1])                                       # offered today, free
        t.tick(NOW)
        offer = self._rows(db, "SELECT * FROM temptation_offers")[0]
        at = datetime.fromisoformat(offer["offer_at"].replace("Z", "+00:00"))
        t.tick(at)
        assert "This one is free." in self._rows(db, "SELECT text FROM notifications WHERE kind = 'temptation'")[0]["text"]
        t.tick(at + timedelta(hours=1, minutes=5))
        assert self._rows(db, "SELECT status FROM temptation_offers")[0]["status"] == "expired"
        assert t.press(user_id=U, row_id=offer["id"], action="accept", now=at + timedelta(hours=2)) == "That offer is gone."
        db2 = core(tmp_path / "b")
        t2 = self._t(db2, rolls=[0.0, 0.9], blocked=True)
        t2.tick(NOW)
        offer2 = self._rows(db2, "SELECT * FROM temptation_offers")[0]
        t2.tick(datetime.fromisoformat(offer2["offer_at"].replace("Z", "+00:00")))
        assert self._rows(db2, "SELECT status FROM temptation_offers")[0]["status"] == "skipped"

    def test_most_days_have_none(self, tmp_path) -> None:
        db = core(tmp_path)
        t = self._t(db, rolls=[0.5, 0.5])
        t.tick(NOW)
        t.tick(NOW + timedelta(hours=1))
        assert self._rows(db, "SELECT status FROM temptation_offers") == [{"status": "skipped"}]

    def test_delay_words(self) -> None:
        from temptations.service import delay_words
        assert delay_words(Decimal("0.05"), Decimal("0.05")) == "At your pace this would push your next token back by about a day."
        assert delay_words(Decimal("0.05"), None) == "It would push your next token back a little."


def test_the_pool_takes_the_cost_but_never_goes_below_zero() -> None:
    from release_engine.token_pool import apply_day
    assert apply_day(Decimal("0.02"), 0, 0, 0, earning=False, cost=Decimal("0.05")).pool_after == Decimal(0)
    assert apply_day(Decimal("0.50"), 0, 0, 0, earning=False, cost=Decimal("0.05")).pool_after == Decimal("0.45")


def test_the_monthly_review_on_the_first_sunday() -> None:
    from weekly_review.service import MONTHLY_ASK, WeekFacts, render_week
    facts = WeekFacts(start=date(2026, 11, 30), end=date(2026, 12, 7),
                      month={"name": "November", "days": 28, "tokens": 3, "check_ins_ok": 40, "check_ins_due": 50})
    text = render_week(facts)
    assert "Your month (November): 3 tokens earned, 40 of 50 check-ins" in text and MONTHLY_ASK in text



def test_an_accepted_temptation_can_have_the_bot_run_the_deep_diver(tmp_path) -> None:
    """Owner, 2026-11-28: "controlled by the system in every single task having it"; caged edging said plainly (2026-11-25)."""
    from datetime import timedelta
    from application.models import OutgoingMessage
    from temptations.service import OFFER

    class Toy:
        def __init__(self):
            self.started, self.stopped = [], 0

        def start(self, minutes, mode, *, profile, base, on_problem, on_done, on_started):
            self.started.append((minutes, profile))
            on_started()
            return True

        def stop(self):
            self.stopped += 1
            return True

    assert "caged edging" in OFFER and "hands only around the cage" in OFFER
    db = core(tmp_path)
    helper = TestTemptations()
    t = helper._t(db, rolls=[0.0, 0.9])
    t.tick(NOW)
    offer = helper._rows(db, "SELECT * FROM temptation_offers")[0]
    now = datetime.fromisoformat(offer["offer_at"].replace("Z", "+00:00"))
    t.tick(now)
    row_id = offer["id"]
    told = []
    t.toy, t.toy_level, t.notify = Toy(), lambda: 0.6, lambda key, text, actions: told.append(text)
    reply = t.press(user_id=U, row_id=row_id, action="accept", now=now)
    assert isinstance(reply, OutgoingMessage) and reply.actions[0].action_id == f"temptation:toy:{row_id}"
    run = t.press(user_id=U, row_id=row_id, action="toy", now=now + timedelta(minutes=2))
    assert isinstance(run, OutgoingMessage) and t.toy.started == [(15, "tease")] and told and "Don't cum" in told[0]
    assert t.press(user_id=U, row_id=row_id, action="toystop", now=now) == "Stopped."
    assert t.press(user_id="other", row_id=row_id, action="toy", now=now) == "That button isn't for you."
