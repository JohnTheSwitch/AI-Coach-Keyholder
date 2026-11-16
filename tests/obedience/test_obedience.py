"""Work queue Q4b (owner, 2026-11-14): one obedience task a day in free time, shown in Chaster; missed or rejected = asked to explain,
unexplained a Warning-level incident; a health or safety need cancels without consequence. Fake Chaster, no network."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from infractions.signals import EVIDENCE, RULE_GROUPS, SignalKind
from lines.service import sentence_for
from obedience.service import CATALOG, GEAR, PHOTO_DEADLINE, Ports, ObedienceTasks, chaster_brief
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

    def start(self, at=None, answer="alone"):
        """The task's moment comes, he is asked whether he is alone, and he answers (owner, 2026-11-15)."""
        at = at or self.due()
        self.tasks.tick(at)
        return self.tasks.press(user_id=U, row_id=self.row()["id"], action=answer, now=at)


def _all_texts():
    from obedience.service import compose
    composed = {compose(random.Random(seed), set(), set(), mode=mode) for seed in range(300) for mode in ("alone", "discreet")}
    return list(CATALOG) + [t for t in composed if t is not None]


def test_the_catalogue_respects_the_limits() -> None:
    owned = ("plug", "deep diver", "dildo", "rope", "cuff", "corner", "cage", "rack", "push-ups", "plank", "pull-ups", "workout", "thigh",
             "kneel", "bike", "treadmill", "calf", "cable", "wall", "fours", "face down", "blindfolded", "forehead", "collar", "clamps", "bijou",
             "eyes closed", "harness")
    for task in _all_texts():
        text = task.text.lower()
        # Owner, 2026-11-15: voters see this, so brief: no "Show:", no mention of the code.
        assert len(task.text) <= 200 and "show:" not in text and "code" not in text and task.text[:1] == task.text[:1].upper(), task.key
        assert "only" in text or "no face" in text, task.key                   # the photo names what it shows and only that
        assert not any(word in text for word in ("face visible", "outside", "outdoor", "public", "balls", " gag")), task.key
        assert any(word in text for word in owned), task.key
        if "rope" in text:
            assert "shears within reach" in text and "minutes" in text
        if "cuff" in text:
            assert any(w in text for w in ("key within reach", "buckled", "fluffy", "bondage harness")), task.key   # a release he can reach
        if "clamps" in text:
            assert "10 minutes" in text
        if "blindfolded" in text:                                               # owner, 2026-11-15: cuffs in front only, never rope on limbs
            assert " tied" not in text and "ankles" not in text and "clipped behind" not in text, task.key
            if "cuff" in text or "clipped" in text:
                assert "in front" in text, task.key
            if "rope" in text:
                assert "chest harness" in text, task.key
    assert {t.kind for t in CATALOG} == {"erotic", "workout"}


def test_dozens_of_combinations() -> None:
    assert len({t.key for t in _all_texts()}) >= 50


def test_a_missed_task_is_a_warning_level_incident_with_lines() -> None:
    assert RULE_GROUPS[SignalKind.OBEDIENCE_MISSED] == "OBEDIENCE_TASK_MISSED" and SignalKind.OBEDIENCE_MISSED in EVIDENCE
    assert "OBEDIENCE_TASK_MISSED" in WARNING_ONLY_RULE_GROUPS and any(v.code == "OBEDIENCE_TASK_MISSED" for v in VIOLATIONS)
    assert sentence_for("OBEDIENCE_TASK_MISSED") == "I obey my keyholder without excuses."


def test_planned_once_a_day_in_free_time_then_assigned_and_verified(tmp_path) -> None:
    w = World(tmp_path)
    w.tasks.tick(MORNING)
    w.tasks.tick(MORNING + timedelta(minutes=5))                          # never a second plan for the day
    due = w.due()
    assert MORNING < due and 8 <= due.hour < 22 and due + PHOTO_DEADLINE <= MORNING.replace(hour=22)
    assert w.chaster.assigned == []
    w.tasks.tick(due)
    assert w.row()["status"] == "asking" and w.chaster.assigned == []
    assert w.tasks.press(user_id=U, row_id=w.row()["id"], action="alone", now=due) == "Alone: the full version. It's in Chaster now."
    row = w.row()
    assert row["status"] == "assigned" and row["mode"] == "alone" and w.chaster.assigned == [chaster_brief(row["text"])]
    with w.core.transaction() as tx:
        note = tx.fetch_one("SELECT text, actions_json FROM notifications WHERE dedupe_key LIKE 'obedience:task:%'")
    assert "Your task for today is in Chaster now (the full version" in note["text"] and "health or safety" in note["actions_json"]
    w.chaster.entries[row["task_id"]] = {"createdAt": (due + timedelta(minutes=20)).isoformat(), "peerVerification": {"status": "verified"}}
    w.tasks.tick(due + timedelta(minutes=25))
    assert w.row()["status"] == "passed" and w.failures == []


def test_missed_or_rejected_asks_for_an_explanation(tmp_path) -> None:
    w = World(tmp_path)
    w.tasks.tick(MORNING)
    due = w.due()
    w.start(due)
    w.tasks.tick(datetime.fromisoformat(w.row()["deadline"].replace("Z", "+00:00")) + timedelta(minutes=5))
    assert (w.row()["status"], w.row()["outcome"]) == ("failed", "missed") and w.failures == ["missed"] and w.chaster.unassigned == 1


def test_a_health_or_safety_need_cancels_without_consequence(tmp_path) -> None:
    w = World(tmp_path)
    w.tasks.tick(MORNING)
    w.start()
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
        w.start()
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
            w.start()
            row = w.row()
            keys.append(row["catalog_key"])
            with w.core.transaction() as tx:                                  # close it so the next day can plan
                tx.execute("UPDATE obedience_tasks SET status = 'passed', outcome = 'verified', decided_at = ? WHERE id = ?",
                           (day.isoformat(), row["id"]))
            w.chaster.current = None
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
        w.start(due)
        with w.core.transaction() as tx:
            note = tx.fetch_one("SELECT text, actions_json FROM notifications WHERE dedupe_key LIKE 'obedience:task:%'")
        assert "door closed" in note["text"] and "Someone's around" in note["actions_json"]
        at = due + timedelta(minutes=10)
        assert w.tasks.press(user_id=U, row_id=w.row()["id"], action="later", now=at).startswith("Postponed once.")
        assert w.row()["deadline"] >= (at + POSTPONE).isoformat()[:16].replace("+00:00", "")
        assert "already postponed" in w.tasks.press(user_id=U, row_id=w.row()["id"], action="later", now=at)
        w.tasks.tick(due + PHOTO_DEADLINE + timedelta(minutes=5))           # the old deadline passes: still open
        assert w.row()["status"] == "assigned" and w.failures == []
        w.tasks.tick(datetime.fromisoformat(w.row()["deadline"].replace("Z", "+00:00")) + timedelta(minutes=5))
        assert (w.row()["status"], w.failures) == ("failed", ["missed"])

    def test_no_postponement_past_free_time(self, tmp_path) -> None:
        w = World(tmp_path, free=lambda at: 8 <= at.hour < 21)
        w.tasks.tick(MORNING)
        w.start()
        late = w.due().replace(hour=20, minute=0)
        assert "past your free time" in w.tasks.press(user_id=U, row_id=w.row()["id"], action="later", now=late)


class TestGearTags:
    """Owner, 2026-11-15: every task names the gear it needs, with variations when a piece is missing; only gear he owns is used."""

    def test_only_owned_gear_and_variations_when_a_piece_is_missing(self, tmp_path) -> None:
        from obedience.service import DEFAULT_OWNED, EXTRAS, GEAR
        w = World(tmp_path)
        assert w.tasks.owned_gear() == DEFAULT_OWNED and "blindfold" not in DEFAULT_OWNED
        assert all(t.needs <= set(GEAR) for t in EXTRAS)
        texts = []
        for _ in range(400):
            for mode in ("alone", "discreet"):
                task = w.tasks._pick(MORNING.date(), mode)
                assert task.needs <= DEFAULT_OWNED, task.key
                texts.append(task.text.lower())
        assert not any(w_ in t for t in texts for w_ in ("blindfolded", "collar", "clamps", "leather cuffs", "gildo", "plug & joy"))
        assert any("eyes closed" in t for t in texts)                   # the blindfold's variation keeps those tasks alive

    def test_new_gear_unlocks_its_tasks(self, tmp_path) -> None:
        w = World(tmp_path)
        assert w.tasks.gear_command(U, "gear add blindfold", MORNING) == "Added: blindfold. Tasks that need it can come now."
        assert w.tasks.gear_command(U, "gear add nipple clamps", MORNING).startswith("Added: nipple clamps")
        assert w.tasks.gear_command(U, "gear add gildo", MORNING).startswith("Added: glass Gildo")
        owned = w.tasks.owned_gear()
        assert {"blindfold", "clamps", "gildo_27"} <= owned
        keys = {w.tasks._pick(MORNING.date(), "alone").key for _ in range(600)}
        assert {"blind_plug", "clamps_chest", "clamps_vibe"} & keys
        listing = w.tasks.gear_command(U, "gear", MORNING)
        assert "Your gear:" in listing and "Tasks waiting for:" in listing and "collar" in listing.split("Tasks waiting for:")[1]
        assert w.tasks.gear_command(U, "gear remove blindfold", MORNING).startswith("Removed: blindfold")
        assert "blindfold" not in w.tasks.owned_gear()

    def test_the_ladder_uses_plugs_from_his_gear_list(self, tmp_path) -> None:
        w = World(tmp_path)
        TestPlugLadder()._verify(w, "plug:deep_diver", 3)
        TestPlugLadder()._verify(w, "plug:bijou_large", 3)
        assert w.tasks.ladder_task().key == "plug:rimfire_s"
        w.tasks.gear_command(U, "gear add gildo", MORNING)
        assert w.tasks.ladder_task().key == "plug:gildo_27"

    def test_the_command_is_his_and_only_for_gear(self, tmp_path) -> None:
        w = World(tmp_path)
        assert w.tasks.gear_command("someone", "gear add collar", MORNING) is None
        assert w.tasks.gear_command(U, "gearbox", MORNING) is None and w.tasks.gear_command(U, "hello", MORNING) is None
        assert w.tasks.gear_command(U, "gear add spaceship", MORNING).startswith("I don't know that piece.")
        assert w.tasks.gear_command(U, "gear buy collar", MORNING).startswith("Use `gear`")

    def test_safety_notes_follow_the_gear(self) -> None:
        from obedience.service import safety_notes
        assert "never tied, clipped or locked to anything" in safety_notes("Show: kneeling in your collar")
        assert "never rope on your limbs while blindfolded" in safety_notes("Show: wrists in the leather cuffs")
        assert "never to the collar ring" in safety_notes("Show: in the bondage harness, wrists clipped behind you")
        assert "release lever" in safety_notes("Show: kneeling, fluffy cuffs behind your back, lever tested first")
        assert "10 minutes at most" in safety_notes("Show: clamps on your nipples")
        assert safety_notes("Show: on all fours") == ""


class TestAloneQuestion:
    """Owner, 2026-11-15: before the task the bot asks whether he is alone; alone = full version, not alone = discreet; both mandatory."""

    def test_discreet_tasks_are_quiet_short_and_single(self, tmp_path) -> None:
        import re
        from obedience.service import ALONE_ONLY
        w = World(tmp_path)
        w.tasks.gear_command(U, "gear add blindfold", MORNING)
        w.tasks.gear_command(U, "gear add leather cuffs", MORNING)
        w.tasks.gear_command(U, "gear add padlocks", MORNING)
        for _ in range(400):
            task = w.tasks._pick(MORNING.date(), "discreet")
            assert task.modes != ALONE_ONLY, task.key
            if task.key.startswith("pose:"):
                assert task.key.count("+") == 1 and int(re.search(r"(\d+) minutes", task.text).group(1)) <= 15, task.text
                assert "deep diver in and vibrating" not in task.text.lower()

    def test_no_answer_means_the_discreet_version(self, tmp_path) -> None:
        from obedience.service import ASK_WAIT
        w = World(tmp_path)
        w.tasks.tick(MORNING)
        due = w.due()
        w.tasks.tick(due)
        with w.core.transaction() as tx:
            ask = tx.fetch_one("SELECT text, actions_json FROM notifications WHERE dedupe_key LIKE 'obedience:ask:%'")
        assert "Are you alone right now?" in ask["text"] and "I'm alone" in ask["actions_json"] and "Not alone" in ask["actions_json"]
        w.tasks.tick(due + ASK_WAIT - timedelta(minutes=1))
        assert w.row()["status"] == "asking"
        w.tasks.tick(due + ASK_WAIT)
        assert (w.row()["status"], w.row()["mode"]) == ("assigned", "discreet") and w.row()["catalog_key"] != "ask"
        # Owner, 2026-11-16 22:40: a late "I'm alone" swaps the discreet task for the full version while no photo is in
        first = w.row()["task_id"]
        assert w.tasks.press(user_id=U, row_id=w.row()["id"], action="alone", now=due + ASK_WAIT).startswith("Alone: the full version instead.")
        assert (w.row()["status"], w.row()["mode"]) == ("assigned", "alone") and w.row()["task_id"] != first
        with w.core.transaction() as tx:
            assert tx.fetch_one("SELECT 1 FROM notifications WHERE dedupe_key LIKE 'obedience:task:%:full'") is not None
        assert w.tasks.press(user_id=U, row_id=w.row()["id"], action="alone", now=due + ASK_WAIT) == "Already answered."

    def test_not_alone_answer_and_a_second_answer(self, tmp_path) -> None:
        w = World(tmp_path)
        w.tasks.tick(MORNING)
        assert w.start(answer="discreet") == "Not alone: a discreet version. It's in Chaster now."
        assert w.row()["mode"] == "discreet"
        assert w.tasks.press(user_id=U, row_id=w.row()["id"], action="alone", now=w.due()).startswith("Alone: the full version instead.")
        assert w.tasks.press(user_id=U, row_id=w.row()["id"], action="discreet", now=w.due()) == "Already answered."   # never back down

    def test_cannot_while_being_asked_and_an_exemption_cancels_the_question(self, tmp_path) -> None:
        w = World(tmp_path)
        w.tasks.tick(MORNING)
        w.tasks.tick(w.due())
        assert w.tasks.press(user_id=U, row_id=w.row()["id"], action="cannot", now=w.due()).startswith("Cancelled, no consequence.")
        assert w.row()["status"] == "cancelled" and w.chaster.assigned == []

    def test_the_question_waits_for_a_free_slot(self, tmp_path) -> None:
        w = World(tmp_path, slot=True)
        w.tasks.tick(MORNING)
        w.tasks.tick(w.due())
        assert w.row()["status"] == "planned"                               # no question while a spot check holds the slot
        w.slot = False
        w.tasks.tick(w.due() + timedelta(minutes=10))
        assert w.row()["status"] == "asking" and w.tasks.has_open_task()


class TestLimitsTalk:
    """Owner, 2026-11-15: cuffed + blindfolded + stimulated, behind-back fluffy cuffs, longer wear, gag told only by the bot, the harness."""

    def test_the_gag_is_never_in_chaster_only_in_the_dm(self, tmp_path) -> None:
        import random as r
        from obedience.service import GAG_NOTE, compose
        gagged = [t for t in (compose(r.Random(s), set(), set()) for s in range(400)) if t and "gag" in t.key]
        assert gagged and all("gag" not in t.text.lower() and t.dm_note == GAG_NOTE for t in gagged)
        assert all(not ({"cuffs", "cuffs_back", "thighs"} & set(t.key.split("+")[1:])) for t in gagged)   # hands free with a gag
        w = World(tmp_path)
        w.tasks._pick = lambda day, mode: next(t for t in gagged)
        w.tasks.tick(MORNING)
        w.start()
        with w.core.transaction() as tx:
            note = tx.fetch_one("SELECT text FROM notifications WHERE dedupe_key LIKE 'obedience:task:%'")["text"]
        assert "Only between us: Ball gag" in note and "gag" not in w.chaster.assigned[0].lower()

    def test_blindfold_behind_the_back_only_after_three_good_tries(self, tmp_path) -> None:
        import random as r
        from obedience.service import compose
        before = {t.key for t in (compose(r.Random(s), set(), set()) for s in range(600)) if t}
        after = {t.key for t in (compose(r.Random(s), set(), set(), back_blind_ok=True) for s in range(600)) if t}
        assert not any("blind" in k and "cuffs_back" in k for k in before) and any("blind" in k and "cuffs_back" in k for k in after)
        assert any({"blind", "cuffs"} <= set(k.split("+")[1:]) for k in before)                            # front cuffs + blindfold from the start

    def test_the_harness_needs_three_good_tries_before_the_blindfold_and_brings_a_collar(self, tmp_path) -> None:
        w = World(tmp_path)
        w.tasks.gear_command(U, "gear add harness", MORNING)
        w.tasks.gear_command(U, "gear add blindfold", MORNING)
        keys = {w.tasks._pick(MORNING.date(), "alone").key for _ in range(800)}
        assert {"hd_front", "hd_back"} & keys and "hd_blind" not in keys and {"collar_kneel", "wear_collar_long"} & keys
        TestPlugLadder()._verify(w, "hd_front", 3)
        keys = {w.tasks._pick(MORNING.date(), "alone").key for _ in range(1500)}
        assert "hd_blind" in keys

    def test_longer_wear_tasks_exist_and_restraint_stays_short(self) -> None:
        import re
        from obedience.service import EXTRAS
        assert any("90 minutes" in t.text for t in EXTRAS) and any("2 hours" in t.text for t in EXTRAS)
        for t in EXTRAS:
            if any(w in t.text for w in ("cuff", "clipped", "tied")):
                assert int(re.search(r"(\d+) minutes", t.text).group(1)) <= 30, t.key


class TestExtraTask:
    """Owner, 2026-11-15 (migration 077): one extra erotic task on a day that already has its own (e.g. a training day), run the usual way."""

    def test_it_waits_for_the_days_own_task_then_asks_and_runs(self, tmp_path) -> None:
        from obedience.service import plan_extra_task
        w = World(tmp_path)
        w.tasks.tick(MORNING)
        own = w.row()
        evening = MORNING.replace(hour=19)
        assert plan_extra_task(w.core, U, MORNING.date(), evening, MORNING) is not None
        assert plan_extra_task(w.core, U, MORNING.date(), evening, MORNING) is None          # one extra a day
        w.tasks.tick(MORNING + timedelta(minutes=5))
        with w.core.transaction() as tx:
            rows = [dict(r) for r in tx.fetch_all("SELECT * FROM obedience_tasks ORDER BY slot")]
        assert [r["slot"] for r in rows] == [0, 1] and rows[0]["id"] == own["id"]            # the day's own plan is untouched
        w.slot = True                                                                         # Chaster's one task slot is taken
        w.tasks.tick(evening)

        def extra():
            with w.core.transaction() as tx:
                return dict(tx.fetch_one("SELECT * FROM obedience_tasks WHERE slot = 1"))
        assert extra()["status"] == "planned"
        w.slot = False
        w.tasks.tick(evening + timedelta(minutes=2))
        assert extra()["status"] == "asking"
        w.tasks.press(user_id=U, row_id=extra()["id"], action="alone", now=evening + timedelta(minutes=3))
        assert extra()["status"] == "assigned" and chaster_brief(extra()["text"]) in w.chaster.assigned


class TestTrainingAndExemptions:
    """Owner, 2026-11-15: a wear exemption no longer cancels a training session (it needs no cage); one lost that way may be credited."""

    def _training_row(self, w, status="assigned", outcome=None):
        with w.core.transaction() as tx:
            tx.execute("INSERT INTO obedience_tasks (id, user_id, local_date, catalog_key, text, planned_at, status, created_at, lock_id, task_id, "
                       "deadline, outcome, details) VALUES ('t', ?, ?, 'training:upper', 'Workout done.', ?, ?, ?, 'lock', 'task-t', ?, ?, 'x')",
                       (U, MORNING.date().isoformat(), MORNING.isoformat(), status, MORNING.isoformat(), (MORNING + timedelta(hours=8)).isoformat(),
                        outcome))

    def test_an_exemption_leaves_an_open_training_session_alone(self, tmp_path) -> None:
        import dataclasses
        w = World(tmp_path)
        self._training_row(w)
        w.tasks._ports = dataclasses.replace(w.tasks._ports, in_exemption=lambda: True)
        w.tasks.tick(MORNING + timedelta(minutes=5))
        assert w.row()["status"] == "assigned" and w.chaster.unassigned == 0

    def test_a_session_an_exemption_cancelled_can_be_credited_once(self, tmp_path) -> None:
        from obedience.service import credit_training, get_verified_training
        w = World(tmp_path)
        self._training_row(w, status="cancelled", outcome="exemption")
        day = MORNING.date().isoformat()
        assert get_verified_training(w.core, U, day) == []
        assert credit_training(w.core, U, day, MORNING) is True
        assert get_verified_training(w.core, U, day) == ["training:upper"]
        assert credit_training(w.core, U, day, MORNING) is False

    def test_only_an_exemption_cancel_is_credited(self, tmp_path) -> None:
        from obedience.service import credit_training
        w = World(tmp_path)
        self._training_row(w, status="failed", outcome="missed")
        assert credit_training(w.core, U, MORNING.date().isoformat(), MORNING) is False


class TestExtraAnytime:
    def test_the_one_time_exception_runs_outside_free_time(self, tmp_path) -> None:
        from obedience.service import plan_extra_task
        late = MORNING.replace(hour=22, minute=15)
        for anytime, expected in ((False, "skipped"), (True, "asking")):
            (tmp_path / str(anytime)).mkdir()
            w = World(tmp_path / str(anytime))
            plan_extra_task(w.core, U, MORNING.date(), late, MORNING, anytime=anytime)
            w.tasks.tick(late)
            with w.core.transaction() as tx:
                assert tx.fetch_one("SELECT status FROM obedience_tasks WHERE slot = 1")["status"] == expected

    def test_still_in_the_exemption_at_its_deadline_it_is_cancelled_quietly(self, tmp_path) -> None:
        import dataclasses
        w = World(tmp_path)
        TestTrainingAndExemptions._training_row(self, w)
        w.tasks._ports = dataclasses.replace(w.tasks._ports, in_exemption=lambda: True)
        w.tasks.tick(MORNING + timedelta(hours=8, minutes=1))
        assert (w.row()["status"], w.row()["outcome"]) == ("cancelled", "exemption") and w.failures == []
        with w.core.transaction() as tx:
            assert tx.fetch_one("SELECT COUNT(*) AS n FROM notifications WHERE dedupe_key LIKE 'obedience:decided:%'")["n"] == 0

    def test_back_before_its_deadline_he_still_has_to_do_it(self, tmp_path) -> None:
        w = World(tmp_path)
        TestTrainingAndExemptions._training_row(self, w)
        w.tasks.tick(MORNING + timedelta(hours=9))                           # no exemption any more: the usual rules
        assert w.failures == ["missed"]


class TestCleanUpHeadsUp:
    """Owner, 2026-11-15: 30 minutes before an erotic task, a heads-up to clean up (with or without a douche); never before training."""

    def _notes(self, w):
        with w.core.transaction() as tx:
            return [r["text"] for r in tx.fetch_all("SELECT text FROM notifications WHERE dedupe_key LIKE 'obedience:prep:%'")]

    def test_it_comes_thirty_minutes_before_and_once(self, tmp_path) -> None:
        from obedience.service import PREP_AHEAD, PREP_TEXT
        w = World(tmp_path)
        w.tasks.tick(MORNING)
        due = w.due()
        w.tasks.tick(due - PREP_AHEAD - timedelta(minutes=5))
        assert self._notes(w) == []
        from obedience.service import Task
        w.tasks._pick = lambda day, mode="alone", owned=None: Task("plug:deep_diver", "The Deep Diver in. Photo of the plug base only. "
                                                                   "Keep it in 20 minutes.", "erotic")
        w.tasks.tick(due - PREP_AHEAD + timedelta(minutes=1))
        w.tasks.tick(due - timedelta(minutes=5))
        assert self._notes(w) == [PREP_TEXT.format(certainty="uses")] and "clean up" in PREP_TEXT

    def test_none_when_the_task_does_not_go_in_and_the_chosen_version_is_given(self, tmp_path) -> None:
        """Owner, 2026-11-16 (A0): the versions are chosen at the heads-up; no clean-up message for a task without a plug."""
        from obedience.service import PREP_AHEAD, Task
        w = World(tmp_path)
        w.tasks.tick(MORNING)
        due = w.due()
        picks = {"alone": Task("pose:kneel+blind", "Kneeling upright, eyes closed. Photo from behind, waist down only. 20 minutes.", "erotic"),
                 "discreet": Task("pose:fours", "On all fours. Photo from behind, waist down only. 10 minutes.", "erotic")}
        w.tasks._pick = lambda day, mode="alone", owned=None: picks[mode]
        w.tasks.tick(due - PREP_AHEAD + timedelta(minutes=1))
        assert self._notes(w) == [] and w.row()["prepared"]
        w.tasks._pick = lambda *a, **k: (_ for _ in ()).throw(AssertionError("chosen again"))
        w.start(due)
        row = w.row()
        assert row["catalog_key"] == "pose:kneel+blind"
        deadline = datetime.fromisoformat(row["deadline"].replace("Z", "+00:00"))
        assert deadline - due == timedelta(minutes=80)                      # the task's own 20 minutes + 1 hour

    def test_may_when_only_one_version_goes_in(self, tmp_path) -> None:
        from obedience.service import PREP_AHEAD, PREP_TEXT, Task
        w = World(tmp_path)
        w.tasks.tick(MORNING)
        due = w.due()
        picks = {"alone": Task("plug:bijou_large", "The Bijou in. Photo of the plug base only. Keep it in 30 minutes.", "erotic"),
                 "discreet": Task("pose:fours", "On all fours. Photo from behind, waist down only. 10 minutes.", "erotic")}
        w.tasks._pick = lambda day, mode="alone", owned=None: picks[mode]
        w.tasks.tick(due - PREP_AHEAD + timedelta(minutes=1))
        assert self._notes(w) == [PREP_TEXT.format(certainty="may use")]

    def test_none_before_a_training_session(self, tmp_path) -> None:
        from obedience.service import PREP_AHEAD
        w = World(tmp_path)
        with w.core.transaction() as tx:
            tx.execute("INSERT INTO obedience_tasks (id, user_id, local_date, catalog_key, text, planned_at, status, created_at, details) "
                       "VALUES ('t', ?, ?, 'training:upper', 'Workout done.', ?, 'planned', ?, 'x')",
                       (U, MORNING.date().isoformat(), (MORNING + timedelta(hours=1)).isoformat(), MORNING.isoformat()))
        w.tasks.tick(MORNING + timedelta(hours=1) - PREP_AHEAD + timedelta(minutes=1))
        assert self._notes(w) == []


def test_a_verified_task_sometimes_carries_a_surprise_reward(tmp_path) -> None:
    """Research a (owner, 2026-11-15): about 1 in 5 verified tasks adds +0.05, announced only when it happens; it counts in the day's bonus."""
    from decimal import Decimal
    from obedience.service import SURPRISE_REWARD, get_surprise_rewards
    from system.daily_evaluation import DayFacts

    class Always(random.Random):
        def random(self):
            return 0.0

    w = World(tmp_path)
    w.tasks._rng = Always(1)
    w.tasks.tick(MORNING)
    due = w.due()
    w.start(due)
    row = w.row()
    w.chaster.entries[row["task_id"]] = {"createdAt": (due + timedelta(minutes=20)).isoformat(), "peerVerification": {"status": "verified"}}
    w.tasks.tick(due + timedelta(minutes=25))
    assert w.row()["status"] == "passed" and w.row()["surprise"] == SURPRISE_REWARD
    with w.core.transaction() as tx:
        note = tx.fetch_one("SELECT text FROM notifications WHERE dedupe_key LIKE 'obedience:decided:%'")
    assert "surprise: +0.05" in note["text"]
    assert get_surprise_rewards(w.core, U, "2026-11-15T00:00", "2026-11-16T00:00") == Decimal("0.05")
    facts = DayFacts(goals_assigned=0, goals_met=0, check_ins_due=0, check_ins_ok=0, violations=0, surprise=Decimal("0.05"))
    assert facts.training_bonus == Decimal("0.05")


def test_no_surprise_when_the_roll_misses(tmp_path) -> None:
    class Never(random.Random):
        def random(self):
            return 0.99

    w = World(tmp_path)
    w.tasks._rng = Never(1)
    w.tasks.tick(MORNING)
    due = w.due()
    w.start(due)
    row = w.row()
    w.chaster.entries[row["task_id"]] = {"createdAt": (due + timedelta(minutes=20)).isoformat(), "peerVerification": {"status": "verified"}}
    w.tasks.tick(due + timedelta(minutes=25))
    assert w.row()["status"] == "passed" and w.row()["surprise"] is None


def test_chaster_sees_a_brief_text_and_the_dm_the_full_one(tmp_path) -> None:
    """Owner, 2026-11-15: no lube, warm-up or wearing time in Chaster; the DM keeps them."""
    from obedience.service import chaster_brief
    full = ("Your gray Satisfyer Deep Diver in, with water-based lube. Warm up with the clear Crushious Bijou for 10 minutes first. "
            "Photo of the plug base only. Keep it in 20 minutes.")
    assert chaster_brief(full) == "Your Deep Diver in. Photo of the plug base only."
    assert chaster_brief("20 minutes kneeling, hands behind your back. Photo from behind, waist down only.") == (
        "Kneeling, hands behind your back. Photo from behind, waist down only.")
    assert chaster_brief("Clamps on your nipples, kneeling upright. Photo of your chest only, no face. 10 minutes, then off slowly.") == (
        "Clamps on your nipples, kneeling upright. Photo of your chest only, no face.")
    for task in _all_texts():
        brief = chaster_brief(task.text).lower()
        assert ("only" in brief or "no face" in brief) and "lube" not in brief and "minutes" not in brief, (task.key, brief)



def test_the_conversation_sees_todays_tasks_and_where_they_stand(tmp_path) -> None:
    """Bug fix 2026-11-15: "The task is done" got praise for a workout; the model now sees today's tasks (read-only)."""
    from conversation_engine.context_providers.obedience_today_provider import ObedienceTodayContextProvider
    from conversation_engine.prompt_builder import _render_todays_tasks
    from obedience.service import get_obedience_today
    w = World(tmp_path)
    w.tasks.tick(MORNING)
    assert get_obedience_today(w.core, U, "2026-11-15") == [{"kind": "obedience task", "task": "not chosen yet", "state": "planned for later today"}]
    due = w.due()
    w.start(due)
    row = w.row()
    w.chaster.entries[row["task_id"]] = {"createdAt": (due + timedelta(minutes=20)).isoformat(), "peerVerification": {"status": "pending"}}
    w.tasks.tick(due + timedelta(minutes=25))
    [task] = get_obedience_today(w.core, U, "2026-11-15")
    assert task["state"] == "photo sent, the community vote is running" and "lube" not in task["task"]
    fragment = ObedienceTodayContextProvider(read=lambda u, now: get_obedience_today(w.core, u, "2026-11-15")).provide_context(subject_key=U, now=due)
    assert "the community vote is running" in _render_todays_tasks(fragment.data)
    assert ObedienceTodayContextProvider(read=lambda u, now: []).provide_context(subject_key=U, now=due) is None


def test_standing_reminders_come_once_a_week_but_the_restraint_line_stays(tmp_path) -> None:
    """Owner, 2026-11-16 (A0b): safety and discretion in full at most once a week; a restraint task always says "Key and shears within reach."."""
    from obedience.service import DISCRETION, RESTRAINT_SHORT, Task
    w = World(tmp_path)
    w.tasks._pick = lambda day, mode="alone", owned=None: Task("pose:fours+cuffs", "On all fours, wrists cuffed in front, key within reach. "
                                                               "Photo from behind, waist down only. 15 minutes.", "erotic")
    texts = []
    for day in range(3):
        start = MORNING + timedelta(days=day)
        w.tasks.tick(start)
        w.start(w.due())
        row_id = w.row()["id"]
        with w.core.transaction() as tx:
            texts.append(tx.fetch_one("SELECT text FROM notifications WHERE dedupe_key = ?", (f"obedience:task:{row_id}",))["text"])
        with w.core.transaction() as tx:
            tx.execute("UPDATE obedience_tasks SET status = 'passed', outcome = 'verified'")
        w.chaster.current = None
    assert DISCRETION in texts[0] and DISCRETION not in texts[1] and DISCRETION not in texts[2]
    assert all(RESTRAINT_SHORT in t or "within reach the whole time" in t for t in texts)


class TestGearRemovalWaits:
    """Owner, 2026-11-16: "gear remove shouldn't work instantly" (G3's waiting, via opt_outs)."""

    def _wired(self, tmp_path, locked=True):
        from opt_outs.service import OptOuts
        w = World(tmp_path)
        opt = OptOuts(w.tasks._core, locked=lambda u: locked, local_clock=lambda t: t.strftime("%H:%M"))
        opt.register_gear(lambda user, piece, now: w.tasks.set_gear(piece, 0, now, reason="asked_after_24h"), label=lambda piece: GEAR[piece])
        w.tasks.remove_wait = lambda user, piece, now: opt.request_off(user, f"gear:{piece}", now)
        w.tasks.add_hook = lambda user, piece, now: opt.withdraw(user, f"gear:{piece}", now)
        return w, opt

    def _row(self, w):
        with w.tasks._core.transaction() as tx:
            return dict(tx.fetch_one("SELECT * FROM opt_out_requests ORDER BY requested_at DESC LIMIT 1"))

    def test_locked_removal_waits_then_a_yes_removes_it(self, tmp_path) -> None:
        from opt_outs.service import COOLING
        w, opt = self._wired(tmp_path)
        reply = w.tasks.gear_command(U, "gear remove deep diver", MORNING)
        assert reply.startswith("Noted.") and "gray Satisfyer Deep Diver" in reply and "broken" in reply
        assert "deep_diver" in w.tasks.owned_gear()
        assert w.tasks.gear_command(U, "gear remove diver", MORNING).startswith("You already asked to remove")
        opt.tick(MORNING + COOLING)
        assert opt.press(user_id=U, row_id=self._row(w)["id"], action="yes", now=MORNING + COOLING).startswith("Removed:")
        assert "deep_diver" not in w.tasks.owned_gear()

    def test_keep_it_or_adding_back_keeps_it(self, tmp_path) -> None:
        from opt_outs.service import COOLING
        w, opt = self._wired(tmp_path)
        w.tasks.gear_command(U, "gear remove rope hemp", MORNING)
        opt.tick(MORNING + COOLING)
        assert "stays on your gear list" in opt.press(user_id=U, row_id=self._row(w)["id"], action="no", now=MORNING + COOLING)
        later = MORNING + COOLING + timedelta(hours=1)
        w.tasks.gear_command(U, "gear remove gag", later)
        w.tasks.gear_command(U, "gear add gag", later)
        assert self._row(w)["status"] == "withdrawn" and {"rope_hemp", "gag"} <= w.tasks.owned_gear()

    def test_broken_lost_never_had_and_unlocked_are_at_once(self, tmp_path) -> None:
        w, _ = self._wired(tmp_path)
        assert w.tasks.gear_command(U, "gear remove deep diver broken", MORNING).startswith("Removed:")
        assert w.tasks.gear_command(U, "gear remove fluffy cuffs never had", MORNING).startswith("Removed: fluffy cuffs")
        assert w.tasks.gear_command(U, "gear remove gag lost", MORNING).startswith("Removed:")
        assert not {"deep_diver", "toy_cuffs", "gag"} & w.tasks.owned_gear()
        assert w.tasks.gear_command(U, "gear remove collar", MORNING).startswith("Removed:")      # not owned: nothing to wait for
        w2, _ = self._wired(tmp_path / "u", locked=False)
        assert w2.tasks.gear_command(U, "gear remove deep diver", MORNING).startswith("Removed:")

    def test_a_broken_wait_never_removes(self, tmp_path) -> None:
        w = World(tmp_path)
        w.tasks.remove_wait = lambda user, piece, now: 1 / 0
        assert not w.tasks.gear_command(U, "gear remove deep diver", MORNING).startswith("Removed")
        assert "deep_diver" in w.tasks.owned_gear()


class TestOwnGearNames:
    """Leak audit (owner, 2026-11-16): another wearer sees pieces by role and their own names replace the owner's items in task texts."""

    def test_a_wearer_names_their_own_piece_and_tasks_use_it(self, tmp_path) -> None:
        from obedience.service import ROLE
        w = World(tmp_path)
        w.tasks.default_owned, w.tasks.generic_names = frozenset(), True
        assert w.tasks.gear_command(U, "gear add vibrating plug as Black Silicone Plug", MORNING) == \
            "Added: Black Silicone Plug. Tasks that need it can come now."
        listing = w.tasks.gear_command(U, "gear", MORNING)
        assert "Your gear: Black Silicone Plug." in listing and ROLE["dildo_purple"] in listing and "RealRock" not in listing
        assert w.tasks.personalize("Your gray Satisfyer Deep Diver in. Keep the Deep Diver in.") == \
            "Your Black Silicone Plug in. Keep the Black Silicone Plug in."
        for _ in range(200):
            task = w.tasks._pick(MORNING.date(), "alone")
            assert "Deep Diver" not in task.text and "Satisfyer" not in task.text

    def test_the_owner_keeps_his_names(self, tmp_path) -> None:
        w = World(tmp_path)
        assert w.tasks.personalize("Your gray Satisfyer Deep Diver in.") == "Your gray Satisfyer Deep Diver in."
        assert "gray Satisfyer Deep Diver" in w.tasks.gear_command(U, "gear", MORNING)


def test_today_for_the_short_session_switches_a_task_that_is_only_planned(tmp_path) -> None:
    """Bug fix 2026-11-16: "Today" came after the day's (erotic) task was planned, so the short session never came."""
    import dataclasses
    from types import SimpleNamespace
    w = World(tmp_path)
    w.tasks._plan(MORNING)
    row = w.tasks._row_for(w.tasks._ports.today(MORNING))
    assert row is not None and row["status"] == "planned"
    session = SimpleNamespace(key="training:short:upper_short", text="Workout done: Upper body, short. Photo of the gym only.", details="x")
    w.tasks._ports = dataclasses.replace(w.tasks._ports, training=lambda day: session)
    assert w.tasks.take_training_today(MORNING) is True
    row = w.tasks._row_for(w.tasks._ports.today(MORNING))
    assert row["catalog_key"] == "training:short:upper_short" and row["status"] == "planned"


def test_today_open_until_the_task_is_out(tmp_path) -> None:
    """`extra training` (owner, 2026-11-16) may take today's place only while that task is not out yet."""
    w = World(tmp_path)
    assert w.tasks.today_open(MORNING) is True                                   # nothing planned yet
    w.tasks._plan(MORNING)
    assert w.tasks.today_open(MORNING) is True                                   # only planned
    with w.core.transaction() as tx:
        tx.execute("UPDATE obedience_tasks SET status = 'asked'")
    assert w.tasks.today_open(MORNING) is False


def test_an_extra_task_on_request(tmp_path) -> None:
    """Owner, 2026-11-16: "I don't have a workout today, so I feel it's only right I have a proper erotic task" (`extra task [at HH:MM]`)."""
    w = World(tmp_path)
    assert w.tasks.extra_task_command(U, "hello", MORNING) is None and w.tasks.extra_task_command("other", "extra task", MORNING) is None
    reply = w.tasks.extra_task_command(U, "Extra task at 18:30", MORNING)
    assert reply.startswith("Good. An extra task at 18:30") and "usual consequences" in reply
    with w.core.transaction() as tx:
        row = tx.fetch_one("SELECT slot, catalog_key, status, planned_at FROM obedience_tasks WHERE slot = 1")
    assert row["status"] == "planned" and row["planned_at"].startswith("2026-11-15T18:30")
    assert "One a day" in w.tasks.extra_task_command(U, "extra task", MORNING)
    assert "already passed" in World(tmp_path / "b").tasks.extra_task_command(U, "extra task at 08:00", MORNING)
    assert "outside your free time" in World(tmp_path / "c").tasks.extra_task_command(U, "extra task at 21:30", MORNING)
    assert "paused" in World(tmp_path / "d", paused=True).tasks.extra_task_command(U, "extra task", MORNING)
