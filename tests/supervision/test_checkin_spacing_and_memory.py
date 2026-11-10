"""Owner, 2026-11-10: "the bot keeps forgetting that it already made a check-in with me". Two causes, two fixes. No network."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from memory_system.working_memory import InMemoryWorkingMemory
from supervision.checkins import QUIET_AFTER_ANSWER
from system.memory_seed import seed_working_memory
from infrastructure.time_format import iso
from tests.supervision.test_checkins import MON, U, Env, local


class TestNoSecondCheckInRightAfterAnAnswer:
    def _env(self, tmp_path, answered_ago):
        e = Env(tmp_path)
        due = local(MON, 18, 0)
        with e.core.raw_connection() as conn:
            conn.execute("INSERT INTO check_ins (id, user_id, local_date, slot_index, level, due_at, status, created_at, updated_at, responded_at) "
                         "VALUES ('a', ?, ?, 0, 2, ?, 'answered', ?, ?, ?)", (U, MON.isoformat(), iso(due - answered_ago - timedelta(minutes=5)), iso(due), iso(due),
                                                                              iso(due - answered_ago)))
            conn.execute("INSERT INTO check_ins (id, user_id, local_date, slot_index, level, due_at, status, created_at, updated_at) "
                         "VALUES ('b', ?, ?, 1, 2, ?, 'scheduled', ?, ?)", (U, MON.isoformat(), iso(due), iso(due), iso(due)))
            conn.commit()
        e.svc.tick(due)
        return e

    def test_a_slot_an_hour_after_an_answer_is_skipped(self, tmp_path) -> None:
        e = self._env(tmp_path, timedelta(hours=1))
        assert e.sender.sent == [] and [r["status"] for r in e.rows() if r["id"] == "b"] == ["skipped"]

    def test_a_slot_later_goes_out(self, tmp_path) -> None:
        e = self._env(tmp_path, timedelta(hours=4))
        assert len(e.sender.sent) == 1

    def test_the_gap(self) -> None:
        assert QUIET_AFTER_ANSWER == timedelta(hours=3)


class Core:
    def __init__(self, rows) -> None:
        self.rows = rows

    class _Tx:
        def __init__(self, rows) -> None:
            self.rows = rows

        def fetch_all(self, _sql, _args):
            return self.rows

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    def transaction(self):
        return Core._Tx(self.rows)


NOW = datetime(2026, 11, 10, 18, 0, tzinfo=timezone.utc)


class TestARestartKeepsTheConversation:
    def test_stored_exchanges_come_back_in_order(self) -> None:
        memory = InMemoryWorkingMemory(max_exchanges_per_subject=10, max_characters_per_subject=8000)
        rows = [{"role": "user", "content": "first"}, {"role": "assistant", "content": "reply one"},
                {"role": "user", "content": "second"}, {"role": "assistant", "content": "reply two"}, {"role": "user", "content": "unanswered"}]
        assert seed_working_memory(memory, Core(rows), subject_key=U, since=NOW - timedelta(hours=24)) == 2
        turns = [t.content for t in memory.read(subject_key=U).turns]
        assert turns == ["first", "reply one", "second", "reply two"]

    def test_a_failure_only_means_a_fresh_start(self) -> None:
        class Broken:
            def transaction(self):
                raise RuntimeError("db")

        memory = InMemoryWorkingMemory(max_exchanges_per_subject=10, max_characters_per_subject=8000)
        assert seed_working_memory(memory, Broken(), subject_key=U, since=NOW) == 0
        assert seed_working_memory(memory, Core([]), subject_key=None, since=NOW) == 0
