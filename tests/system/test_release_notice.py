"""Phase J2, the Communication-First release: the user is told before a release, an incomplete check-in is requested in that same message, and a
reply releases them. A release is never held back silently. Fake transport and sender: no live call of any kind, and nothing here can unlock."""

from __future__ import annotations

from datetime import timedelta

import pytest

from supervision.checkin_evaluator import Verdict
from supervision.checkins import RELEASE_CHECK_IN_VALIDITY, RELEASE_SLOT, CheckInService, SendOutcome
from system.release_notice import AFTER_CHECK_IN, CHECK_IN_PROMPT, ELIGIBLE_NOW, WINDOW_CLOSED, ReleaseNotifier
from tests.supervision.test_checkins import Sender
from tests.system.test_release_tick import CFG, MON, U, World, local
from trust_manager.repository import TrustManager


class Harness(World):
    """The real release tick and the real check-in service, recording one timeline of everything the user or Chaster sees."""

    def __init__(self, tmp_path, *, score=0.6, verdict=Verdict.SUFFICIENT, outcome=SendOutcome.DELIVERED, check_ins=True, paused=False, **kw) -> None:
        super().__init__(tmp_path, **kw)
        self.timeline: list[tuple[str, str]] = []
        self.sender = Sender(outcome=outcome)
        timeline, sender = self.timeline, self.sender

        def send(*, user_id, text):
            timeline.append(("message", text))
            return Sender.send(sender, user_id=user_id, text=text)

        self.sender_send = send
        original = self.transport.set_frozen

        def set_frozen(*, lock_id, frozen):
            timeline.append(("unfreeze" if not frozen else "freeze", lock_id))
            return original(lock_id=lock_id, frozen=frozen)

        self.transport.set_frozen = set_frozen
        TrustManager(self.core.db_path, core=self.core).create_domain(
            domain_id="d", display_name="D", description="d", created_via_consent_id="c", now=local(MON, 9))
        self.score, self.verdict = score, verdict
        self.check_ins = CheckInService(
            self.core.db_path, core=self.core, window_config=CFG, user_id=U, trust_domain="d", get_trust_score=lambda: self.score,
            sender=type("S", (), {"send": staticmethod(send)})(), evaluate=lambda prompt, reply: self.verdict, rand_fraction=lambda: 0.5,
            paused=lambda now: paused)
        self.task.notifier = ReleaseNotifier(self.core, user_id=U, send=lambda text: send(user_id=U, text=text),
                                             today=lambda now: CFG.local(now).date(), check_ins=self.check_ins if check_ins else None)

    def go_to_due(self):
        self.tokens()
        self.cycle()
        target = self.target()
        report = self.cycle(target)
        return target, report

    def tick(self, instant):
        self.check_ins.tick(instant)
        return self.cycle(instant)

    def messages(self):
        return [text for kind, text in self.timeline if kind == "message"]

    def rows(self):
        with self.core.transaction() as tx:
            return [dict(r) for r in tx.fetch_all("SELECT * FROM check_ins WHERE slot_index = ?", (RELEASE_SLOT,))]

    def trust_evidence(self):
        with self.core.transaction() as tx:
            return tx.fetch_one("SELECT COUNT(*) AS n FROM trust_evidence")["n"]


class TestStep1And2:
    def test_the_eligibility_notice_and_the_prompt_come_first_and_nothing_is_released_or_spent(self, tmp_path) -> None:
        w = Harness(tmp_path)
        _, report = w.go_to_due()
        assert w.timeline == [("message", CHECK_IN_PROMPT)] and "release_waiting_for_check_in" in report.notes
        assert w.transport.calls == [] and w.engine.get_balance(U) == 1 and w.decisions() == []

    def test_the_prompt_uses_the_owner_wording(self) -> None:
        assert CHECK_IN_PROMPT.startswith("You're eligible for release today! Please complete your quick daily check-in first to execute the unfreeze.")

    def test_the_prompt_goes_out_once_however_often_the_cycle_runs(self, tmp_path) -> None:
        w = Harness(tmp_path)
        target, _ = w.go_to_due()
        for minutes in (1, 2, 3, 4):
            w.cycle(target + timedelta(minutes=minutes))
        assert w.messages() == [CHECK_IN_PROMPT]

    def test_nothing_is_announced_before_the_plan_is_due(self, tmp_path) -> None:
        """The plan's time stays secret: the notice is sent at the moment of release, never when the plan is made."""
        w = Harness(tmp_path)
        w.tokens()
        w.cycle()
        w.cycle(w.target() - timedelta(minutes=1))
        assert w.timeline == [] and w.engine.has_planned_release(U)

    @pytest.mark.parametrize("attr", ["penalty", "supervised"])
    def test_a_vetoed_release_is_not_announced(self, tmp_path, attr) -> None:
        w = Harness(tmp_path)
        w.tokens()
        w.cycle()
        setattr(w, attr, True)
        w.cycle(w.target())
        assert w.timeline == []

    def test_a_halted_system_announces_nothing(self, tmp_path) -> None:
        w = Harness(tmp_path)
        w.tokens()
        w.cycle()
        w.control.set_halted(True, reason="test", now=w.now)
        w.cycle(w.target())
        assert w.timeline == [] and w.transport.calls == []

    def test_outside_a_discretionary_window_it_is_not_announced_and_only_retargets(self, tmp_path) -> None:
        w = Harness(tmp_path)
        w.tokens()
        w.cycle()
        w.cycle(local(MON, 23, 30))                       # quiet hours
        assert w.timeline == []

    def test_no_message_carries_a_time_or_a_token_count(self) -> None:
        for text in (ELIGIBLE_NOW, CHECK_IN_PROMPT, AFTER_CHECK_IN, WINDOW_CLOSED):
            assert "token" not in text.lower() and not any(c.isdigit() for c in text) and "trust" not in text.lower()


class TestStep3:
    def test_submitting_the_check_in_releases_in_the_next_cycle_and_the_order_holds(self, tmp_path) -> None:
        w = Harness(tmp_path)
        target, _ = w.go_to_due()
        at = target + timedelta(minutes=2)
        assert w.check_ins.on_user_message(U, "good day, all done", at) is True
        report = w.tick(at + timedelta(seconds=30))
        assert "commit:authorized" in report.notes and w.transport.calls == [("unfreeze", "lock-1")]
        assert [k for k, _ in w.timeline] == ["message", "message", "unfreeze"]
        assert w.timeline[:2] == [("message", CHECK_IN_PROMPT), ("message", AFTER_CHECK_IN)]
        assert w.engine.get_balance(U) == 0

    @pytest.mark.parametrize("verdict", [Verdict.INSUFFICIENT, Verdict.UNCERTAIN, Verdict.NOT_EVALUATED])
    def test_submitting_is_what_counts_whatever_the_rubric_says(self, tmp_path, verdict) -> None:
        w = Harness(tmp_path, verdict=verdict)
        target, _ = w.go_to_due()
        at = target + timedelta(minutes=2)
        w.check_ins.on_user_message(U, "ok", at)
        w.tick(at + timedelta(seconds=30))
        assert w.transport.calls == [("unfreeze", "lock-1")] and w.rows()[0]["status"] == "answered_uncertain"

    def test_a_reply_is_stored_on_the_release_check_in_even_with_a_scheduled_one_open(self, tmp_path) -> None:
        w = Harness(tmp_path)
        target, _ = w.go_to_due()
        w.check_ins.on_user_message(U, "fine", target + timedelta(minutes=1))
        assert w.rows()[0]["status"] == "responded"


class TestNeverSilent:
    def test_a_check_in_already_done_today_is_told_and_released_at_once(self, tmp_path) -> None:
        w = Harness(tmp_path)
        with w.core.transaction() as tx:
            tx.execute("INSERT INTO check_ins (id, user_id, local_date, slot_index, level, due_at, status, created_at, updated_at) "
                       "VALUES ('c', ?, ?, 0, 2, 'x', 'answered', 'x', 'x')", (U, MON.isoformat()))
        target, report = w.go_to_due()
        assert "commit:authorized" in report.notes
        assert w.timeline == [("message", ELIGIBLE_NOW), ("unfreeze", "lock-1")]

    def test_a_missed_scheduled_check_in_does_not_block_once_the_prompt_is_answered(self, tmp_path) -> None:
        w = Harness(tmp_path)
        with w.core.transaction() as tx:
            tx.execute("INSERT INTO check_ins (id, user_id, local_date, slot_index, level, due_at, status, created_at, updated_at) "
                       "VALUES ('c', ?, ?, 0, 2, 'x', 'missed', 'x', 'x')", (U, MON.isoformat()))
        target, _ = w.go_to_due()
        w.check_ins.on_user_message(U, "sorry, here it is", target + timedelta(minutes=1))
        w.tick(target + timedelta(minutes=2))
        assert w.transport.calls == [("unfreeze", "lock-1")]

    def test_without_check_ins_at_all_the_user_is_told_and_released(self, tmp_path) -> None:
        w = Harness(tmp_path, check_ins=False)
        w.go_to_due()
        assert w.timeline == [("message", ELIGIBLE_NOW), ("unfreeze", "lock-1")]

    def test_at_level_4_no_check_in_applies(self, tmp_path) -> None:
        w = Harness(tmp_path, score=0.9)
        w.go_to_due()
        assert w.timeline == [("message", ELIGIBLE_NOW), ("unfreeze", "lock-1")]

    def test_during_a_vacation_no_check_in_applies(self, tmp_path) -> None:
        w = Harness(tmp_path, paused=True)
        w.go_to_due()
        assert w.transport.calls == [("unfreeze", "lock-1")]

    def test_an_undeliverable_prompt_never_becomes_a_silent_block(self, tmp_path) -> None:
        w = Harness(tmp_path, outcome=SendOutcome.UNDELIVERABLE)
        w.go_to_due()
        assert w.transport.calls == [("unfreeze", "lock-1")]

    def test_an_ambiguous_delivery_stays_open_for_a_reply(self, tmp_path) -> None:
        w = Harness(tmp_path, outcome=SendOutcome.UNKNOWN)
        target, _ = w.go_to_due()
        assert w.transport.calls == [] and w.rows()[0]["status"] == "sent"
        w.check_ins.on_user_message(U, "here", target + timedelta(minutes=1))
        w.tick(target + timedelta(minutes=2))
        assert w.transport.calls == [("unfreeze", "lock-1")]

    def test_a_failing_notice_never_stops_a_release(self, tmp_path) -> None:
        w = Harness(tmp_path, check_ins=False)

        def boom(text):
            raise RuntimeError("discord down")

        w.task.notifier._send = boom
        w.go_to_due()
        assert w.transport.calls == [("unfreeze", "lock-1")]

    def test_a_window_that_closes_while_waiting_is_explained_once(self, tmp_path) -> None:
        w = Harness(tmp_path)
        target, _ = w.go_to_due()
        late = local(MON, 22, 30)
        w.check_ins.on_user_message(U, "done", late - timedelta(minutes=1))
        w.tick(late)
        w.tick(late + timedelta(minutes=1))
        assert w.messages().count(WINDOW_CLOSED) == 1 and w.transport.calls == []
        assert w.engine.has_planned_release(U) and w.engine.get_balance(U) == 1


class TestNeverATrap:
    def test_ignoring_the_prompt_costs_nothing(self, tmp_path) -> None:
        w = Harness(tmp_path)
        target, _ = w.go_to_due()
        before = w.trust_evidence()
        w.tick(target + RELEASE_CHECK_IN_VALIDITY + timedelta(minutes=1))
        assert w.rows()[0]["status"] == "skipped" and w.trust_evidence() == before and w.transport.calls == []

    def test_the_backstop_is_never_held_up_by_a_pending_check_in(self, tmp_path) -> None:
        w = Harness(tmp_path, frozen_since=timedelta(days=4))
        report = w.go_to_due()[1]
        assert "release_waiting_for_check_in" in report.notes and "backstop:authorized" in report.notes


class TestSafety:
    def test_the_transport_can_only_freeze_and_has_no_unlock(self) -> None:
        from chaster_execution.transport import ChasterHttpTransport
        public = {n for n in dir(ChasterHttpTransport) if not n.startswith("_")}
        assert public == {"set_frozen", "set_extensions"}
