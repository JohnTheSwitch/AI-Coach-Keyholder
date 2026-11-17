"""The bot running the Deep Diver during a task (owner, 2026-11-17), against a fake Buttplug server: no network, no toy."""

from __future__ import annotations

import random
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest

from infrastructure.database import Database
from tests.toy_control.test_client import DIVER, FakeServer
from toy_control.session import MAX_MINUTES, ToySessions, plan_pattern, profile_for
from toy_control.store import START_LEVEL, ToyLevels

ROOT = Path(__file__).parent.parent.parent


def _sessions(devices, servers):
    @asynccontextmanager
    async def connect(_url):
        server = FakeServer(devices)
        servers.append(server)
        yield server.send, server.recv

    return ToySessions(connect=connect, rng=random.Random(1))


def _wait(s: ToySessions, seconds=10.0) -> None:
    end = time.monotonic() + seconds
    while s.running() and time.monotonic() < end:
        time.sleep(0.05)


class TestThePattern:
    """Owner, 2026-11-17: "make it more intelligently done": shaped, fitted to the task, built around his own level."""

    @pytest.mark.parametrize("profile,mode,minutes", [("default", "alone", 20), ("prostate", "alone", 25), ("wear", "alone", 90),
                                                      ("discreet", "discreet", 15)])
    def test_it_fills_the_time_within_the_limits(self, profile, mode, minutes) -> None:
        steps = plan_pattern(random.Random(3), minutes, mode, profile=profile, base=0.9)
        assert abs(sum(s for _, s in steps) - minutes * 60) < 1e-6
        assert all(level == 0 or 0.05 <= level <= (0.25 if mode != "alone" else 1.0) for level, _ in steps)

    def test_the_default_warms_up_builds_teases_and_finishes(self) -> None:
        levels = [level for level, _ in plan_pattern(random.Random(3), 20, "alone", profile="default", base=0.6)]
        third = len(levels) // 3
        assert levels[0] < 0.25 and sum(levels[:third]) / third < sum(levels[-third:]) / third
        assert 0.05 in levels and levels[-1] >= 0.6                               # a tease drop, and the strongest at the end

    def test_a_long_wear_is_mostly_off_and_not_alone_stays_gentle(self) -> None:
        wear = plan_pattern(random.Random(3), 90, "alone", profile="wear", base=0.6)
        assert sum(s for level, s in wear if level == 0) > 0.8 * 90 * 60
        assert max(level for level, _ in plan_pattern(random.Random(5), 60, "discreet", base=1.0)) <= 0.25
        assert sum(s for _, s in plan_pattern(random.Random(3), 600, "alone", profile="wear")) == MAX_MINUTES * 60

    def test_his_level_moves_the_whole_session(self) -> None:
        weak = plan_pattern(random.Random(3), 20, "alone", base=0.3)
        strong = plan_pattern(random.Random(3), 20, "alone", base=0.8)
        assert sum(level for level, _ in weak) < sum(level for level, _ in strong)

    def test_the_profile_fits_the_task(self) -> None:
        assert profile_for("prostate_training", "alone", 25) == "prostate"
        assert profile_for("wear_diver_long", "alone", 90) == "wear"
        assert profile_for("pose:wall+vibe", "alone", 20) == "default"
        assert profile_for("prostate_training", "discreet", 25) == "discreet"


class TestASession:
    def test_it_vibrates_and_always_ends_with_a_stop(self) -> None:
        servers, done = [], []
        s = _sessions([DIVER], servers)
        assert s.start(0.02, "alone", on_done=lambda: done.append(1)) is True
        _wait(s)
        assert done == [1]
        kinds = [k for k, _ in servers[0].sent]
        assert "ScalarCmd" in kinds and kinds[-1] == "StopDeviceCmd"
        assert all(0.05 <= b["Scalars"][0]["Scalar"] <= 1.0 for k, b in servers[0].sent if k == "ScalarCmd")

    def test_stop_ends_it_early_and_one_at_a_time(self) -> None:
        servers = []
        s = _sessions([DIVER], servers)
        assert s.start(10, "discreet") is True
        time.sleep(0.3)
        assert s.start(10, "alone") is False
        assert s.stop() is True and not s.running()
        assert servers[0].sent[-1][0] == "StopDeviceCmd" and s.stop() is False

    def test_no_toy_tells_him_and_runs_nothing(self) -> None:
        servers, told, done = [], [], []
        s = _sessions([], servers)
        s.start(1, "alone", on_problem=told.append, on_done=lambda: done.append(1))
        _wait(s, 20)
        assert told and not done and "ScalarCmd" not in [k for k, _ in servers[0].sent]

    def test_no_intiface_tells_him(self) -> None:
        told = []

        @asynccontextmanager
        async def refused(_url):
            raise OSError("connection refused")
            yield

        s = ToySessions(connect=refused)
        s.start(1, "alone", on_problem=told.append)
        _wait(s)
        assert told == ["connection refused"]


class TestHisLevel:
    def test_feedback_moves_the_next_level_and_right_still_climbs(self, tmp_path) -> None:
        core = Database(tmp_path / "d.db")
        with core.raw_connection() as conn:
            for path in sorted((ROOT / "database" / "migrations").glob("*.sql")):
                conn.executescript(path.read_text(encoding="utf-8"))
            conn.commit()
        now = datetime(2026, 11, 17, 18, 0, tzinfo=timezone.utc)
        levels = ToyLevels(core, user_id="u")
        assert levels.level() == START_LEVEL
        assert levels.feedback(task_ref="t1", answer="too_weak", now=now) is None             # nothing to answer yet
        levels.record_start(task_ref="t1", mode="alone", profile="default", level=levels.level(), now=now)
        assert levels.feedback(task_ref="t1", answer="too_weak", now=now) == 0.6 and levels.level() == 0.6
        assert levels.feedback(task_ref="t1", answer="too_much", now=now) is None             # one answer per session
        levels.record_start(task_ref="t2", mode="alone", profile="default", level=levels.level(), now=now)
        assert levels.level() == 0.6                                                           # no answer keeps it
        assert levels.feedback(task_ref="t2", answer="right", now=now) == 0.62
        levels.record_start(task_ref="t3", mode="alone", profile="default", level=0.95, now=now)
        assert levels.feedback(task_ref="t3", answer="too_weak", now=now) == 1.0               # never above 1.0
        with core.transaction() as tx:
            kinds = [r["event_type"] for r in tx.fetch_all("SELECT event_type FROM domain_events ORDER BY rowid")]
        assert kinds.count("toy_control.session_started") == 3 and kinds.count("toy_control.feedback_recorded") == 3


def test_steps_are_in_it_because_he_liked_them() -> None:
    """Owner, 2026-11-17: the Satisfyer app's "Steps" (measured from his recording): four bursts at 40/60/80/100 %, 0.5 s on, 0.5 s off."""
    steps = plan_pattern(random.Random(1), 20, "alone", base=0.6)
    sets = [steps[i:i + 8] for i in range(len(steps) - 8)
            if all(steps[i + 2 * j][1] == 0.5 and steps[i + 2 * j + 1] == (0.0, 0.5) for j in range(4))]
    assert sets and all(s[0][0] < s[2][0] < s[4][0] < s[6][0] for s in sets[:1])


def test_every_few_minutes_a_surprise_so_he_never_gets_used_to_it() -> None:
    """Owner, 2026-11-17: "once a few minutes it changed, stop for a few seconds, then turned on for a few seconds"."""
    steps = plan_pattern(random.Random(2), 20, "alone", base=0.6)
    assert abs(sum(s for _, s in steps) - 1200) < 1e-6
    breaks = [i for i in range(len(steps) - 1) if steps[i][0] == 0 and 3 <= steps[i][1] <= 8 and 2 <= steps[i + 1][1] <= 5 and steps[i + 1][0] > 0]
    assert len(breaks) >= 3
    assert not any(l == 0 and 3 <= s <= 8 for l, s in plan_pattern(random.Random(2), 15, "discreet"))   # not alone: no sudden changes
