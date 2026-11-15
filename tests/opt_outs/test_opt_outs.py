"""G3 (owner, 2026-11-15): while locked a switch-off waits 24 hours, then the keyholder asks; only a yes applies it."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from opt_outs.service import ANSWER_WINDOW, COOLING, OptOuts
from tests.lines.test_lines import core

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
U = "wearer"


class World:
    def __init__(self, tmp_path, locked=True) -> None:
        self.core = core(tmp_path)
        self.locked = locked
        self.applied: list[str] = []
        self.opt = OptOuts(self.core, locked=lambda u: self.locked, local_clock=lambda t: t.strftime("%H:%M"))
        for key in ("photo_check_ins", "conditioning", "weekly_review"):
            self.opt.register(key, lambda user, now, k=key: (self.applied.append(k), f"{k} off")[1])

    def row(self):
        with self.core.transaction() as tx:
            return dict(tx.fetch_one("SELECT * FROM opt_out_requests ORDER BY requested_at DESC LIMIT 1"))

    def ask_note(self):
        with self.core.transaction() as tx:
            return tx.fetch_one("SELECT text, actions_json FROM notifications WHERE kind = 'opt_out'")


def test_unlocked_applies_at_once(tmp_path) -> None:
    w = World(tmp_path, locked=False)
    assert w.opt.request_off(U, "photo_check_ins", NOW) is None


def test_locked_waits_then_a_yes_applies_it(tmp_path) -> None:
    w = World(tmp_path)
    reply = w.opt.request_off(U, "photo_check_ins", NOW)
    assert "isn't done at once" in reply and "never delayed" in reply and w.applied == []
    assert "already asked" in w.opt.request_off(U, "photo_check_ins", NOW)
    w.opt.tick(NOW + COOLING - timedelta(minutes=1))
    assert w.row()["status"] == "waiting"
    w.opt.tick(NOW + COOLING)
    note = w.ask_note()
    assert "still want it off" in note["text"] and "optout:yes:" in note["actions_json"]
    assert w.opt.press(user_id=U, row_id=w.row()["id"], action="yes", now=NOW + COOLING) == "photo_check_ins off"
    assert w.applied == ["photo_check_ins"] and w.row()["status"] == "applied"
    assert w.opt.press(user_id=U, row_id=w.row()["id"], action="yes", now=NOW + COOLING) == "That was already decided."


def test_keep_it_on_no_answer_or_switching_back_on_leave_it_on(tmp_path) -> None:
    w = World(tmp_path)
    w.opt.request_off(U, "conditioning", NOW)
    w.opt.tick(NOW + COOLING)
    assert w.opt.press(user_id=U, row_id=w.row()["id"], action="no", now=NOW + COOLING).startswith("Good.")
    later = NOW + timedelta(minutes=1)
    w.opt.request_off(U, "weekly_review", later)
    w.opt.tick(later + COOLING)
    w.opt.tick(later + COOLING + ANSWER_WINDOW)
    assert w.row()["status"] == "lapsed"
    w.opt.request_off(U, "photo_check_ins", NOW + timedelta(hours=1))
    assert w.opt.withdraw(U, "photo_check_ins", NOW + timedelta(hours=2)) and w.row()["status"] == "withdrawn"
    assert w.applied == []


def test_someone_else_cannot_press(tmp_path) -> None:
    w = World(tmp_path)
    w.opt.request_off(U, "conditioning", NOW)
    w.opt.tick(NOW + COOLING)
    assert w.opt.press(user_id="x", row_id=w.row()["id"], action="yes", now=NOW) == "That isn't yours to decide."
