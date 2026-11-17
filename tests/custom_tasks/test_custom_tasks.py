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
