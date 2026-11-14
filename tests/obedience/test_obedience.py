"""Work queue Q4b (owner, 2026-11-14): one obedience task a day in free time, shown in Chaster; missed or rejected = asked to explain,
unexplained a Warning-level incident; a health or safety need cancels without consequence. Fake Chaster, no network."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from infractions.signals import EVIDENCE, RULE_GROUPS, SignalKind
from lines.service import sentence_for
from obedience.service import CATALOG, PHOTO_DEADLINE, Ports, ObedienceTasks
from penalty_engine.repository import WARNING_ONLY_RULE_GROUPS
from penalty_engine.violations_catalog import CATALOG as VIOLATIONS
from tests.lines.test_lines import core
from tests.recovery_tasks.test_recovery_tasks import FakeChaster

MORNING = datetime(2026, 11, 15, 9, 0, tzinfo=timezone.utc)
U = "wearer"


class World:
    def __init__(self, tmp_path, *, free=lambda at: 8 <= at.hour < 22, slot=False, paused=False) -> None:
        self.core = core(tmp_path)
        self.chaster, self.failures, self.slot = FakeChaster(), [], slot
        self.tasks = ObedienceTasks(self.core, user_id=U, chaster=self.chaster, rng=random.Random(4), ports=Ports(
            lock_id=lambda now: "lock", halted=lambda: False, paused=lambda now: paused, free_time=free, today=lambda now: now.date(),
            local_clock=lambda t: t.strftime("%H:%M"), slot_taken=lambda: self.slot,
            raise_failure=lambda row, reason, now: self.failures.append(reason)))

    def row(self):
        with self.core.transaction() as tx:
            return dict(tx.fetch_one("SELECT * FROM obedience_tasks ORDER BY created_at DESC LIMIT 1"))

    def due(self):
        return datetime.fromisoformat(self.row()["planned_at"].replace("Z", "+00:00"))


def _all_texts():
    from obedience.service import compose
    composed = {compose(random.Random(seed), set(), set()) for seed in range(300)}
    return list(CATALOG) + [t for t in composed if t is not None]


def test_the_catalogue_respects_the_limits() -> None:
    owned = ("plug", "deep diver", "dildo", "rope", "cuffed", "corner", "cage", "rack", "push-ups", "plank", "pull-ups", "workout", "thigh",
             "kneel", "bike", "treadmill", "calf", "cable", "wall", "fours", "face down", "blindfolded", "forehead")
    for task in _all_texts():
        text = task.text.lower()
        assert len(task.text) <= 200 and "code beside it" in text and task.text.startswith("Show: "), task.key
        assert "only" in text or "no face" in text, task.key                   # the photo names what it shows and only that
        assert not any(word in text for word in ("face visible", "outside", "outdoor", "public", "balls", " gag")), task.key
        assert any(word in text for word in owned), task.key
        if "rope" in text:
            assert "shears within reach" in text and "minutes" in text
        if "cuff" in text:
            assert "key within reach" in text
        if "blindfolded" in text:
            assert "rope" not in text and "cuff" not in text                    # never blindfolded in restraints
    assert {t.kind for t in CATALOG} == {"erotic", "workout"}


def test_dozens_of_combinations() -> None:
    assert len({t.key for t in _all_texts()}) >= 50


def test_a_missed_task_is_a_warning_level_incident_with_lines() -> None:
    assert RULE_GROUPS[SignalKind.OBEDIENCE_MISSED] == "OBEDIENCE_TASK_MISSED" and SignalKind.OBEDIENCE_MISSED in EVIDENCE
    assert "OBEDIENCE_TASK_MISSED" in WARNING_ONLY_RULE_GROUPS and any(v.code == "OBEDIENCE_TASK_MISSED" for v in VIOLATIONS)
    assert sentence_for("OBEDIENCE_TASK_MISSED") == "I obey my keyholder's tasks."


def test_planned_once_a_day_in_free_time_then_assigned_and_verified(tmp_path) -> None:
    w = World(tmp_path)
    w.tasks.tick(MORNING)
    w.tasks.tick(MORNING + timedelta(minutes=5))                          # never a second plan for the day
    due = w.due()
    assert MORNING < due and 8 <= due.hour < 22 and due + PHOTO_DEADLINE <= MORNING.replace(hour=22)
    assert w.chaster.assigned == []
    w.tasks.tick(due)
    row = w.row()
    assert row["status"] == "assigned" and w.chaster.assigned == [row["text"]]
    with w.core.transaction() as tx:
        note = tx.fetch_one("SELECT text, actions_json FROM notifications WHERE kind = 'obedience_task'")
    assert "Your task for today is in Chaster now" in note["text"] and "health or safety" in note["actions_json"]
    w.chaster.entries[row["task_id"]] = {"createdAt": (due + timedelta(minutes=20)).isoformat(), "peerVerification": {"status": "verified"}}
    w.tasks.tick(due + timedelta(minutes=25))
    assert w.row()["status"] == "passed" and w.failures == []


def test_missed_or_rejected_asks_for_an_explanation(tmp_path) -> None:
    w = World(tmp_path)
    w.tasks.tick(MORNING)
    due = w.due()
    w.tasks.tick(due)
    w.tasks.tick(due + PHOTO_DEADLINE + timedelta(minutes=5))
    assert (w.row()["status"], w.row()["outcome"]) == ("failed", "missed") and w.failures == ["missed"] and w.chaster.unassigned == 1


def test_a_health_or_safety_need_cancels_without_consequence(tmp_path) -> None:
    w = World(tmp_path)
    w.tasks.tick(MORNING)
    w.tasks.tick(w.due())
    reply = w.tasks.press(user_id=U, row_id=w.row()["id"], action="cannot", now=w.due() + timedelta(minutes=3))
    assert reply == "Cancelled, no consequence. Look after yourself first."
    assert w.row()["status"] == "cancelled" and w.failures == [] and w.chaster.unassigned == 1
    assert w.tasks.press(user_id="someone", row_id=w.row()["id"], action="cannot", now=MORNING) == "That isn't yours to decide."


def test_a_taken_slot_waits_and_the_day_is_skipped_when_free_time_ends(tmp_path) -> None:
    w = World(tmp_path, slot=True)
    w.tasks.tick(MORNING)
    w.tasks.tick(w.due())
    assert w.row()["status"] == "planned" and w.tasks.due_now(w.due())
    w.tasks.tick(MORNING.replace(hour=21, minute=30))                     # the photo deadline would reach past free time
    assert (w.row()["status"], w.row()["outcome"]) == ("skipped", "no_free_slot") and w.failures == []


def test_nothing_on_vacation_or_outside_free_time(tmp_path) -> None:
    w = World(tmp_path, paused=True)
    w.tasks.tick(MORNING)
    with w.core.transaction() as tx:
        assert tx.fetch_one("SELECT COUNT(*) AS n FROM obedience_tasks")["n"] == 0
    (tmp_path / "night").mkdir()
    w2 = World(tmp_path / "night")
    w2.tasks.tick(MORNING.replace(hour=23))
    with w2.core.transaction() as tx:
        assert tx.fetch_one("SELECT COUNT(*) AS n FROM obedience_tasks")["n"] == 0


def test_about_one_day_in_four_is_a_workout() -> None:
    import tempfile
    from pathlib import Path
    kinds = []
    with tempfile.TemporaryDirectory() as d:
        w = World(Path(d))
        for _ in range(200):
            kinds.append(w.tasks._pick(MORNING.date()).kind)
    share = kinds.count("workout") / len(kinds)
    assert 0.15 < share < 0.35


class TestPlugLadder:
    """Owner, 2026-11-14: training up to the Wolf Rimfire S, one rung after 3 verified wears, only plugs he owns."""

    _day = 0

    def _verify(self, w, key, n):
        with w.core.transaction() as tx:
            for _ in range(n):
                TestPlugLadder._day += 1
                i = TestPlugLadder._day
                tx.execute("INSERT INTO obedience_tasks (id, user_id, local_date, catalog_key, text, planned_at, status, created_at, outcome) "
                           "VALUES (?, ?, ?, ?, 't', ?, 'passed', ?, 'verified')", (f"{key}{i}", U, f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}", key,
                                                                                        MORNING.isoformat(), MORNING.isoformat()))

    def test_the_ladder_starts_small_and_moves_up_only_through_owned_plugs(self, tmp_path) -> None:
        from obedience.service import PLUG_LADDER
        assert [p.diameter_cm for p in PLUG_LADDER] == sorted(p.diameter_cm for p in PLUG_LADDER)
        w = World(tmp_path)
        first = w.tasks.ladder_task()
        assert first.key == "plug:deep_diver" and "20 minutes" in first.text and "water-based lube" in first.text and "Warm up" not in first.text
        self._verify(w, "plug:deep_diver", 2)
        assert w.tasks.ladder_task().key == "plug:deep_diver" and "45 minutes" in w.tasks.ladder_task().text
        self._verify(w, "plug:deep_diver_more", 0)
        with w.core.transaction() as tx:
            tx.execute("UPDATE obedience_tasks SET catalog_key = 'plug:deep_diver'")
        self._verify(w, "plug:deep_diver", 1)                              # now 3 verified wears
        assert w.tasks.ladder_task().key == "plug:bijou_large" and "Warm up with the gray Satisfyer Deep Diver" in w.tasks.ladder_task().text
        self._verify(w, "plug:bijou_large", 3)
        assert w.tasks.ladder_task().key == "plug:rimfire_s"                # the Gildo and the Plug & Joy are not owned yet: skipped


class TestWearExemption:
    def test_no_task_during_a_wear_exemption_and_an_open_one_is_cancelled(self, tmp_path) -> None:
        w = World(tmp_path)
        w.tasks.tick(MORNING)
        w.tasks.tick(w.due())
        exempt = {"on": True}
        w.tasks._ports = __import__("dataclasses").replace(w.tasks._ports, in_exemption=lambda: exempt["on"])
        w.tasks.tick(w.due() + timedelta(minutes=5))
        assert (w.row()["status"], w.row()["outcome"]) == ("cancelled", "exemption") and w.failures == [] and w.chaster.unassigned == 1
        (tmp_path / "x").mkdir()
        w2 = World(tmp_path / "x")
        w2.tasks._ports = __import__("dataclasses").replace(w2.tasks._ports, in_exemption=lambda: True)
        w2.tasks.tick(MORNING)
        with w2.core.transaction() as tx:
            assert tx.fetch_one("SELECT COUNT(*) AS n FROM obedience_tasks")["n"] == 0

    def test_a_hygiene_opening_is_not_a_wear_exemption(self, tmp_path) -> None:
        from tests.exemptions.test_hygiene_requests import World as HygieneWorld
        from tests.exemptions.test_exemptions import WEARER
        h = HygieneWorld(tmp_path)
        with h.service._core.transaction() as tx:
            tx.execute("INSERT INTO wear_exemptions (id, user_id, status, created_at, kind, minutes) VALUES ('h', ?, 'approved', ?, 'hygiene', 15)",
                       (WEARER, MORNING.isoformat()))
        assert h.service.in_force(WEARER) == "h" and h.service.wear_exemption_in_force(WEARER) is False
        with h.service._core.transaction() as tx:
            tx.execute("UPDATE wear_exemptions SET kind = 'exemption' WHERE id = 'h'")
        assert h.service.wear_exemption_in_force(WEARER) is True


class TestVariety:
    def test_nothing_repeats_within_two_weeks_and_no_gear_two_days_running(self, tmp_path) -> None:
        from obedience.service import REPEAT_DAYS, gear_of
        w = World(tmp_path)
        start = MORNING
        keys = []
        for d in range(REPEAT_DAYS):
            day = start + timedelta(days=d)
            w.tasks.tick(day)
            row = w.row()
            keys.append(row["catalog_key"])
            with w.core.transaction() as tx:                                  # close it so the next day can plan
                tx.execute("UPDATE obedience_tasks SET status = 'passed', outcome = 'verified', decided_at = ? WHERE id = ?",
                           (day.isoformat(), row["id"]))
        assert len(keys) == REPEAT_DAYS and len(set(k for k in keys if not k.startswith("plug:"))) == len([k for k in keys if not k.startswith("plug:")])
        for a, b in zip(keys, keys[1:]):
            assert not (gear_of(a) & gear_of(b)), (a, b)

    def test_it_hurt_last_time_so_one_rung_back(self, tmp_path) -> None:
        w = World(tmp_path)
        with w.core.transaction() as tx:
            for i in range(3):
                tx.execute("INSERT INTO obedience_tasks (id, user_id, local_date, catalog_key, text, planned_at, status, created_at, outcome, decided_at) "
                           "VALUES (?, ?, ?, 'plug:deep_diver', 't', ?, 'passed', ?, 'verified', ?)",
                           (f"d{i}", U, f"2026-10-0{i + 1}", MORNING.isoformat(), MORNING.isoformat(), f"2026-10-0{i + 1}T10:00:00Z"))
            tx.execute("INSERT INTO obedience_tasks (id, user_id, local_date, catalog_key, text, planned_at, status, created_at, outcome, decided_at) "
                       "VALUES ('b', ?, '2026-10-05', 'plug:bijou_large', 't', ?, 'cancelled', ?, 'health_or_safety', '2026-10-05T10:00:00Z')",
                       (U, MORNING.isoformat(), MORNING.isoformat()))
        assert w.tasks.ladder_task().key == "plug:deep_diver"


class TestNotAlone:
    """Owner, 2026-11-14: he does not live alone. One postponement of up to 2 hours, then the task is done or it fails."""

    def test_one_postponement_then_no_more(self, tmp_path) -> None:
        from obedience.service import POSTPONE
        w = World(tmp_path)
        w.tasks.tick(MORNING)
        due = w.due()
        w.tasks.tick(due)
        with w.core.transaction() as tx:
            note = tx.fetch_one("SELECT text, actions_json FROM notifications WHERE kind = 'obedience_task'")
        assert "door closed" in note["text"] and "Not alone right now" in note["actions_json"]
        at = due + timedelta(minutes=10)
        assert w.tasks.press(user_id=U, row_id=w.row()["id"], action="later", now=at).startswith("Postponed once.")
        assert w.row()["deadline"].startswith((at + POSTPONE).isoformat()[:16].replace("+00:00", ""))
        assert "already postponed" in w.tasks.press(user_id=U, row_id=w.row()["id"], action="later", now=at)
        w.tasks.tick(due + PHOTO_DEADLINE + timedelta(minutes=5))           # the old deadline passes: still open
        assert w.row()["status"] == "assigned" and w.failures == []
        w.tasks.tick(at + POSTPONE + timedelta(minutes=5))
        assert (w.row()["status"], w.failures) == ("failed", ["missed"])

    def test_no_postponement_past_free_time(self, tmp_path) -> None:
        w = World(tmp_path, free=lambda at: 8 <= at.hour < 21)
        w.tasks.tick(MORNING)
        w.tasks.tick(w.due())
        late = w.due().replace(hour=20, minute=0)
        assert "past your free time" in w.tasks.press(user_id=U, row_id=w.row()["id"], action="later", now=late)
