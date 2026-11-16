"""Owner, 2026-11-16 ("learning what you avoid", option 1): an avoided task comes back more often; the health button never counts."""

from __future__ import annotations

import random
import uuid
from datetime import timedelta

from obedience.service import AVOID_WEIGHT, DEFAULT_OWNED, compose, task_parts
from tests.obedience.test_obedience import MORNING, U, World


def _row(w, day, key, status, outcome=None, postponed=False) -> None:
    with w.tasks._core.transaction() as tx:
        tx.execute("INSERT INTO obedience_tasks (id, user_id, local_date, catalog_key, text, planned_at, status, created_at, outcome, postponed_at) "
                   "VALUES (?, ?, ?, ?, 'x', ?, ?, ?, ?, ?)",
                   (str(uuid.uuid4()), U, day.isoformat(), key, MORNING.isoformat(), status, MORNING.isoformat(), outcome,
                    MORNING.isoformat() if postponed else None))


def test_task_parts() -> None:
    assert task_parts("pose:kneel+blind+cuffs") == {"pose:kneel", "addon:blind", "addon:cuffs"}
    assert task_parts("plug:deep_diver") == {"plug"} and task_parts("workout_squats") == {"workout"} and task_parts("dildo_kneel") == {"dildo_kneel"}


def test_avoided_parts_weigh_double_and_no_more() -> None:
    class Rng(random.Random):
        seen = None

        def choices(self, population, weights=None, k=1):
            Rng.seen = weights
            return super().choices(population, weights=weights, k=k)

    rng = Rng(1)
    some = compose(random.Random(1), set(), set(), DEFAULT_OWNED)
    part = sorted(task_parts(some.key))[1]
    compose(rng, set(), set(), DEFAULT_OWNED, avoided=frozenset({part}))
    assert set(Rng.seen) == {1.0, AVOID_WEIGHT}


def test_what_counts_as_avoided(tmp_path) -> None:
    w = World(tmp_path)
    day = MORNING.date()
    _row(w, day - timedelta(days=3), "dildo_kneel", "failed", "missed")
    _row(w, day - timedelta(days=4), "pose:kneel+blind", "passed", "verified", postponed=True)
    _row(w, day - timedelta(days=5), "plug:deep_diver", "cancelled", "health_or_safety", postponed=True)
    _row(w, day - timedelta(days=6), "workout_squats", "cancelled", "exemption")
    _row(w, day - timedelta(days=40), "workout_plank", "failed", "missed")             # older than AVOID_DAYS
    keys, parts = w.tasks._avoided(day)
    assert keys == {"dildo_kneel", "pose:kneel+blind"}
    assert parts == {"dildo_kneel", "pose:kneel", "addon:blind"}


def test_an_avoided_task_may_come_back_after_a_week(tmp_path) -> None:
    w = World(tmp_path)
    day = MORNING.date()
    _row(w, day - timedelta(days=8), "dildo_kneel", "failed", "missed")
    _row(w, day - timedelta(days=9), "dildo_ride", "passed", "verified")
    picks = {w.tasks._pick(day, "alone").key for _ in range(400)}
    assert "dildo_ride" not in picks                       # done: still within the 14 days
    assert "dildo_kneel" in picks                          # avoided: back after 7
