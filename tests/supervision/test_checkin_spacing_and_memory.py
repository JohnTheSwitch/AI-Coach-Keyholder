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
        rows = [{"role": "user", "content": "first", "from_model": None}, {"role": "assistant", "content": "reply one", "from_model": 1},
                {"role": "user", "content": "second", "from_model": None}, {"role": "assistant", "content": "reply two", "from_model": 1},
                {"role": "user", "content": "unanswered", "from_model": None}]
        assert seed_working_memory(memory, Core(rows), subject_key=U, since=NOW - timedelta(hours=24)) == 2
        turns = [t.content for t in memory.read(subject_key=U).turns]
        assert turns == ["first", "reply one", "second", "reply two"]

    def test_only_the_models_own_replies_come_back(self) -> None:
        """2026-11-13: status cards and command replies came back as the model's own words ("Active Locks (2)")."""
        memory = InMemoryWorkingMemory(max_exchanges_per_subject=10, max_characters_per_subject=8000)
        rows = [{"role": "user", "content": "what is my lock", "from_model": None}, {"role": "assistant", "content": "Active Locks (2)", "from_model": 0},
                {"role": "user", "content": "old", "from_model": None}, {"role": "assistant", "content": "before the column", "from_model": None},
                {"role": "user", "content": "hi", "from_model": None}, {"role": "assistant", "content": "hey you", "from_model": 1}]
        assert seed_working_memory(memory, Core(rows), subject_key=U, since=NOW - timedelta(hours=24)) == 1
        assert [t.content for t in memory.read(subject_key=U).turns] == ["hi", "hey you"]

    def test_a_failure_only_means_a_fresh_start(self) -> None:
        class Broken:
            def transaction(self):
                raise RuntimeError("db")

        memory = InMemoryWorkingMemory(max_exchanges_per_subject=10, max_characters_per_subject=8000)
        assert seed_working_memory(memory, Broken(), subject_key=U, since=NOW) == 0
        assert seed_working_memory(memory, Core([]), subject_key=None, since=NOW) == 0


class TestTheChatFixes20261113:
    def test_a_check_in_answer_is_a_report(self) -> None:
        from system.coach_dm import CHECK_IN_GUIDANCE
        assert "not the goal" in CHECK_IN_GUIDANCE and "report on their day" in CHECK_IN_GUIDANCE

    def test_a_veto_means_requirements_are_not_met_for_the_model(self) -> None:
        from types import SimpleNamespace
        from system.composition import RulesExecutionWiring as ReleaseWiring
        class Engine:
            def coarse_phase(self, _u):
                return "requirements_met_pending"
        w = ReleaseWiring()
        w.release_engine, w.release_user_id = Engine(), "u"
        w.supervision = SimpleNamespace(is_active=lambda: True)
        assert w.release_phase("u") == "requirements_not_met"
        w.supervision = SimpleNamespace(is_active=lambda: False)
        w.penalty_hold = SimpleNamespace(is_active=lambda: False)
        assert w.release_phase("u") == "requirements_met_pending"

    def test_the_bot_stores_who_wrote_a_reply(self) -> None:
        from pathlib import Path
        assert "from_model=bool(getattr(reply, \"from_model\", False))" in Path("bot/discord_bot.py").read_text(encoding="utf-8")


class TestBothLoggingPathsRecordWhoWrote:
    def test_the_owners_path_too(self) -> None:
        """Live, 2026-11-13: the non-gateway path logged no `from_model`, so a restart restored no conversation at all."""
        from pathlib import Path
        text = Path("bot/discord_bot.py").read_text(encoding="utf-8")
        assert "sent.id, from_model=from_model)" in text and "from_model = bool(getattr(outgoing, \"from_model\", False))" in text
