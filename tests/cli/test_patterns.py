"""Learning options 2 and 3 (owner, 2026-11-16: report and propose only): `python -m cli patterns`."""

from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone

from cli.main import run_patterns
from cli.patterns import patterns_report
from tests.coach.test_desk import World

NOW = datetime(2026, 12, 20, 12, 0, tzinfo=timezone.utc)
U = "u"


def _utc(t: datetime) -> datetime:
    return t


def _check_in(tx, i: int, due: datetime, status: str) -> None:
    tx.execute("INSERT INTO check_ins (id, user_id, local_date, slot_index, level, due_at, status, created_at, updated_at) "
               "VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)", (f"c{i}", U, due.date().isoformat(), i, due.isoformat(), status, due.isoformat(), due.isoformat()))


def test_a_repeated_miss_becomes_a_proposal_and_one_miss_does_not(tmp_path) -> None:
    w = World(tmp_path)
    wednesdays = [NOW - timedelta(days=d) for d in (4, 11, 18)]                     # 16, 9, 2 Dec: Wednesdays
    assert all(t.weekday() == 2 for t in wednesdays)
    with w.core.transaction() as tx:
        for i, t in enumerate(wednesdays):
            _check_in(tx, i, t.replace(hour=18), "missed")
        _check_in(tx, 10, (NOW - timedelta(days=3)).replace(hour=18), "answered")
        _check_in(tx, 11, (NOW - timedelta(days=2)).replace(hour=10), "missed")
    text = patterns_report(w.core, U, NOW, _utc)
    assert "Check-ins: 4 of 5 missed or insufficient." in text
    assert "Check-ins on Wed are often missed (3 of 3)" in text and "in the evening (17-21) are often missed (3 of 4)" in text
    assert "late morning (9-13) are often" not in text                                 # a single miss is no pattern


def test_nothing_before_the_cut_off_counts(tmp_path) -> None:
    w = World(tmp_path)
    with w.core.transaction() as tx:
        for i, d in enumerate((4, 11, 18)):
            _check_in(tx, i, (NOW - timedelta(days=d)).replace(hour=18), "missed")
    text = patterns_report(w.core, U, NOW, _utc, counts_from=NOW - timedelta(days=5))
    assert "Check-ins: 1 of 1" in text and "Proposals: none yet" in text


def test_what_came_before_a_clean_week(tmp_path) -> None:
    w = World(tmp_path)
    with w.core.transaction() as tx:
        for i, (mode, days_ago) in enumerate(((None, 20), (None, 15), ("swats", 12))):
            at = NOW - timedelta(days=days_ago)
            tx.execute("INSERT INTO line_assignments (id, user_id, incident_id, rule_group_id, sentence, required, done, status, created_at, mode) "
                       "VALUES (?, ?, ?, 'g', 's', 20, 20, 'done', ?, ?)", (f"l{i}", U, f"i{i}", at.isoformat(), mode))
        _check_in(tx, 1, NOW - timedelta(days=17), "missed")                          # inside the week after the first lines only
    text = patterns_report(w.core, U, NOW, _utc)
    assert "after lines: 1 of 2 weeks clean" in text and "after swats instead: 1 of 1 weeks clean" in text
    assert "praise and teasing: not measured" in text


def test_the_command_writes_nothing(tmp_path) -> None:
    w = World(tmp_path)
    before = w.core.db_path.read_bytes()
    out = io.StringIO()
    assert run_patterns(out=out, now=lambda: NOW, factory=lambda: (w.core, U, _utc)) == 0
    assert out.getvalue().startswith("Patterns since") and w.core.db_path.read_bytes() == before
