"""P1, the task board (owner, 2026-11-15): tasks posted on Monday with a deadline and a reward; one taken at a time; a taken task is a promise;
verified = the reward joins the token pool. Fake Chaster through the obedience World, no network."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from obedience.service import chaster_brief
from task_board.service import BOARD, OFFER_COUNT, WEEK_CAP, TaskBoard, get_board_rewards
from tests.obedience.test_obedience import U, World

MONDAY = datetime(2026, 11, 16, 9, 0, tzinfo=timezone.utc)


class Board:
    def __init__(self, tmp_path, *, owned=frozenset({"plug", "deep diver", "collar"})) -> None:
        from obedience.service import get_obedience_state, plan_board_task
        self.w = World(tmp_path)
        self.missed = []
        self.board = TaskBoard(self.w.core, user_id=U, owned=lambda: owned, free_time=lambda at: 8 <= at.hour < 22, today=lambda now: now.date(),
                               day_end=lambda d: datetime(d.year, d.month, d.day, 21, 45, tzinfo=timezone.utc), local_clock=lambda t: t.strftime("%a %H:%M"),
                               start_task=lambda row, now: plan_board_task(self.w.core, U, now.date(), row["task_key"], row["text"], now),
                               task_state=lambda ident: get_obedience_state(self.w.core, ident),
                               raise_missed=lambda row, now: self.missed.append(row["id"]))

    def rows(self):
        with self.w.core.transaction() as tx:
            return [dict(r) for r in tx.fetch_all("SELECT * FROM board_tasks ORDER BY created_at, rowid")]


def test_the_catalogue() -> None:
    assert OFFER_COUNT == 3 and WEEK_CAP == Decimal("0.10")
    for t in BOARD:
        text = t.text.lower()
        assert "show:" not in text and "code" not in text and ("only" in text or "no face" in text) and len(t.text) <= 200, t.key
        assert Decimal("0.03") <= t.reward <= Decimal("0.05") and 2 <= t.days <= 5


def test_posted_once_on_monday_in_free_time(tmp_path) -> None:
    b = Board(tmp_path)
    b.board.tick(MONDAY - timedelta(days=1))                     # Sunday
    b.board.tick(MONDAY.replace(hour=6))                         # Monday, not free time
    assert b.rows() == []
    b.board.tick(MONDAY)
    b.board.tick(MONDAY + timedelta(hours=1))
    assert len(b.rows()) == OFFER_COUNT and {r["status"] for r in b.rows()} == {"offered"}


def test_only_owned_gear(tmp_path) -> None:
    b = Board(tmp_path, owned=frozenset())
    for _ in range(5):
        b.board._rng.seed(_)
    b.board.tick(MONDAY)
    assert all(not next(t for t in BOARD if t.key == r["task_key"]).needs for r in b.rows())


def test_take_start_verify_and_the_reward(tmp_path) -> None:
    b = Board(tmp_path)
    b.board.tick(MONDAY)
    first, second = b.rows()[0], b.rows()[1]
    assert "promise" in b.board.press(user_id=U, row_id=first["id"], action="take", now=MONDAY)
    assert "One at a time" in b.board.press(user_id=U, row_id=second["id"], action="take", now=MONDAY)
    at = MONDAY + timedelta(hours=2)
    assert "Go." in b.board.press(user_id=U, row_id=first["id"], action="now", now=at)
    b.w.slot = False
    b.w.tasks.tick(at + timedelta(minutes=1))
    with b.w.core.transaction() as tx:
        task = dict(tx.fetch_one("SELECT * FROM obedience_tasks WHERE slot = 2"))
    assert task["status"] == "assigned" and task["text"] == first["text"] and chaster_brief(first["text"]) in b.w.chaster.assigned
    b.w.chaster.entries[task["task_id"]] = {"createdAt": (at + timedelta(minutes=20)).isoformat(), "peerVerification": {"status": "verified"}}
    b.w.tasks.tick(at + timedelta(minutes=25))
    b.board.tick(at + timedelta(minutes=26))
    row = next(r for r in b.rows() if r["id"] == first["id"])
    assert row["status"] == "passed"
    assert get_board_rewards(b.w.core, U, "2026-11-16T00:00:00Z", "2026-11-17T00:00:00Z") == Decimal(row["reward"])


def test_taken_and_never_started_is_a_miss_untaken_just_expires(tmp_path) -> None:
    b = Board(tmp_path)
    b.board.tick(MONDAY)
    taken = b.rows()[0]
    b.board.press(user_id=U, row_id=taken["id"], action="take", now=MONDAY)
    b.board.tick(MONDAY + timedelta(days=8))
    statuses = {r["id"]: r["status"] for r in b.rows()}
    assert statuses[taken["id"]] == "missed" and b.missed == [taken["id"]]
    assert all(s == "expired" for i, s in statuses.items() if i != taken["id"])


def test_the_weekly_cap(tmp_path) -> None:
    b = Board(tmp_path)
    b.board.tick(MONDAY)
    ids = [r["id"] for r in b.rows()]
    with b.w.core.transaction() as tx:
        tx.execute("UPDATE board_tasks SET reward = '0.05'")
        tx.execute("UPDATE board_tasks SET status = 'passed' WHERE id IN (?, ?)", (ids[0], ids[1]))
    assert "past +0.10" in b.board.press(user_id=U, row_id=ids[2], action="take", now=MONDAY)


def test_the_daily_evaluation_adds_the_reward() -> None:
    from system.daily_evaluation import DayFacts
    facts = DayFacts(goals_assigned=0, goals_met=0, check_ins_due=0, check_ins_ok=0, violations=0, board=Decimal("0.04"))
    assert facts.training_bonus == Decimal("0.04")


def test_fitness_never_on_a_training_day(tmp_path) -> None:
    b = Board(tmp_path)
    b.board._training_today = lambda now: True
    b.board.tick(MONDAY)
    with b.w.core.transaction() as tx:
        tx.execute("UPDATE board_tasks SET kind = 'fitness'")
    row = b.rows()[0]
    b.board.press(user_id=U, row_id=row["id"], action="take", now=MONDAY)
    assert "Not on a training day" in b.board.press(user_id=U, row_id=row["id"], action="now", now=MONDAY + timedelta(hours=1))


def test_at_most_one_erotic_from_the_obedience_catalogue(tmp_path) -> None:
    from obedience.service import ALONE
    from task_board.service import BoardTask, erotic_reward
    b = Board(tmp_path)
    picked = []

    def erotic(day):
        task = b.w.tasks._pick(day, ALONE)
        picked.append(task.key)
        return BoardTask(f"e:{task.key}", task.text, erotic_reward(task.key, task.needs), 4, "erotic", task.needs)

    b.board._erotic = erotic
    b.board._rng.random = lambda: 0.0
    b.board.tick(MONDAY)
    kinds = [r["kind"] for r in b.rows()]
    assert kinds.count("erotic") == 1 and len(kinds) == OFFER_COUNT and picked


def test_the_wiring_never_offers_a_dm_only_task(tmp_path) -> None:
    from system.obedience_wiring import build_task_board
    from schedule_windows.windows import WindowConfig
    w = World(tmp_path)
    cfg = WindowConfig.build(timezone_name="UTC", quiet_start="22:00", quiet_end="07:00", work_start="09:00", work_end="10:00",
                             holiday_country=None, weekend_release_start="10:00")
    board = build_task_board(w.core, w.tasks, cfg)
    for _ in range(30):
        task = board._erotic(MONDAY.date())
        assert task is None or (task.kind == "erotic" and "gag" not in task.key)
