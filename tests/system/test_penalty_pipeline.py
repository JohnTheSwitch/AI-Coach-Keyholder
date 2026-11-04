"""Phase J2, end to end through the real outbox: confirmed Incident -> Penalty Engine (Warning or Penalty Window) -> redemption hold and barrier
queue -> freeze on the configured keyholder lock. Each owner rule is an assertion here. No network."""

from __future__ import annotations

import ast
import io
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from chaster_execution.control import ExecutionControl
from chaster_execution.models import TransportOutcome, TransportResult
from chaster_execution.penalty_barrier import PenaltyBarrierExecutor
from cli.main import main, render_penalty_status
from infrastructure.clock import FrozenClock
from infrastructure.database import Database as CoreDatabase
from penalty_engine.models import FreezeReason
from peer_review.watch import RULE_GROUP
from peer_review.wiring import PEER_REVIEW_EVIDENCE
from release_engine.service import ReleaseGates
from rules_engine.penalty_hold import PenaltyHold
from supervision.escalation import EscalationDecisions
from system.penalty_policy import make_penalty_engine
from system.periodic import run_periodic_cycle
from trust_manager.models import ConfirmationSource, IncidentConfirmation
from trust_manager.repository import TrustManager

ROOT = Path(__file__).parent.parent.parent
NOW = datetime(2026, 11, 4, 10, 0, tzinfo=timezone.utc)
LOCK = "6aca95cdc5ee0f6d4989cd25"
DOMAIN = "routine"
AFTER_ANY_WINDOW = NOW + timedelta(days=40)                # beyond the 14-day hard cap of active time


class Transport:
    def __init__(self, outcome=TransportOutcome.OK, raises=None, probe=None) -> None:
        self.outcome, self.raises, self.probe, self.calls = outcome, raises, probe, []

    def set_frozen(self, *, lock_id, frozen):
        if self.probe:
            self.probe()
        self.calls.append((lock_id, frozen))
        if self.raises:
            raise self.raises
        return TransportResult(outcome=self.outcome, http_status=204 if self.outcome is TransportOutcome.OK else 500)


class World:
    def __init__(self, tmp_path, *, halted=False, enabled=True, role="keyholder", frozen=False, transport=None, read=None, lock_id=LOCK) -> None:
        self.core = CoreDatabase(tmp_path / "p.db")
        with self.core.raw_connection() as conn:
            for path in sorted((ROOT / "database" / "migrations").glob("*.sql")):
                conn.executescript(path.read_text(encoding="utf-8"))
        self.trust = TrustManager(self.core.db_path, core=self.core)
        self.trust.create_domain(domain_id=DOMAIN, display_name="R", description="d", created_via_consent_id="c", now=NOW - timedelta(days=30))
        self.control = ExecutionControl(self.core.db_path, core=self.core)
        self.control.set_halted(halted, reason="test", now=NOW)
        self.transport = transport if transport is not None else Transport()
        self.lock = {"role": role, "status": "locked", "isFrozen": frozen}
        self.reads = []

        def read_lock(lock):
            self.reads.append(lock)
            return dict(self.lock)

        self.barrier = PenaltyBarrierExecutor(self.core.db_path, core=self.core, enabled=enabled, control=self.control, transport=self.transport,
                                              read_lock=read or read_lock, lock_id=lock_id)
        self.n = 0

    def confirm_rejection(self, at=NOW):
        incident = self.trust.register_incident_report(rule_group_id=RULE_GROUP, trust_domain=DOMAIN, description="rejected", evidence=PEER_REVIEW_EVIDENCE, now=at)
        self.trust.confirm_incident(incident.id, new_confirmation=IncidentConfirmation.CONFIRMED, source=ConfirmationSource.KEYHOLDER_REVIEW,
                                    evidence_description="keyholder", now=at)
        return incident.id

    def cycle(self, at=NOW):
        self.n += 1
        assert run_periodic_cycle(self.core, f"p{self.n}", FrozenClock(at), rules_engine=None, executor=None, penalty_barrier=self.barrier)

    def q(self, sql, args=()):
        with self.core.transaction() as tx:
            return [dict(r) for r in tx.fetch_all(sql, args)]

    def intents(self):
        return [(r["action"], r["reason"], r["status"]) for r in self.q("SELECT * FROM penalty_barrier_intents ORDER BY created_at, rowid")]

    def window(self):
        return make_penalty_engine(self.core).get_active_or_frozen_penalty_window()

    def open_window(self, at=NOW):
        """Two confirmed rejections: the first is a Warning, the second starts the window."""
        self.confirm_rejection(at)
        self.cycle(at)
        self.confirm_rejection(at + timedelta(minutes=1))
        self.cycle(at + timedelta(minutes=1))
        return self.window()


class TestWarningThenWindow:
    def test_a_first_minor_rejection_is_a_warning_only(self, tmp_path) -> None:
        w = World(tmp_path)
        before = w.trust.get_domain_state(DOMAIN).score
        w.confirm_rejection()
        w.cycle()
        assert w.window() is None and w.intents() == [] and w.transport.calls == []
        assert len(w.q("SELECT * FROM incident_warnings")) == 1 and not PenaltyHold(w.core).is_active()
        assert w.trust.get_domain_state(DOMAIN).score < before                      # Trust still drops

    def test_a_repeated_rejection_opens_the_window_and_freezes_the_lock_once(self, tmp_path) -> None:
        w = World(tmp_path)
        assert w.open_window() is not None
        assert w.transport.calls == [(LOCK, True)] and w.intents() == [("freeze", "window_started", "executed")]
        assert PenaltyHold(w.core).is_active() and w.q("SELECT raised_freeze FROM penalty_holds") == [{"raised_freeze": 1}]
        w.cycle(NOW + timedelta(minutes=2))
        assert len(w.transport.calls) == 1

    def test_an_unconfirmed_incident_does_nothing(self, tmp_path) -> None:
        w = World(tmp_path)
        w.trust.register_incident_report(rule_group_id=RULE_GROUP, trust_domain=DOMAIN, description="r", evidence=PEER_REVIEW_EVIDENCE, now=NOW)
        w.cycle()
        assert w.window() is None and w.transport.calls == [] and w.q("SELECT * FROM incident_warnings") == []


class TestNoRawHours:
    def test_the_barrier_module_has_no_time_call(self) -> None:
        text = (ROOT / "chaster_execution" / "penalty_barrier.py").read_text(encoding="utf-8")
        calls = {n.func.attr for n in ast.walk(ast.parse(text)) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        assert calls >= {"set_frozen"} and not calls & {"add_time", "remove_time"}


class TestTokenRules:
    def test_redemption_is_blocked_by_the_hold_while_earning_has_no_gate(self, tmp_path) -> None:
        w = World(tmp_path)
        w.open_window()
        assert ReleaseGates(lock_barrier_confirmed=True, penalty_window_active=PenaltyHold(w.core).is_active()).hard_veto() == "penalty_window_active"
        for name in ("system/daily_evaluation.py", "release_engine/token_pool.py"):
            assert "penalty" not in (ROOT / name).read_text(encoding="utf-8").lower()

    def test_the_hold_ends_with_the_window(self, tmp_path) -> None:
        w = World(tmp_path)
        w.open_window()
        w.cycle(AFTER_ANY_WINDOW)
        assert w.window() is None and not PenaltyHold(w.core).is_active()


class TestWindowEnd:
    def test_time_up_ends_the_window_and_releases_what_it_raised(self, tmp_path) -> None:
        w = World(tmp_path)
        w.open_window()
        w.lock["isFrozen"] = True
        w.cycle(AFTER_ANY_WINDOW)
        assert w.transport.calls == [(LOCK, True), (LOCK, False)]
        assert w.intents()[-1] == ("unfreeze", "window_completed", "executed")

    def test_a_barrier_the_window_did_not_raise_stays_up(self, tmp_path) -> None:
        w = World(tmp_path, frozen=True)                    # already frozen, as the keyholder lock is today
        w.open_window()
        assert w.transport.calls == [] and w.intents() == [("freeze", "window_started", "skipped")]
        w.cycle(AFTER_ANY_WINDOW)
        assert w.transport.calls == [] and w.intents()[-1] == ("unfreeze", "window_completed", "skipped")


class TestExemption:
    def _exempt(self, w, reason=FreezeReason.TEMPORARY_WEAR_EXEMPTION):
        window = w.open_window()
        w.lock["isFrozen"] = True
        at = NOW + timedelta(hours=1)
        make_penalty_engine(w.core).freeze(window.id, reason, now=at, exemption_id="exemption-1", expires_at=at + timedelta(days=1))
        w.cycle(at)
        return window, at

    def test_an_approved_exemption_releases_the_lock_and_pauses_the_window(self, tmp_path) -> None:
        w = World(tmp_path)
        window, _ = self._exempt(w)
        assert w.transport.calls[-1] == (LOCK, False) and w.intents()[-1] == ("unfreeze", "exemption_started", "executed")
        assert w.window().status.value == "frozen" and PenaltyHold(w.core).is_active()          # paused, redemption still blocked

    def test_the_end_of_an_exemption_freezes_again(self, tmp_path) -> None:
        w = World(tmp_path)
        window, at = self._exempt(w)
        w.lock["isFrozen"] = False
        make_penalty_engine(w.core).resume(window.id, FreezeReason.TEMPORARY_WEAR_EXEMPTION, now=at + timedelta(hours=2))
        w.cycle(at + timedelta(hours=2))
        assert w.transport.calls[-1] == (LOCK, True) and w.intents()[-1] == ("freeze", "exemption_ended", "executed")

    def test_an_emergency_override_queues_nothing(self, tmp_path) -> None:
        w = World(tmp_path)
        window = w.open_window()
        count = len(w.intents())
        make_penalty_engine(w.core).emergency_freeze(window.id, now=NOW + timedelta(hours=1))
        w.cycle(NOW + timedelta(hours=1))
        assert len(w.intents()) == count and w.q("SELECT 1 FROM incidents WHERE confirmation = 'unconfirmed'") == []


class TestBackstop:
    def test_nothing_re_freezes_a_lock_the_backstop_released(self, tmp_path) -> None:
        w = World(tmp_path)
        w.open_window()
        w.lock["isFrozen"] = False                          # the 72 h safety release happened during the window
        for hours in (2, 3, 4):
            w.cycle(NOW + timedelta(hours=hours))
        assert w.transport.calls == [(LOCK, True)]

    def test_the_backstop_still_ignores_vetoes(self) -> None:
        text = (ROOT / "release_engine" / "service.py").read_text(encoding="utf-8")
        body = text[text.index("def commit_backstop"):text.index("# ------------------------------------------------------------------ provider progress")]
        assert "hard_veto" not in body and "penalty_window_active" not in body


class TestHalt:
    def test_while_halted_nothing_is_read_or_sent_and_the_intent_never_expires(self, tmp_path, caplog) -> None:
        w = World(tmp_path, halted=True)
        w.open_window()
        with caplog.at_level("WARNING"):
            w.cycle(NOW + timedelta(minutes=5))
        w.cycle(NOW + timedelta(hours=12))                  # far beyond the 15 minutes that would expire a lock decision
        assert w.transport.calls == [] and w.reads == [] and w.intents() == [("freeze", "window_started", "pending")]
        assert w.q("SELECT logged_at FROM penalty_barrier_intents")[0]["logged_at"] is not None

    def test_the_intended_action_is_logged_once(self, tmp_path, caplog) -> None:
        w = World(tmp_path, halted=True)
        with caplog.at_level("WARNING", logger="ai_coach_keyholder.chaster_execution.penalty_barrier"):
            w.open_window()
            for minutes in (5, 6, 7):
                w.cycle(NOW + timedelta(minutes=minutes))
        assert sum("QUEUED because the runtime is halted" in r.getMessage() for r in caplog.records) == 1

    def test_it_executes_when_the_halt_clears(self, tmp_path) -> None:
        w = World(tmp_path, halted=True)
        w.open_window()
        w.control.set_halted(False, reason="test", now=NOW + timedelta(days=2))
        w.cycle(NOW + timedelta(days=2))
        assert w.transport.calls == [(LOCK, True)] and w.intents() == [("freeze", "window_started", "executed")]

    def test_a_window_that_ended_during_the_halt_is_never_frozen_afterwards(self, tmp_path) -> None:
        w = World(tmp_path, halted=True)
        w.open_window()
        later = AFTER_ANY_WINDOW
        w.cycle(later)
        w.control.set_halted(False, reason="test", now=later)
        w.cycle(later + timedelta(minutes=1))
        assert w.transport.calls == [] and [i[2] for i in w.intents()] == ["skipped", "skipped"]

    def test_a_halt_set_between_the_claim_and_the_call_sends_nothing(self, tmp_path) -> None:
        w = World(tmp_path)
        w.confirm_rejection()
        w.cycle()
        w.confirm_rejection(NOW + timedelta(minutes=1))
        states = iter([False, True])
        w.control.is_halted = lambda: next(states)
        w.cycle(NOW + timedelta(minutes=1))
        assert w.transport.calls == [] and w.intents() == [("freeze", "window_started", "pending")]

    def test_an_unreadable_control_counts_as_halted(self, tmp_path) -> None:
        w = World(tmp_path)

        def boom():
            raise RuntimeError("db")

        w.control.is_halted = boom
        w.open_window()
        assert w.transport.calls == [] and w.intents()[0][2] == "pending"

    def test_switched_off_is_a_dry_run(self, tmp_path, caplog) -> None:
        w = World(tmp_path, enabled=False)
        with caplog.at_level("WARNING", logger="ai_coach_keyholder.chaster_execution.penalty_barrier"):
            w.open_window()
        assert w.transport.calls == [] and w.reads == [] and w.intents()[0][2] == "pending"
        assert any("switched off" in r.getMessage() for r in caplog.records)


class TestTargeting:
    def test_a_worn_lock_is_never_called(self, tmp_path) -> None:
        w = World(tmp_path, role="wearer", lock_id="6ae98ca410556a82f4b0d1aa")
        w.open_window()
        assert w.transport.calls == [] and w.intents() == [("freeze", "window_started", "skipped")]
        assert PenaltyHold(w.core).is_active()              # the internal block still applies
        assert "redemption hold only" in w.q("SELECT detail FROM penalty_barrier_intents")[0]["detail"]

    def test_only_the_configured_lock_is_called(self, tmp_path) -> None:
        w = World(tmp_path)
        w.open_window()
        assert {c[0] for c in w.transport.calls} == {LOCK} and set(w.reads) == {LOCK}

    @pytest.mark.parametrize("lock_id", [None, "", "../x"])
    def test_without_a_valid_lock_id_it_stays_a_dry_run(self, tmp_path, lock_id) -> None:
        w = World(tmp_path, lock_id=lock_id)
        w.open_window()
        assert w.transport.calls == [] and w.reads == [] and w.intents()[0][2] == "pending"

    def test_a_lock_that_is_not_locked_is_skipped(self, tmp_path) -> None:
        w = World(tmp_path)
        w.lock["status"] = "unlocked"
        w.open_window()
        assert w.transport.calls == [] and w.intents()[0][2] == "skipped"


class TestOutcomes:
    @pytest.mark.parametrize("outcome, status", [(TransportOutcome.FORBIDDEN, "failed"), (TransportOutcome.NOT_ELIGIBLE, "failed"),
                                                 (TransportOutcome.NOT_FOUND, "failed"), (TransportOutcome.UNKNOWN, "needs_review")])
    def test_a_final_outcome_is_recorded_and_never_retried(self, tmp_path, outcome, status) -> None:
        w = World(tmp_path, transport=Transport(outcome))
        w.open_window()
        w.cycle(NOW + timedelta(minutes=5))
        assert len(w.transport.calls) == 1 and w.intents()[0][2] == status

    @pytest.mark.parametrize("outcome", [TransportOutcome.NOT_SENT, TransportOutcome.UNAUTHORIZED])
    def test_a_call_that_reached_nothing_stays_queued(self, tmp_path, outcome) -> None:
        w = World(tmp_path, transport=Transport(outcome))
        w.open_window()
        assert w.intents()[0][2] == "pending"
        w.transport.outcome = TransportOutcome.OK
        w.cycle(NOW + timedelta(minutes=5))
        assert w.intents()[0][2] == "executed"

    def test_an_exception_during_the_call_is_ambiguous(self, tmp_path) -> None:
        w = World(tmp_path, transport=Transport(raises=RuntimeError("boom")))
        w.open_window()
        assert w.intents()[0][2] == "needs_review" and len(w.transport.calls) == 1

    def test_the_claim_is_committed_before_the_call(self, tmp_path) -> None:
        seen = []
        w = World(tmp_path, transport=Transport(probe=lambda: seen.append(w.intents()[0][2])))
        w.open_window()
        assert seen == ["executing"]

    def test_a_crash_after_the_claim_is_never_repeated(self, tmp_path) -> None:
        w = World(tmp_path, enabled=False)
        w.open_window()
        with w.core.transaction() as tx:
            tx.execute("UPDATE penalty_barrier_intents SET status = 'executing'")
        w.cycle(NOW + timedelta(minutes=5))
        assert w.intents()[0][2] == "needs_review" and w.transport.calls == []

    def test_a_failed_read_keeps_the_intent_queued(self, tmp_path) -> None:
        def down(lock):
            raise ConnectionError("down")

        w = World(tmp_path, read=down)
        w.open_window()
        assert w.transport.calls == [] and w.intents()[0][2] == "pending"

    def test_a_barrier_failure_never_stops_the_cycle(self, tmp_path) -> None:
        w = World(tmp_path)
        w.barrier = SimpleNamespace(run=lambda now: 1 / 0)
        w.cycle()


class TestEngineBoundaries:
    def test_the_rules_engine_recorder_never_evaluates_an_incident(self) -> None:
        tree = ast.parse((ROOT / "rules_engine" / "penalty_hold.py").read_text(encoding="utf-8"))
        names = [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names] + \
                [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module]
        assert not [n for n in names if n.startswith(("penalty_engine", "trust_manager", "release_engine", "chaster"))]

    def test_a_redelivered_event_cannot_queue_twice(self, tmp_path) -> None:
        from rules_engine.penalty_hold import on_window_started
        w = World(tmp_path)
        event = SimpleNamespace(id="e1", payload={"penalty_window_id": "w1"}, occurred_at=NOW)
        with w.core.transaction() as tx:
            on_window_started(tx, event)
            on_window_started(tx, event)
        assert len(w.q("SELECT * FROM penalty_barrier_intents")) == 1 and len(w.q("SELECT * FROM penalty_holds")) == 1

    def test_tenants_get_no_barrier(self) -> None:
        text = (ROOT / "system" / "composition.py").read_text(encoding="utf-8")
        assert "if observation_source is None:           # the owner's own system only" in text


class TestCli:
    def _run(self, w, *args):
        out = io.StringIO()
        decisions = EscalationDecisions(w.core.db_path, core=w.core, owner_user_id="owner", confirm_incident=lambda *a: None)
        return main(list(args), out=out, escalation_factory=lambda: (decisions, w.core), now=lambda: NOW), out.getvalue()

    def test_penalty_status(self, tmp_path) -> None:
        w = World(tmp_path, halted=True)
        w.open_window()
        code, text = self._run(w, "penalty-status", "--owner-id", "owner")
        assert code == 0 and "BLOCKED by a Penalty Window" in text and "freeze" in text and "pending" in text
        assert self._run(w, "penalty-status", "--owner-id", "x")[0] == 3

    def test_an_empty_status(self, tmp_path) -> None:
        assert "Barrier queue: empty" in render_penalty_status(World(tmp_path).core)

    def test_escalations_list(self, tmp_path) -> None:
        code, text = self._run(World(tmp_path), "check-in-escalations", "list", "--owner-id", "owner")
        assert code == 0 and text.strip() == "No check-in escalations."

    def test_every_escalation_outcome_has_a_text(self) -> None:
        from cli.main import _ESCALATION_TEXT
        from supervision.escalation import EscalationOutcome
        assert set(_ESCALATION_TEXT) == {o.value for o in EscalationOutcome}
