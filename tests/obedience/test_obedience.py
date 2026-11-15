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
    assert sentence_for("OBEDIENCE_TASK_MISSED") == "I obey my keyholder's tasks."


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
    assert row["status"] == "assigned" and row["mode"] == "alone" and w.chaster.assigned == [row["text"]]
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
    w.tasks.tick(due + PHOTO_DEADLINE + timedelta(minutes=5))
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
        assert w.row()["deadline"].startswith((at + POSTPONE).isoformat()[:16].replace("+00:00", ""))
        assert "already postponed" in w.tasks.press(user_id=U, row_id=w.row()["id"], action="later", now=at)
        w.tasks.tick(due + PHOTO_DEADLINE + timedelta(minutes=5))           # the old deadline passes: still open
        assert w.row()["status"] == "assigned" and w.failures == []
        w.tasks.tick(at + POSTPONE + timedelta(minutes=5))
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
        assert w.tasks.press(user_id=U, row_id=w.row()["id"], action="alone", now=due + ASK_WAIT) == "Already answered."

    def test_not_alone_answer_and_a_second_answer(self, tmp_path) -> None:
        w = World(tmp_path)
        w.tasks.tick(MORNING)
        assert w.start(answer="discreet") == "Not alone: a discreet version. It's in Chaster now."
        assert w.row()["mode"] == "discreet"
        assert w.tasks.press(user_id=U, row_id=w.row()["id"], action="alone", now=w.due()) == "Already answered."

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
        assert extra()["status"] == "assigned" and extra()["text"] in w.chaster.assigned


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
