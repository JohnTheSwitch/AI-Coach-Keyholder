"""Per-wearer feature switches (owner, 2026-11-16)."""

from __future__ import annotations

from datetime import datetime, timezone

from features.service import FEATURES, Features, get_feature_on
from opt_outs.service import COOLING, OptOuts
from tests.lines.test_lines import core

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
U = "wearer"


def wired(tmp_path, locked=True):
    db = core(tmp_path)
    features = Features(db)
    opt = OptOuts(db, locked=lambda u: locked, local_clock=lambda t: t.strftime("%H:%M"))
    opt.register_features(features.apply_off, label=lambda key: FEATURES[key][0] if key in FEATURES else None)
    features.request_off, features.withdraw = opt.request_off, opt.withdraw
    return db, features, opt


def test_everything_is_on_by_default_and_listed(tmp_path) -> None:
    db, features, _ = wired(tmp_path)
    assert all(get_feature_on(db, U, key) for key in FEATURES)
    listing = features.command(U, "features", NOW)
    assert all(f"- {words}: on" in listing for words, _ in FEATURES.values())
    assert features.command(U, "hello", NOW) is None and features.command(U, "gear remove gag", NOW) is None


def test_unlocked_switches_at_once_and_back_on(tmp_path) -> None:
    db, features, _ = wired(tmp_path, locked=False)
    assert features.command(U, "Workouts off", NOW).startswith("Workouts is off.")
    assert not get_feature_on(db, U, "workouts") and get_feature_on(db, "someone else", "workouts")
    assert features.command(U, "workouts off", NOW) == "Workouts is already off."
    assert features.command(U, "workouts on", NOW) == "Workouts is on again."
    assert get_feature_on(db, U, "workouts")


def test_locked_switch_off_waits_and_is_asked_about(tmp_path) -> None:
    db, features, opt = wired(tmp_path)
    reply = features.command(U, "release relock off", NOW)
    assert reply.startswith("Noted.") and "release relock" in reply
    assert get_feature_on(db, U, "release_relock")
    opt.tick(NOW + COOLING)
    with db.transaction() as tx:
        row = dict(tx.fetch_one("SELECT * FROM opt_out_requests"))
    assert opt.press(user_id=U, row_id=row["id"], action="yes", now=NOW + COOLING).startswith("Release relock is off.")
    assert not get_feature_on(db, U, "release_relock")


def test_switching_on_withdraws_a_waiting_switch_off(tmp_path) -> None:
    db, features, _ = wired(tmp_path)
    features.command(U, "lines off", NOW)
    assert features.command(U, "lines on", NOW) == "Lines is already on."
    with db.transaction() as tx:
        assert tx.fetch_one("SELECT status FROM opt_out_requests")["status"] == "withdrawn"


def test_a_broken_wait_never_switches_off(tmp_path) -> None:
    db, features, _ = wired(tmp_path)
    features.request_off = lambda *a: 1 / 0
    assert features.command(U, "protocol off", NOW) == "That can't be changed right now. Try again later."
    assert get_feature_on(db, U, "protocol")


def test_the_release_window_does_not_open_when_off(tmp_path) -> None:
    from release_earning.window import ReleaseWindow
    db = core(tmp_path)
    w = ReleaseWindow(db, user_id=U, last_release=lambda: ("auth-1", NOW), pictures=lambda a, b: False, local_clock=lambda t: "x")
    w.enabled = lambda: False
    w.tick(NOW)
    with db.transaction() as tx:
        assert tx.fetch_one("SELECT 1 FROM release_windows") is None


def test_the_protocol_is_silent_when_off() -> None:
    from protocol.service import ProtocolLadder, Ports
    ports = Ports(local=lambda t: t, to_utc=lambda d, t: NOW, quiet_end=NOW.time(), quiet_start=NOW.time(), paused=lambda now: False,
                  open_lines=lambda: 0, todays_task_open=lambda now: False, window_left_hours=lambda now: None, verified_since=lambda s: [],
                  open_duty=lambda *a: None, answer_duties=lambda *a: 0, duty_misses=lambda s: 0, enabled=lambda: False)
    ladder = ProtocolLadder(None, user_id=U, ports=ports)
    assert ladder.gate(U, "this is unfair", NOW) is None
    ladder.tick(NOW)                                   # never touches the (missing) database


def test_an_unavailable_feature_stays_off_and_says_why(tmp_path) -> None:
    db, features, _ = wired(tmp_path, locked=False)
    features.unavailable = {"workouts": "Not for you yet."}
    assert features.command(U, "workouts on", NOW) == "Not for you yet."
    assert not features.is_on(U, "workouts") and features.is_on(U, "lines")
    assert "- workouts: not available yet" in features.command(U, "features", NOW)
