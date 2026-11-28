"""Work queue Q4c (owner, 2026-11-15): his training plan in the bot: the schedule, A/B alternation, the optional short session, the Monday
question, return-from-break, deload and progression from `log ...`."""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone

from tests.lines.test_lines import core
from tests.obedience.test_obedience import MORNING, U, World
from training.plan import EXERCISES
from training.service import DELOAD_EVERY_WEEKS, TrainingPlan

SATURDAY, SUNDAY, MONDAY, THURSDAY, FRIDAY = date(2026, 11, 21), date(2026, 11, 22), date(2026, 11, 16), date(2026, 11, 19), date(2026, 11, 20)


def plan(tmp_path, free=lambda at: 8 <= at.hour < 22):
    return TrainingPlan(core(tmp_path), user_id=U, free_time=free, today=lambda now: now.date())


def test_the_week(tmp_path) -> None:
    p = plan(tmp_path)
    assert p.day_task(FRIDAY) is None and p.day_task(date(2026, 11, 17)) is None          # no session Tuesday or Friday
    sat = p.day_task(SATURDAY)
    assert sat.key == "training:legs_a" and not sat.optional and "Back squat" in sat.details
    assert sat.text.startswith("Workout done:") and "Photo of the gym only." in sat.text and "code" not in sat.text and len(sat.text) <= 200
    sun = p.day_task(SUNDAY)
    assert sun.key == "training:upper" and "Warm-up: one set of bench with 60 kg." in sun.details
    assert p.day_task(SATURDAY + timedelta(days=7)).key == "training:legs_b"              # A and B alternate


def test_the_short_session_is_thursday_unless_he_picks_monday(tmp_path) -> None:
    p = plan(tmp_path)
    assert p.day_task(MONDAY) is None
    thu = p.day_task(THURSDAY)                                                             # ISO week 47: odd, so legs
    assert thu.optional and thu.key == "training:short:legs_a_short" and "optional" in thu.details
    p.tick(datetime(2026, 11, 23, 9, 0, tzinfo=timezone.utc))                              # next Monday, week 48 (even: upper)
    with p._core.transaction() as tx:
        row = tx.fetch_one("SELECT id FROM training_weeks")
        note = tx.fetch_one("SELECT text, actions_json FROM notifications WHERE kind = 'training'")
    assert "upper body" in note["text"] and "Today" in note["actions_json"]
    assert p.press(user_id=U, row_id=row["id"], action="mon", now=MORNING).startswith("Today")
    assert p.press(user_id=U, row_id=row["id"], action="thu", now=MORNING) == "That's already decided for this week."
    assert p.day_task(date(2026, 11, 23)).key == "training:short:upper_short" and p.day_task(date(2026, 11, 26)) is None


def test_back_after_a_break_and_deload(tmp_path) -> None:
    p = plan(tmp_path)
    first = p.day_task(SATURDAY)
    assert "Back squat: 3 x 8-10, 95 kg" in first.details and "back after a break" in first.details
    p.day_task(SATURDAY + timedelta(days=7))
    third = p.day_task(SATURDAY + timedelta(days=14))
    assert "Back squat: 4 x 8-10, 105 kg" in third.details
    deload = p.day_task(SATURDAY + timedelta(days=7 * (DELOAD_EVERY_WEEKS - 1)))
    assert "deload week" in deload.details


def test_progression_from_logs(tmp_path) -> None:
    p = plan(tmp_path)
    assert p.log(U, "log squat 105 4x9", MORNING).text == "Logged: Back squat. Stay at this weight until every set reaches 10."
    assert p.weight("squat") == 105
    assert "next time 107.5 kg" in p.log(U, "log squat 105 10,10,10,10", MORNING).text
    assert p.weight("squat") == 107.5
    assert "next time 22.5 per hand" in p.log(U, "log incline 20 12,12,12", MORNING).text
    assert "make it harder" in p.log(U, "log pullups 8,8,8", MORNING).text
    assert "How much weight?" in p.log(U, "log bench 3x8", MORNING).text
    assert "couldn't read" in p.log(U, "log something odd", MORNING).text
    assert p.log(U, "hello", MORNING) is None and p.log("someone", "log squat 100 4x9", MORNING) is None
    p.day_task(SATURDAY)
    p.day_task(SATURDAY + timedelta(days=7))                                               # the two return-from-break leg days
    assert "Back squat: 4 x 8-10, 107.5 kg" in p.day_task(SATURDAY + timedelta(days=14)).details


def test_the_session_is_the_days_obedience_task_and_the_short_one_is_optional(tmp_path) -> None:
    import dataclasses
    w = World(tmp_path)
    tp = TrainingPlan(w.core, user_id=U, free_time=lambda at: 8 <= at.hour < 22, today=lambda now: now.date())
    w.tasks._ports = dataclasses.replace(w.tasks._ports, training=tp.day_task, training_tick=tp.tick)
    sat = datetime(2026, 11, 21, 9, 0, tzinfo=timezone.utc)
    w.tasks.tick(sat)
    row = w.row()
    assert row["catalog_key"] == "training:legs_a" and row["details"] and w.due() == sat + timedelta(minutes=15)
    w.tasks.tick(w.due())
    row = w.row()
    assert row["status"] == "assigned" and row["deadline"] >= "2026-11-21T21:"            # due by the end of free time
    with w.core.transaction() as tx:
        note = tx.fetch_one("SELECT text FROM notifications WHERE kind = 'obedience_task'")
    assert note["text"].startswith("Today's training:") and "Back squat" in note["text"]
    (tmp_path / "thu").mkdir()
    w2 = World(tmp_path / "thu")
    tp2 = TrainingPlan(w2.core, user_id=U, free_time=lambda at: 8 <= at.hour < 22, today=lambda now: now.date())
    w2.tasks._ports = dataclasses.replace(w2.tasks._ports, training=tp2.day_task)
    thu = datetime(2026, 11, 19, 9, 0, tzinfo=timezone.utc)
    w2.tasks.tick(thu)
    w2.tasks.tick(w2.due())
    w2.tasks.tick(thu.replace(hour=23))                                                    # never sent: skipped, no consequence
    assert w2.row()["status"] == "skipped" and w2.failures == []


def test_every_exercise_is_complete() -> None:
    for ex in EXERCISES.values():
        assert ex.low <= ex.high and ex.sets >= 2 and ex.unit in ("kg", "kg/hand", "plates", "bw")
        assert (ex.step == 0) == (ex.unit == "bw")
    assert random  # imported for parity with the obedience tests


def test_the_exercise_key_with_an_underscore_and_the_cable_steps(tmp_path) -> None:
    """Bug fixes 2026-11-16: `log cable_row` was not understood; the triceps cable stack moves in 4 kg steps."""
    from training.plan import EXERCISES
    assert EXERCISES["triceps"].step == 4 and EXERCISES["cable_row"].step == 4
    from training.plan import ALIASES
    from training.service import _LOG
    name = _LOG.match("log cable_row 34 12,12").group("name").strip().lower()
    assert (ALIASES.get(name) or ALIASES.get(name.replace("_", " ")) or (name if name in EXERCISES else None)) == "cable_row"



def test_an_extra_session_on_demand(tmp_path) -> None:
    """Owner, 2026-11-16: "I've already done the 3 this week but I have time today, so I can do the fourth"."""
    from training.service import get_planned_sessions
    p = plan(tmp_path)
    tue = datetime(2026, 11, 17, 9, 0, tzinfo=timezone.utc)                                 # week 47: odd, the short session is legs
    assert p.extra(U, "hello", tue) is None and p.extra("someone", "extra training", tue) is None
    reply = p.extra(U, "Extra training", tue)
    assert "upper body" in reply.text
    task = p.day_task(date(2026, 11, 17))
    assert task.optional and task.key == "training:short:upper_short"
    assert "One a week" in p.extra(U, "extra workout", datetime(2026, 11, 18, 9, 0, tzinfo=timezone.utc)).text
    assert p.day_task(THURSDAY).key == "training:short:legs_a_short"                         # the week's own short session stays
    assert "2026-11-17" not in get_planned_sessions(p._core, U, "2026-11-16", "2026-11-22")  # never part of the week bonus
    assert "2026-11-19" in get_planned_sessions(p._core, U, "2026-11-16", "2026-11-22")


def test_an_extra_session_is_never_legs_too_close_to_a_leg_day(tmp_path) -> None:
    p = plan(tmp_path)
    p.day_task(SATURDAY)                                                                   # legs on Saturday 21 Nov
    assert "legs" in p.extra(U, "extra training", datetime(2026, 11, 24, 9, 0, tzinfo=timezone.utc)).text
    p2 = plan(tmp_path / "b")
    p2.day_task(date(2026, 11, 28))                                                        # ...but not on the Friday before the leg day
    assert "upper body" in p2.extra(U, "extra training", datetime(2026, 12, 4, 9, 0, tzinfo=timezone.utc)).text


def test_no_extra_session_on_a_training_day_or_when_todays_task_is_out(tmp_path) -> None:
    p = plan(tmp_path)
    assert "already have a session" in p.extra(U, "extra training", datetime(2026, 11, 21, 9, 0, tzinfo=timezone.utc)).text
    p.today_open = lambda now: False
    assert "already out" in p.extra(U, "extra training", datetime(2026, 11, 17, 9, 0, tzinfo=timezone.utc)).text
    assert p.day_task(date(2026, 11, 17)) is None


def test_a_moved_session_keeps_the_extra_free(tmp_path) -> None:
    """Owner, 2026-11-16: Monday's short leg session moved to Tuesday must not use up the week's `extra training`."""
    from training.service import get_planned_sessions
    p = plan(tmp_path)
    with p._core.transaction() as tx:
        tx.execute("INSERT INTO training_weeks (id, user_id, iso_week, short_day, asked_at, moved_date, moved_kind) "
                   "VALUES ('w', ?, '2026-W47', 'mon', NULL, '2026-11-17', 'legs')", (U,))
    assert p.day_task(date(2026, 11, 17)).key == "training:short:legs_a_short"
    assert "2026-11-17" in get_planned_sessions(p._core, U, "2026-11-16", "2026-11-22")     # still one of the week's sessions
    assert "upper body" in p.extra(U, "extra training", datetime(2026, 11, 18, 9, 0, tzinfo=timezone.utc)).text


def test_a_leg_day_he_never_did_does_not_count(tmp_path) -> None:
    """Live, 2026-11-28: short A done on 17 Nov, B skipped on 21 Nov (unlocked, no free slot). Saturday 28 Nov came as a full "Leg day A"
    instead of B, and without the return-from-break loads, because the skipped B counted as done."""
    p = plan(tmp_path)
    done = {"2026-11-14"}
    p.done_on = lambda d: d in done
    first = p.day_task(date(2026, 11, 14))                                                 # legs A, done
    assert first.key == "training:legs_a" and "back after a break" in first.details
    second = p.day_task(SATURDAY)                                                          # legs B, never done
    assert second.key == "training:legs_b" and "back after a break" in second.details
    third = p.day_task(SATURDAY + timedelta(days=7))
    assert third.key == "training:legs_b" and "back after a break" in third.details        # B again, still the 2nd leg day back
    done.add((SATURDAY + timedelta(days=7)).isoformat())
    fourth = p.day_task(SATURDAY + timedelta(days=14))
    assert fourth.key == "training:legs_a" and "back after a break" not in fourth.details


def test_a_leg_day_recorded_as_the_other_one(tmp_path) -> None:
    """Owner, 2026-11-28: the task went out as A, he did B: recorded as B once, so the next Saturday is A."""
    p = plan(tmp_path)
    p.done_on = lambda d: True
    p.day_task(SATURDAY)                                                                  # A
    assert p.correct_session(SATURDAY, "legs_b", reason="t", now=datetime(2026, 11, 21, 12, tzinfo=timezone.utc))
    assert not p.correct_session(SATURDAY, "legs_b", reason="t", now=datetime(2026, 11, 21, 12, tzinfo=timezone.utc))
    assert not p.correct_session(SUNDAY, "legs_b", reason="t", now=datetime(2026, 11, 21, 12, tzinfo=timezone.utc))   # upper: never
    assert p.day_task(SATURDAY + timedelta(days=7)).key == "training:legs_a"
