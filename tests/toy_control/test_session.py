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
        assert max(s for level, s in wear if level == 0) <= 240           # owner, 2026-11-23: quiet stretches of 1-4 minutes
        bursts, current = [], []
        for level, s in wear:                                             # owner, 2026-11-23: a burst is not one level throughout
            if level == 0 and s > 2:
                if current:
                    bursts.append(current)
                current = []
            elif level > 0:
                current.append(level)
        assert bursts and all(len(set(b)) > 1 for b in bursts if len(b) > 1)
        assert sum(1 for b in bursts if len(b) > 1) >= len(bursts) // 2
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

    def test_pause_turns_it_off_and_its_time_waits(self) -> None:
        """Owner, 2026-11-23: Pause / Continue. While paused the toy is off and the session's time does not run."""
        servers, done = [], []
        s = _sessions([DIVER], servers)
        assert s.resume() is False and s.pause() is False                  # nothing running
        assert s.start(0.05, "alone", on_done=lambda: done.append(1)) is True  # 3 seconds of pattern
        time.sleep(2.2)
        assert s.pause() is True and s.pause() is False and s.paused()
        time.sleep(4)                                                       # longer than the whole pattern
        assert s.running() and not done
        assert servers[0].sent[-1][0] == "StopDeviceCmd"                    # off while paused
        assert s.resume() is True and s.resume() is False and not s.paused()
        _wait(s)
        assert done == [1] and servers[0].sent[-1][0] == "StopDeviceCmd"

    def test_every_command_goes_to_the_toy_log(self) -> None:
        """Owner, 2026-11-23: what the bot did with the toy can be followed in toy.log."""
        import logging
        from infrastructure.decision_log import TOY_LOGGER
        lines = []

        class Keep(logging.Handler):
            def emit(self, record):
                lines.append(record.getMessage())

        log = logging.getLogger(TOY_LOGGER)
        handler, level = Keep(), log.level
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        try:
            s = _sessions([DIVER], [])
            s.start(0.02, "alone")
            time.sleep(2.2)
            s.pause()
            time.sleep(0.8)
            s.resume()
            _wait(s)
        finally:
            log.removeHandler(handler)
            log.setLevel(level)
        text = " | ".join(lines)
        for part in ("Session planned.", "Session started.", "Level ", "Pause pressed.", "Off (paused).", "Continue pressed.",
                     "Session ended, toy stopped."):
            assert part in text, part

    def test_a_check_buzz_first_then_the_confirmation(self) -> None:
        """Owner, 2026-11-23: a short buzz at the start of every session, so he knows it works."""
        servers, started = [], []
        s = _sessions([DIVER], servers)
        s.start(0.02, "alone", on_started=lambda: started.append(1))
        _wait(s)
        scalars = [b for k, b in servers[0].sent if k == "ScalarCmd"]
        assert scalars[0]["Scalars"][0]["Scalar"] == 0.4 and started == [1]
        kinds = [k for k, _ in servers[0].sent]
        assert kinds.index("StopDeviceCmd") < kinds.index("ScalarCmd", kinds.index("ScalarCmd") + 1)

    def test_a_long_quiet_step_keeps_the_link_and_the_toy_awake(self, monkeypatch) -> None:
        """Live, 2026-11-23: a 3.7-minute quiet step sent nothing and Intiface dropped the bot; the toy also sleeps after about 5 idle
        minutes. The current command is sent again every KEEPALIVE_SECONDS."""
        import toy_control.session as session
        monkeypatch.setattr(session, "KEEPALIVE_SECONDS", 0.5)
        monkeypatch.setattr(session, "plan_pattern", lambda rng, minutes, mode, profile=None, base=0.5: [(0.0, 2.5)])
        servers = []
        s = _sessions([DIVER], servers)
        s.start(1, "alone")
        _wait(s)
        stops = [k for k, _ in servers[0].sent].count("StopDeviceCmd")
        assert stops >= 5                                                   # check buzz off, the step, keep-alives, the final stop

    def test_stop_works_while_paused(self) -> None:
        servers = []
        s = _sessions([DIVER], servers)
        s.start(10, "alone")
        time.sleep(0.3)
        s.pause()
        assert s.stop() is True and not s.running()

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


    def test_no_intiface_starts_it_and_tries_once_more(self) -> None:
        """Owner, 2026-11-25: insert, turn on, press: the bot opens Intiface itself when it isn't running."""
        told, launched, calls = [], [], []

        @asynccontextmanager
        async def refused_once(_url):
            calls.append(1)
            if len(calls) == 1:
                raise OSError("connection refused")
            raise LookupError("the toy was not found")      # the second try reaches the next step
            yield

        s = ToySessions(connect=refused_once, start_intiface=lambda: launched.append(1) or True)
        s.start(1, "alone", on_problem=told.append)
        _wait(s)
        assert launched == [1] and len(calls) == 2 and told == ["the toy was not found"]

        calls.clear()
        launched.clear()
        told.clear()
        s = ToySessions(connect=refused_once, start_intiface=lambda: launched.append(1) or False)
        s.start(1, "alone", on_problem=told.append)
        _wait(s)
        assert launched == [1] and len(calls) == 1 and told == ["connection refused"]       # could not start it: told as before


class TestTheLauncher:
    def test_starts_intiface_minimized_and_waits_for_its_server(self, tmp_path) -> None:
        from toy_control.launcher import IntifaceLauncher
        exe = tmp_path / "intiface_central.exe"
        exe.write_text("")
        started, up = [], iter([False, False, False, True])
        launcher = IntifaceLauncher("ws://127.0.0.1:12345", path=lambda: exe, popen=lambda args, **kw: started.append(args),
                                    listening=lambda h, p: next(up), sleep=lambda s: None)
        assert launcher() is True and started == [[str(exe)]]

    def test_already_up_never_found_or_remote_does_nothing(self, tmp_path) -> None:
        from toy_control.launcher import IntifaceLauncher
        started = []
        popen = lambda args, **kw: started.append(args)
        assert IntifaceLauncher("ws://127.0.0.1:12345", path=lambda: None, popen=popen, listening=lambda h, p: True)() is True
        assert IntifaceLauncher("ws://127.0.0.1:12345", path=lambda: None, popen=popen, listening=lambda h, p: False)() is False
        assert IntifaceLauncher("ws://192.168.1.5:12345", path=lambda: tmp_path, popen=popen, listening=lambda h, p: False)() is False
        never = IntifaceLauncher("ws://127.0.0.1:12345", path=lambda: tmp_path / "x.exe", popen=popen, listening=lambda h, p: False,
                                 sleep=lambda s: None, wait_seconds=3)
        assert never() is False and len(started) == 1


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
