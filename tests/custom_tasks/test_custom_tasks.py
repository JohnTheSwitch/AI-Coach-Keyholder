"""Another wearer's own erotic tasks (owner, 2026-11-17): their limits, tasks the owner writes from them, approved one by one by the wearer."""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone

from custom_tasks.service import NOT_YET, CustomTasks
from features.service import Features
from obedience.service import ALONE, CUSTOM_FALLBACK, DISCREET
from tests.obedience.test_obedience import U, World

NOW = datetime(2026, 11, 17, 15, 0, tzinfo=timezone.utc)


def _custom(w: World) -> CustomTasks:
    custom = CustomTasks(w.core, user_id=U)
    w.tasks.custom = custom
    return custom


def _notices(w: World) -> list[str]:
    with w.core.transaction() as tx:
        return [r["text"] for r in tx.fetch_all("SELECT text FROM notifications WHERE user_id = ? ORDER BY created_at", (U,))]


def test_limits_are_given_and_shown(tmp_path) -> None:
    custom = _custom(World(tmp_path))
    assert "not given yet" in custom.command(U, "limits", NOW)
    assert custom.command(U, "limits hard: no pain, nothing public", NOW) == "Saved. Hard limits (never): no pain, nothing public"
    custom.command(U, "Limits likes being tied", NOW)
    assert custom.limits() == {"hard": "no pain, nothing public", "soft": None, "likes": "being tied"}
    assert custom.command(U, "limits soft", NOW).startswith("Write it after the word")
    assert custom.command("someone else", "limits", NOW) is None and custom.command(U, "hello", NOW) is None


def test_a_task_is_given_only_after_the_wearer_approves_it(tmp_path) -> None:
    w = World(tmp_path)
    custom = _custom(w)
    assert w.tasks._pick(date(2026, 11, 18), ALONE) == CUSTOM_FALLBACK                  # nothing approved: never the owner's catalogue
    n = custom.propose(text="Kneel with your collar on for 10 minutes. Photo of the collar only.", needs=frozenset({"collar"}), now=NOW)
    assert any("`tasks`" in t for t in _notices(w))
    assert "1. Kneel with your collar on" in custom.command(U, "tasks", NOW) and "(only when you're alone)" in custom.command(U, "tasks", NOW)
    assert w.tasks._pick(date(2026, 11, 18), ALONE) == CUSTOM_FALLBACK                  # proposed is not approved
    assert custom.command(U, f"task approve {n}", NOW).startswith("Approved: 1")
    assert w.tasks._pick(date(2026, 11, 18), ALONE, owned=frozenset()) == CUSTOM_FALLBACK  # no collar: not given
    picked = w.tasks._pick(date(2026, 11, 18), ALONE, owned=frozenset({"collar"}))
    assert picked.key == "custom:1" and picked.text.startswith("Kneel with your collar")
    assert w.tasks._pick(date(2026, 11, 18), DISCREET, owned=frozenset({"collar"})) == CUSTOM_FALLBACK   # alone only
    assert custom.command(U, "task reject 1", NOW).startswith("Rejected: 1")              # consent taken back at once
    assert w.tasks._pick(date(2026, 11, 18), ALONE, owned=frozenset({"collar"})) == CUSTOM_FALLBACK
    assert custom.command(U, "task approve 1", NOW) == "There is no such task waiting."


def test_approve_all_and_no_repeat_while_another_is_left(tmp_path) -> None:
    w = World(tmp_path)
    custom = _custom(w)
    for text in ("Task one. Photo of the cage only.", "Task two. Photo of the cage only."):
        custom.propose(text=text, modes="both", now=NOW)
    assert custom.command(U, "task approve all", NOW) == "Approved: 1, 2. It can now be given to you."
    assert custom.command(U, "task reject all", NOW).startswith("Reject them one at a time")
    w.tasks._rng = random.Random(1)
    first = w.tasks._pick(date(2026, 11, 18), DISCREET)
    with w.core.transaction() as tx:
        tx.execute("INSERT INTO obedience_tasks (id, user_id, local_date, catalog_key, text, planned_at, status, created_at) "
                   "VALUES ('x', ?, '2026-11-18', ?, 't', ?, 'passed', ?)", (U, first.key, NOW.isoformat(), NOW.isoformat()))
    for seed in range(5):
        w.tasks._rng = random.Random(seed)
        assert w.tasks._pick(date(2026, 11, 19), ALONE).key != first.key


def test_erotic_tasks_wait_for_the_first_approved_task(tmp_path) -> None:
    w = World(tmp_path)
    custom = _custom(w)
    features = Features(w.core)
    features.unavailable["erotic_tasks"] = custom.not_yet
    assert features.command(U, "erotic tasks on", NOW) == NOT_YET and not features.is_on(U, "erotic_tasks")
    custom.propose(text="Task one. Photo of the cage only.", now=NOW)
    custom.command(U, "task approve 1", NOW)
    assert features.command(U, "erotic tasks on", NOW) in ("Erotic tasks is already on.", "Erotic tasks is on again.")
    assert features.is_on(U, "erotic_tasks")


def test_a_proposal_is_checked(tmp_path) -> None:
    custom = _custom(World(tmp_path))
    for kwargs in ({"text": " "}, {"text": "x" * 601}, {"text": "ok", "modes": "sometimes"}):
        try:
            custom.propose(now=NOW, **kwargs)
        except ValueError:
            continue
        raise AssertionError(kwargs)
    assert custom.tasks() == []


# ---------------------------------------------------------------------------- workouts and temptations (owner, 2026-11-17)

def _ex(key, name, unit="kg", start=20.0, step=2.5):
    return {"key": key, "name": name, "sets": 3, "low": 8, "high": 12, "start": start, "step": step, "unit": unit, "rest": "90 s"}


PLAN = {
    "exercises": [_ex("goblet", "Goblet squat"), _ex("lunge", "Walking lunge", "kg/hand", 8), _ex("pushup", "Push-up", "bw", 0, 0),
                  _ex("row", "Dumbbell row", "kg/hand", 12)],
    "sessions": {"legs_a": {"title": "Legs A", "exercises": ["goblet", "lunge"]}, "legs_a_short": {"title": "Legs A short", "exercises": ["goblet"]},
                 "legs_b": {"title": "Legs B", "exercises": ["lunge", "goblet"]}, "legs_b_short": {"title": "Legs B short", "exercises": ["lunge"]},
                 "upper": {"title": "Upper", "exercises": ["pushup", "row"]}, "upper_short": {"title": "Upper short", "exercises": ["pushup"]}},
    "proof": "Photo of your dumbbells only.",
}


def _plan(w: World, custom: CustomTasks):
    from training.service import TrainingPlan
    return TrainingPlan(w.core, user_id=U, free_time=lambda at: True, today=lambda at: at.date(), plan=custom.training_plan)


def test_their_training_plan_is_given_only_after_they_approve_it(tmp_path) -> None:
    w = World(tmp_path)
    custom = _custom(w)
    plan = _plan(w, custom)
    saturday = date(2026, 11, 21)
    assert plan.day_task(saturday) is None and custom.plan_not_yet() is not None                 # no plan: no session at all
    assert plan.log(U, "log squat 100 4x8", NOW) is None and plan.extra(U, "extra training", NOW) is None
    custom.propose_plan(PLAN, NOW)
    assert any("`training plan`" in t for t in _notices(w))
    shown = custom.command(U, "training plan", NOW)
    assert "waiting for your decision" in shown and "Goblet squat: 3 x 8-12, start 20 kg" in shown and "dumbbells" in shown
    assert plan.day_task(saturday) is None                                                        # proposed is not approved
    assert custom.command(U, "training plan approve", NOW).startswith("Approved")
    day = plan.day_task(saturday)
    assert day.key == "training:legs_a" and "Goblet squat" in day.details and "Back squat" not in day.details
    assert day.text == "Workout done: Legs A. Photo of your dumbbells only." and "`log goblet 20 3x8`" in day.details
    assert plan.log(U, "log goblet squat 20 12,12,12", NOW).text.startswith("Logged: Goblet squat. Every set")
    assert "Exercises: goblet" in plan.log(U, "log squat 100 4x8", NOW).text
    assert "slower" in plan.log(U, "log pushup 12,12,12", NOW).text and "vest" not in plan.log(U, "log pushup 12,12,12", NOW).text
    assert custom.command(U, "training plan reject", NOW).startswith("Rejected: your training plan is taken back")
    assert plan.day_task(date(2026, 11, 22)) is None and custom.plan_not_yet() is not None


def test_a_new_plan_replaces_the_old_only_when_approved(tmp_path) -> None:
    w = World(tmp_path)
    custom = _custom(w)
    custom.propose_plan(PLAN, NOW)
    custom.command(U, "training plan approve", NOW)
    second = {**PLAN, "proof": "Photo of the mat only."}
    custom.propose_plan(second, NOW)
    assert custom.training_plan().proof == "Photo of your dumbbells only." and "replace your current one" in custom.command(U, "training plan", NOW)
    assert custom.command(U, "training plan reject", NOW) == "Rejected: that plan will never be given to you."
    assert custom.training_plan().proof == "Photo of your dumbbells only."
    custom.propose_plan(second, NOW)
    custom.command(U, "training plan approve", NOW)
    assert custom.training_plan().proof == "Photo of the mat only."


def test_a_broken_plan_is_refused(tmp_path) -> None:
    custom = _custom(World(tmp_path))
    for bad in ({}, {**PLAN, "sessions": {}}, {**PLAN, "exercises": [{**_ex("goblet", "Goblet squat"), "unit": "stones"}]},
                {**PLAN, "proof": "x" * 81}):
        try:
            custom.propose_plan(bad, NOW)
        except ValueError:
            continue
        raise AssertionError(bad)
    assert custom.command(U, "training plan", NOW).startswith("No training plan")


def test_only_their_approved_temptations_are_offered(tmp_path) -> None:
    from decimal import Decimal
    from temptations.service import Ports, Temptations
    w = World(tmp_path)
    custom = _custom(w)
    texts = lambda: (lambda fits: fits[0].text if fits else None)(custom.temptations())
    tempt = Temptations(w.core, user_id=U, rng=random.Random(0), ports=Ports(
        today=lambda now: now.date(), free_time=lambda now: True, blocked=lambda now: False, avg_daily=lambda: Decimal("0.1"),
        local_clock=lambda t: t.strftime("%H:%M"), offer_text=texts))

    def offer_now(day: int) -> str | None:
        with w.core.transaction() as tx:
            tx.execute("INSERT INTO temptation_offers (id, user_id, local_date, offer_at, cost, status, created_at) VALUES (?, ?, ?, ?, '0', 'planned', ?)",
                       (f"o{day}", U, f"2026-11-{day}", NOW.isoformat(), NOW.isoformat()))
        tempt._offer(NOW)
        with w.core.transaction() as tx:
            return tx.fetch_one("SELECT status, text FROM temptation_offers WHERE id = ?", (f"o{day}",))
    custom.propose(text="Edge for 10 minutes, no cumming.", now=NOW, kind="temptation")
    assert offer_now(18)["status"] == "skipped"                                                   # proposed is not approved
    assert custom.temptation_not_yet() is not None and custom.not_yet() is not None
    custom.command(U, "task approve 1", NOW)
    assert custom.temptation_not_yet() is None and custom.not_yet() is not None                  # a temptation is never a daily task
    row = offer_now(19)
    assert row["status"] == "offered" and row["text"] == "Edge for 10 minutes, no cumming."
    assert any(t.startswith("Temptation: Edge for 10 minutes") and "Deep Diver" not in t for t in _notices(w))
    assert tempt.press(user_id=U, row_id="o19", action="accept", now=NOW) == "Accepted: Edge for 10 minutes, no cumming. If you slip, you tell me."
    assert w.tasks._pick(date(2026, 11, 20), ALONE) == CUSTOM_FALLBACK
    assert "Temptation: " in custom.command(U, "tasks", NOW) or "(temptation)" in custom.command(U, "tasks", NOW)
