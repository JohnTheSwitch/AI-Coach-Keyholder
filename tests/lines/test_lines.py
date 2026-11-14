"""Brat taming B2 (owner, 2026-11-14): lines for every confirmed incident, counted by code; they hold planned hygiene and, as release debt (B4),
a release. A health need never waits."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from infrastructure.database import Database as CoreDatabase
from lines.service import LINES_WARNING, LINES_WINDOW, LineService, normalize, sentence_for
from release_engine.service import ReleaseGates
from tests.exemptions.test_exemptions import WEARER
from tests.exemptions.test_hygiene_requests import World as HygieneWorld
from tests.system.test_tenant_factory import ROOT

NOW = datetime(2026, 11, 14, 20, 0, tzinfo=timezone.utc)
U = "wearer"


def core(tmp_path) -> CoreDatabase:
    db = CoreDatabase(tmp_path / "l.db")
    with db.raw_connection() as conn:
        for path in sorted((ROOT / "database" / "migrations").glob("*.sql")):
            conn.executescript(path.read_text(encoding="utf-8"))
    return db


def event(kind, incident="inc-1", rule="HYGIENE_MISUSE"):
    return SimpleNamespace(event_type=kind, payload={"incident_id": incident, "rule_group_id": rule}, occurred_at=NOW)


def assign(service, db, kind, **kw):
    with db.transaction() as tx:
        service.consumers()[kind](tx, event(kind, **kw))


def rows(db, sql, *args):
    with db.transaction() as tx:
        return [dict(r) for r in tx.fetch_all(sql, args)]


def test_sentences_and_matching() -> None:
    assert sentence_for("HYGIENE_MISUSE") == "I use hygiene breaks only for hygiene."
    assert sentence_for("SOMETHING_NEW") == "I follow the rules I agreed to."
    assert normalize("12.  i use HYGIENE breaks only for hygiene") == normalize("I use hygiene breaks only for hygiene.")
    assert normalize("3) I answer my keyholder’s questions!") == normalize("I answer my keyholder's questions.")
    assert normalize("I use hygiene breaks for hygiene.") != normalize("I use hygiene breaks only for hygiene.")


class TestAssignment:
    def test_a_warning_gets_20_and_a_window_40_once_per_incident(self, tmp_path) -> None:
        db = core(tmp_path)
        s = LineService(db, user_id=U)
        assign(s, db, "penalty_warning.recorded", incident="a", rule="FALSE_REPORT")
        assign(s, db, "extension.decision_recorded", incident="b")
        assign(s, db, "extension.decision_recorded", incident="b")                      # a replay
        assert [(r["incident_id"], r["required"]) for r in rows(db, "SELECT * FROM line_assignments ORDER BY incident_id")] == [
            ("a", LINES_WARNING), ("b", LINES_WINDOW)]
        assert s.open_lines(U) == 60
        notes = rows(db, "SELECT text FROM notifications WHERE kind = 'lines' ORDER BY created_at, rowid")
        assert len(notes) == 2 and '"I report only what I really did."' in notes[0]["text"] and "A health need never waits." in notes[0]["text"]
        assert len(rows(db, "SELECT 1 FROM domain_events WHERE event_type = 'lines.assigned'")) == 2

    def test_no_incident_id_no_lines(self, tmp_path) -> None:
        db = core(tmp_path)
        s = LineService(db, user_id=U)
        with db.transaction() as tx:
            s.consumers()["penalty_warning.recorded"](tx, SimpleNamespace(payload={}, occurred_at=NOW))
        assert s.open_lines(U) == 0


class TestWriting:
    def test_counted_across_messages_and_assignments(self, tmp_path) -> None:
        db = core(tmp_path)
        s = LineService(db, user_id=U)
        assign(s, db, "penalty_warning.recorded", incident="a")                          # 20 x hygiene
        assign(s, db, "penalty_warning.recorded", incident="b", rule="FALSE_REPORT")     # 20 x false report
        hygiene = "I use hygiene breaks only for hygiene."
        reply = s.take(U, "\n".join([f"{i}. {hygiene}" for i in range(1, 16)] + ["I use breaks for fun."]), NOW)
        assert reply.text == "15 counted, 25 to go. 1 line didn't match exactly and didn't count."
        reply = s.take(U, "\n".join([hygiene] * 8 + ["I report only what I really did."] * 20), NOW)
        assert reply.text == "25 counted. That's all of them. Good."            # 5 hygiene (3 extra ignored) + 20
        assert s.open_lines(U) == 0
        assert len(rows(db, "SELECT 1 FROM domain_events WHERE event_type = 'lines.completed'")) == 2

    def test_an_ordinary_message_is_not_lines(self, tmp_path) -> None:
        db = core(tmp_path)
        s = LineService(db, user_id=U)
        assert s.take(U, "I use hygiene breaks only for hygiene.", NOW) is None          # nothing owed
        assign(s, db, "penalty_warning.recorded")
        assert s.take(U, "How are you?", NOW) is None and s.take("someone-else", "I use hygiene breaks only for hygiene.", NOW) is None


class TestWhatWaits:
    def test_planned_hygiene_waits_but_a_health_need_never_does(self, tmp_path) -> None:
        w = HygieneWorld(tmp_path)
        w.service.lines_open = lambda user_id: 12
        card = w.ask()
        assert card.text == "Finish your lines first: 12 to go. A health need never waits for them."
        assert [a.label for a in card.actions] == ["It's a health need", "Not now"]
        w.service.lines_open = lambda user_id: 0
        card = w.ask()
        w.service.lines_open = lambda user_id: 5                                         # an old card's Open it
        assert "Finish your lines first" in w.press(card, "Open it").text and w.opening.opens == []

    def test_a_read_failure_never_holds_hygiene(self, tmp_path) -> None:
        w = HygieneWorld(tmp_path)

        def boom(user_id):
            raise RuntimeError("x")
        w.service.lines_open = boom
        assert [a.label for a in w.ask().actions] == ["Open it", "It's a health need", "Not now"]

    def test_owed_lines_hold_a_release(self) -> None:
        assert ReleaseGates(lock_barrier_confirmed=True, release_debt=True).hard_veto() == "release_debt"
        assert ReleaseGates(lock_barrier_confirmed=True, penalty_window_active=True, release_debt=True).hard_veto() == "penalty_window_active"
        assert ReleaseGates(lock_barrier_confirmed=True).hard_veto() is None

    def test_the_release_tick_reads_the_debt_and_fails_open(self) -> None:
        from system.release_tick import ReleaseEnginePeriodicTask
        task = ReleaseEnginePeriodicTask.__new__(ReleaseEnginePeriodicTask)
        task.release_debt = None
        assert task._debt_owed() is False
        task.release_debt = lambda: 7
        assert task._debt_owed() is True
        task.release_debt = lambda: (_ for _ in ()).throw(RuntimeError("x"))
        assert task._debt_owed() is False


def test_the_dm_counts_lines_before_any_model(tmp_path) -> None:
    from application.service import ApplicationService
    from tests.application.test_lock_and_task_commands import FIXED_TIME, _complete_onboarding, _incoming
    db = core(tmp_path)
    service = ApplicationService(db.db_path, core=db)
    _complete_onboarding(service)
    with db.transaction() as tx:
        user_id = tx.fetch_one("SELECT id FROM user_accounts")["id"]
    lines = LineService(db, user_id=user_id)
    service.lines = lines
    assign(lines, db, "penalty_warning.recorded")
    reply = service.handle_message(_incoming("I use hygiene breaks only for hygiene.\nI use hygiene breaks only for hygiene.", now=FIXED_TIME))
    assert reply.text == "2 counted, 18 to go."
    assert WEARER  # the hygiene world's wearer id is imported for the helpers above


class TestOnPaper:
    """Owner, 2026-11-14: lines are handwritten and shown as a Chaster task; typing counts only after the slot stayed taken for 3 hours."""

    def _paper(self, db, chaster, slot=False):
        from lines.service import PaperPort
        s = LineService(db, user_id=U)
        s.paper = PaperPort(chaster=chaster, lock_id=lambda now: "lock", halted=lambda: False, slot_taken=lambda: slot)
        return s

    def test_a_sheet_is_assigned_verified_and_done(self, tmp_path) -> None:
        from tests.recovery_tasks.test_recovery_tasks import FakeChaster
        db, chaster = core(tmp_path), FakeChaster()
        s = self._paper(db, chaster)
        assign(s, db, "penalty_warning.recorded")
        assert "by hand on paper" in rows(db, "SELECT text FROM notifications WHERE kind = 'lines'")[0]["text"]
        s.paper_tick(NOW)
        assert chaster.assigned == ["Show: 20 handwritten lines: I use hygiene breaks only for hygiene."]
        assert s.is_lines_task("t1") and s.has_open_task()
        s.paper_tick(NOW)                                                   # nothing submitted yet: still open
        assert s.open_lines(U) == 20
        chaster.entries["t1"] = {"createdAt": NOW.isoformat(), "peerVerification": {"status": "verified"}}
        s.paper_tick(NOW)
        assert s.open_lines(U) == 0 and len(rows(db, "SELECT 1 FROM domain_events WHERE event_type = 'lines.completed'")) == 1

    def test_a_rejected_sheet_is_written_again(self, tmp_path) -> None:
        from tests.recovery_tasks.test_recovery_tasks import FakeChaster
        db, chaster = core(tmp_path), FakeChaster()
        s = self._paper(db, chaster)
        assign(s, db, "penalty_warning.recorded")
        s.paper_tick(NOW)
        chaster.entries["t1"] = {"createdAt": NOW.isoformat(), "peerVerification": {"status": "rejected"}}
        chaster.current = None
        s.paper_tick(NOW)
        assert s.open_lines(U) == 20 and not s.has_open_task()
        assert any("rejected" in r["text"] for r in rows(db, "SELECT text FROM notifications WHERE kind = 'lines'"))
        s.paper_tick(NOW)
        assert len(chaster.assigned) == 2

    def test_typing_counts_only_after_the_slot_stayed_taken(self, tmp_path) -> None:
        from tests.recovery_tasks.test_recovery_tasks import FakeChaster
        db, chaster = core(tmp_path), FakeChaster()
        s = self._paper(db, chaster, slot=True)
        assign(s, db, "penalty_warning.recorded")
        s.paper_tick(NOW)
        assert chaster.assigned == []                                       # a spot check holds the slot
        line = "I use hygiene breaks only for hygiene."
        assert "Lines go on paper" in s.take(U, line, NOW).text and s.open_lines(U) == 20
        assert s.take(U, "How are you?", NOW) is None
        assert s.take(U, line, NOW + timedelta(hours=3)).text == "1 counted, 19 to go."
