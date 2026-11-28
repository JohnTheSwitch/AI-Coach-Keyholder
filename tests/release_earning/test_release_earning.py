"""Earn the release (owner, 2026-11-15): a token alone no longer plans a release. On a training day the session is the release task; otherwise
20 release lines. What counts is what was earned since the last token spent; a read failure holds nothing. No network."""

from __future__ import annotations

from datetime import timedelta

from lines.service import RELEASE_GROUP, RELEASE_LINES, RELEASE_SENTENCE, LineService
from release_earning.service import WAIT_LINES, WAIT_TRAINING, ReleaseEarning
from tests.lines.test_lines import NOW, U, core, rows
from tests.system.test_release_tick import World as TickWorld


def earning(*, spent=None, trained=False, training_day=False, done=False, assigned=None):
    assigned = [] if assigned is None else assigned
    return ReleaseEarning(user_id=U, last_spend_at=lambda: spent, training_verified_since=lambda since: trained,
                          training_today=lambda now: training_day, release_lines_done_since=lambda since: done,
                          assign_release_lines=lambda since, now: assigned.append(since) or True)


class TestTheRule:
    def test_a_verified_session_or_finished_lines_earn_it(self) -> None:
        assert earning(trained=True).hold(NOW) is None and earning(done=True).hold(NOW) is None

    def test_on_a_training_day_the_session_is_the_task(self) -> None:
        assigned = []
        assert earning(training_day=True, assigned=assigned).hold(NOW) == WAIT_TRAINING and assigned == []

    def test_otherwise_release_lines_since_the_last_spend(self) -> None:
        assigned = []
        assert earning(spent="2026-11-10T10:00:00Z", assigned=assigned).hold(NOW) == WAIT_LINES and assigned == ["2026-11-10T10:00:00Z"]

    def test_a_read_failure_holds_nothing(self) -> None:
        broken = ReleaseEarning(user_id=U, last_spend_at=lambda: 1 / 0, training_verified_since=lambda s: False, training_today=lambda n: False,
                                release_lines_done_since=lambda s: False, assign_release_lines=lambda s, n: True)
        assert broken.hold(NOW) is None


class TestReleaseLines:
    def test_assigned_once_per_release_and_never_debt(self, tmp_path) -> None:
        db = core(tmp_path)
        s = LineService(db, user_id=U)
        assert s.assign_release("", NOW) is True and s.assign_release("", NOW + timedelta(hours=1)) is False
        row = rows(db, "SELECT * FROM line_assignments")[0]
        assert (row["rule_group_id"], row["sentence"], row["required"]) == (RELEASE_GROUP, RELEASE_SENTENCE, RELEASE_LINES)
        assert s.open_lines(U) == 0 and s.get_lines_between(NOW.date(), NOW.date()) == []     # never holds hygiene, never counted as owed
        note = rows(db, "SELECT text FROM notifications WHERE kind = 'lines'")[0]["text"]
        assert "release is within reach" in note and RELEASE_SENTENCE in note

    def test_done_counts_and_a_later_spend_needs_new_ones(self, tmp_path) -> None:
        db = core(tmp_path)
        s = LineService(db, user_id=U)
        s.assign_release("", NOW)
        assert s.release_done_since("") is False
        s.take(U, "\n".join([RELEASE_SENTENCE] * RELEASE_LINES), NOW + timedelta(minutes=5))
        assert s.release_done_since("") is True
        later = (NOW + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        assert s.release_done_since(later) is False and s.assign_release(later, NOW + timedelta(days=2)) is True


class TestTheTickWaits:
    def test_no_plan_until_earned_then_planned(self, tmp_path) -> None:
        w = TickWorld(tmp_path)
        state = {"hold": WAIT_LINES}
        w.task.release_earned = lambda now: state["hold"]
        w.tokens()
        report = w.cycle()
        assert WAIT_LINES in report.notes and "plan_created" not in report.notes and w.plan_statuses() == []
        state["hold"] = None
        assert "plan_created" in w.cycle(w.now + timedelta(minutes=5)).notes

    def test_the_last_spend_is_read_from_the_ledger(self, tmp_path) -> None:
        w = TickWorld(tmp_path)
        assert w.engine.get_last_spend_at(U) is None
        w.tokens()
        w.cycle()
        w.cycle(w.target())
        assert w.engine.get_last_spend_at(U) is not None


def test_a_verified_or_credited_session_after_the_spend_counts(tmp_path) -> None:
    from obedience.service import get_training_verified_since
    from tests.obedience.test_obedience import MORNING, World as ObedienceWorld
    from tests.obedience.test_obedience import U as OU
    w = ObedienceWorld(tmp_path)
    with w.core.transaction() as tx:
        tx.execute("INSERT INTO obedience_tasks (id, user_id, local_date, catalog_key, text, planned_at, status, created_at, outcome, decided_at) "
                   "VALUES ('t', ?, ?, 'training:upper', 'x', ?, 'passed', ?, 'verified', '2026-11-15T18:00:00.000000Z')",
                   (OU, MORNING.date().isoformat(), MORNING.isoformat(), MORNING.isoformat()))
    assert get_training_verified_since(w.core, OU, "2026-11-15T10:00:00Z") is True
    assert get_training_verified_since(w.core, OU, "2026-11-15T19:00:00Z") is False



def test_while_it_waits_on_lines_it_reminds_and_never_on_the_assigning_pass() -> None:
    """Owner, 2026-11-25 (proposal 2): a reminder while the release waits only on lines (the wiring keeps it to once a day, free time)."""
    from datetime import datetime, timezone
    from release_earning.service import ReleaseEarning, WAIT_LINES
    reminded, assigned = [], iter([True, False, False])
    e = ReleaseEarning(user_id="u", last_spend_at=lambda: None, training_verified_since=lambda s: False, training_today=lambda n: False,
                       release_lines_done_since=lambda s: False, assign_release_lines=lambda s, n: next(assigned), remind=reminded.append)
    now = datetime(2026, 11, 25, 18, 0, tzinfo=timezone.utc)
    assert e.hold(now) == WAIT_LINES and reminded == []                    # assigned now: that message is enough
    assert e.hold(now) == WAIT_LINES and reminded == [now]

    def broken(_now):
        raise RuntimeError("boom")

    e._remind = broken
    assert e.hold(now) == WAIT_LINES                                       # a failing reminder never changes the hold
