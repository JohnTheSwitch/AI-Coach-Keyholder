"""Brat taming B3 (owner, 2026-11-14): during a Penalty Window a chore shown in Chaster (photo + code, community-checked) earns 2 hours off it.
Fake Chaster, fake Recovery Plan, no network."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from penalty_engine.models import PenaltyWindow, PenaltyWindowStatus
from penalty_engine.window import is_complete, remaining_active_hours
from recovery_tasks.service import CHORES, CREDIT_HOURS, OFFER_PAUSE, OFFER_VALID, TASK_DEADLINE, VOTE_LIMIT, Ports, RecoveryTasks
from tests.lines.test_lines import core

NOW = datetime(2026, 11, 14, 10, 0, tzinfo=timezone.utc)
U = "wearer"


class FakeChaster:
    def __init__(self) -> None:
        self.current, self.entries, self.assigned, self.unassigned = None, {}, [], 0

    def current_task(self, lock_id):
        return self.current

    def assign(self, lock_id, text):
        self.assigned.append(text)
        self.current = {"id": f"t{len(self.assigned)}"}
        return self.current["id"]

    def unassign(self, lock_id):
        self.unassigned += 1
        self.current = None

    def task_entry(self, lock_id, task_id):
        return self.entries.get(task_id)


class FakePlans:
    def __init__(self) -> None:
        self.calls, self.n, self.fail_complete = [], 0, False

    def propose_task(self, plan_id, title, description, credit_hours, *, now):
        self.n += 1
        self.calls.append(("propose", plan_id, title, credit_hours))
        return SimpleNamespace(id=f"rt{self.n}")

    def accept_task(self, task_id, *, now):
        self.calls.append(("accept", task_id))

    def withdraw_task(self, task_id, *, now):
        self.calls.append(("withdraw", task_id))

    def complete_task(self, task_id, *, now, notes=None):
        if self.fail_complete and task_id == "rt1":
            raise RuntimeError("expired by a regenerated plan")
        self.calls.append(("complete", task_id))


class World:
    def __init__(self, tmp_path, **ports) -> None:
        self.core = core(tmp_path)
        self.chaster, self.plans = FakeChaster(), FakePlans()
        self.window, self.spot = "w1", False
        base = dict(lock_id=lambda now: "lock", halted=lambda: False, paused=lambda now: False, quiet_between=lambda a, b: False,
                    active_window=lambda: self.window, plan_for=lambda wid: "plan-1", slot_taken=lambda: self.spot, local_clock=lambda t: t.strftime("%H:%M"))
        base.update(ports)
        self.tasks = RecoveryTasks(self.core, user_id=U, chaster=self.chaster, plans=self.plans, ports=Ports(**base), rng=lambda options: options[0])

    def rows(self, sql="SELECT * FROM recovery_chaster_tasks ORDER BY created_at, rowid"):
        with self.core.transaction() as tx:
            return [dict(r) for r in tx.fetch_all(sql)]

    def offered_id(self):
        return self.rows()[-1]["id"]


def test_an_offer_during_an_active_window_and_only_one_at_a_time(tmp_path) -> None:
    w = World(tmp_path)
    assert w.tasks.offer(NOW) is True and w.tasks.offer(NOW) is False
    [row] = w.rows()
    assert row["status"] == "offered" and row["chore"] == CHORES[0]
    assert w.plans.calls == [("propose", "plan-1", f"Show: {CHORES[0]}", CREDIT_HOURS)]
    [card] = w.rows("SELECT text, actions_json FROM notifications WHERE kind = 'recovery_task'")
    assert "Earn 2 hours off your Penalty Window" in card["text"] and "Take it" in card["actions_json"]


def test_no_offer_when_anything_says_no(tmp_path) -> None:
    for i, override in enumerate(({"active_window": lambda: None}, {"paused": lambda now: True}, {"halted": lambda: True},
                                  {"quiet_between": lambda a, b: True}, {"slot_taken": lambda: True}, {"lock_id": lambda now: None},
                                  {"plan_for": lambda wid: None})):
        (tmp_path / str(i)).mkdir()
        assert World(tmp_path / str(i), **override).tasks.offer(NOW) is False
    (tmp_path / "busy").mkdir()
    w = World(tmp_path / "busy")
    w.chaster.current = {"id": "the wearer's own task"}
    assert w.tasks.offer(NOW) is False


def test_take_assign_verify_credit(tmp_path) -> None:
    w = World(tmp_path)
    w.tasks.offer(NOW)
    reply = w.tasks.press(user_id=U, row_id=w.offered_id(), action="take", now=NOW)
    assert "It's in Chaster now" in reply and w.chaster.assigned == ["Your bathroom sink, cleaned."]
    assert ("accept", "rt1") in w.plans.calls
    w.chaster.entries["t1"] = {"createdAt": (NOW + timedelta(minutes=30)).isoformat(), "peerVerification": {"status": "ongoing"}}
    assert w.tasks.poll(NOW + timedelta(minutes=40)) == 0 and w.rows()[0]["status"] == "submitted"
    assert w.tasks.is_recovery_task("t1") and not w.tasks.is_recovery_task("other")
    w.chaster.entries["t1"]["peerVerification"]["status"] = "verified"
    assert w.tasks.poll(NOW + timedelta(hours=1)) == 1
    assert w.rows()[0]["status"] == "passed" and ("complete", "rt1") in w.plans.calls
    assert any("2 hours come off" in r["text"] for r in w.rows("SELECT text FROM notifications WHERE kind = 'recovery_task'"))


def test_a_regenerated_plan_never_loses_a_verified_chore(tmp_path) -> None:
    w = World(tmp_path)
    w.plans.fail_complete = True
    w.tasks.offer(NOW)
    w.tasks.press(user_id=U, row_id=w.offered_id(), action="take", now=NOW)
    w.chaster.entries["t1"] = {"createdAt": NOW.isoformat(), "peerVerification": {"status": "verified"}}
    w.tasks.poll(NOW + timedelta(minutes=5))
    assert ("complete", "rt2") in w.plans.calls and ("accept", "rt2") in w.plans.calls


def test_rejected_missed_or_late_earns_nothing_and_nothing_else(tmp_path) -> None:
    w = World(tmp_path)
    w.tasks.offer(NOW)
    w.tasks.press(user_id=U, row_id=w.offered_id(), action="take", now=NOW)
    assert w.tasks.poll(NOW + TASK_DEADLINE + timedelta(minutes=5)) == 1                # nothing submitted
    row = w.rows()[0]
    assert (row["status"], row["outcome"]) == ("failed", "missed") and w.chaster.unassigned == 1 and ("withdraw", "rt1") in w.plans.calls
    assert w.rows("SELECT 1 FROM detector_signals") == [] if _has_table(w, "detector_signals") else True
    assert w.tasks.offer(NOW + TASK_DEADLINE + timedelta(minutes=10)) is False                     # the pause after a failure
    assert w.tasks.offer(NOW + TASK_DEADLINE + OFFER_PAUSE + timedelta(minutes=10)) is True


def _has_table(w, name):
    return bool(w.rows(f"SELECT name FROM sqlite_master WHERE type = 'table' AND name = '{name}'"))


def test_a_vote_that_never_ends_earns_nothing(tmp_path) -> None:
    w = World(tmp_path)
    w.tasks.offer(NOW)
    w.tasks.press(user_id=U, row_id=w.offered_id(), action="take", now=NOW)
    w.chaster.entries["t1"] = {"createdAt": NOW.isoformat(), "peerVerification": {"status": "ongoing"}}
    w.tasks.poll(NOW + VOTE_LIMIT + timedelta(minutes=1))
    assert (w.rows()[0]["status"], w.rows()[0]["outcome"]) == ("cancelled", "unknown")


def test_not_now_expiry_and_a_finished_window(tmp_path) -> None:
    w = World(tmp_path)
    w.tasks.offer(NOW)
    assert w.tasks.press(user_id="someone", row_id=w.offered_id(), action="take", now=NOW) == "That isn't yours to decide."
    assert w.tasks.press(user_id=U, row_id=w.offered_id(), action="skip", now=NOW) == "Okay. I'll offer another one later."
    assert w.rows()[0]["status"] == "declined" and ("withdraw", "rt1") in w.plans.calls
    later = NOW + OFFER_PAUSE + timedelta(minutes=1)
    w.tasks.offer(later)
    w.tasks.offer(later + OFFER_VALID + timedelta(minutes=1))                         # the next pass expires the unanswered offer
    assert w.rows()[1]["status"] == "expired"
    (tmp_path / "over").mkdir()
    w2 = World(tmp_path / "over")
    w2.tasks.offer(NOW)
    w2.window = None
    assert "Penalty Window is over" in w2.tasks.press(user_id=U, row_id=w2.offered_id(), action="take", now=NOW) and w2.chaster.assigned == []


def test_credits_shorten_the_window() -> None:
    window = PenaltyWindow(created_at=NOW, status=PenaltyWindowStatus.ACTIVE, base_duration_hours=10.0, active_period_started_at=NOW,
                           max_target_hours=336.0)
    at = NOW + timedelta(hours=7)
    assert not is_complete(window, at) and remaining_active_hours(window, at) == 3.0
    credited = replace(window, recovery_credits_earned_hours=4.0)
    assert is_complete(credited, at) and remaining_active_hours(credited, at) == 0.0


def test_the_review_watch_leaves_recovery_photos_alone() -> None:
    from bot import discord_bot
    assert "checks.is_check_task(obs.task_id) or rec.is_recovery_task(obs.task_id)" in open(discord_bot.__file__, encoding="utf-8").read()
