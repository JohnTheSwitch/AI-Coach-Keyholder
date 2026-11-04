"""The exemption workflow end to end: DM entry point -> buttons -> keyholder -> Penalty Window pause -> unfreeze through the real barrier -> re-freeze. No network."""

from __future__ import annotations

import ast
from datetime import timedelta
from pathlib import Path

import pytest

from exemptions.service import CATEGORIES, DURATIONS, SAFETY_LINE, ExemptionDecision, ExemptionService, is_exemption_request
from penalty_engine.models import FreezeReason
from rules_engine.penalty_hold import PenaltyHold
from system.dm_actions import ExemptionDm, discord_user_resolver
from system.penalty_notices import penalty_notice_handlers
from system.penalty_policy import make_penalty_engine
from system.periodic import run_periodic_cycle
from infrastructure.clock import FrozenClock
from infrastructure.time_format import iso
from tests.system.test_penalty_pipeline import LOCK, NOW, World as PipelineWorld

ROOT = Path(__file__).parent.parent.parent
OWNER = "owner-1"
WEARER = "wearer-1"
OWNER_DISCORD, WEARER_DISCORD = "100", "200"


class World(PipelineWorld):
    def __init__(self, tmp_path, **kwargs) -> None:
        super().__init__(tmp_path, **kwargs)
        with self.core.raw_connection() as conn:
            for user, discord_id in ((OWNER, OWNER_DISCORD), (WEARER, WEARER_DISCORD)):
                conn.execute("INSERT INTO user_accounts (id, created_at, last_seen_at) VALUES (?, ?, ?)", (user, iso(NOW), iso(NOW)))
                conn.execute("INSERT INTO user_channel_identities (id, user_account_id, channel, external_id, created_at) VALUES (?, ?, 'discord', ?, ?)",
                             (f"i{user}", user, discord_id, iso(NOW)))
            conn.commit()
        self.service = ExemptionService(core=self.core, owner_user_id=OWNER, penalties=make_penalty_engine(self.core))
        self.at = NOW
        self.dm = ExemptionDm(self.service, resolve_user=discord_user_resolver(self.core), now=lambda: self.at)

    def run(self, at):
        self.at = at
        self.n += 1
        assert run_periodic_cycle(self.core, f"e{self.n}", FrozenClock(at), rules_engine=None, executor=None, penalty_barrier=self.barrier, exemptions=self.service,
                                  penalty_notices=penalty_notice_handlers(WEARER))

    def request(self, category="family", hours=24, at=None):
        at = at or self.at
        card = self.service.start(WEARER, at)
        ident = card.actions[0].action_id.split(":")[2]
        self.at = at
        self.dm.press(discord_user_id=WEARER_DISCORD, action=f"cat_{category}", ident=ident)
        self.dm.press(discord_user_id=WEARER_DISCORD, action=f"h{hours}", ident=ident)
        return ident

    def approve(self, ident, at=None):
        self.at = at or self.at
        return self.dm.press(discord_user_id=OWNER_DISCORD, action="approve", ident=ident)

    def notices(self, kind):
        return self.q("SELECT * FROM notifications WHERE kind = ? ORDER BY created_at, rowid", (kind,))

    def exemption(self):
        return self.q("SELECT * FROM wear_exemptions")[0]


class TestEntryPoint:
    @pytest.mark.parametrize("text", ["exemption", "I need an exemption", "Can I get an exemption for the wedding?", "potřebuji výjimku", "chci vyjimku"])
    def test_the_word_opens_it(self, text) -> None:
        assert is_exemption_request(text)

    @pytest.mark.parametrize("text", ["hello", "I am exempt from nothing", "", "how is my day", None])
    def test_nothing_else_does(self, text) -> None:
        assert not is_exemption_request(text)

    def test_the_first_card_offers_each_reason_and_always_says_safety_needs_no_permission(self, tmp_path) -> None:
        w = World(tmp_path)
        card = w.service.start(WEARER, NOW)
        assert [a.label for a in card.actions] == list(CATEGORIES.values()) and SAFETY_LINE in card.text

    def test_the_application_answers_before_any_model(self, tmp_path) -> None:
        from tests.application.test_lock_and_task_commands import _complete_onboarding, _incoming
        from application.service import ApplicationService
        w = World(tmp_path)
        seen = []

        class Listener:
            def __call__(self, user_id, text, now):
                seen.append(text)
                return w.service.start(user_id, now) if is_exemption_request(text) else None

        service = ApplicationService(w.core.db_path, core=w.core, exemption_reply=Listener())
        _complete_onboarding(service)
        reply = service.handle_message(_incoming("I need an exemption", external_message_id="m9"))
        assert reply.actions and reply.actions[0].action_id.startswith("exemption:cat_")

    def test_the_chaster_channel_cannot_reach_it(self) -> None:
        """An untrusted channel is conversation only."""
        source = (ROOT / "application" / "service.py").read_text(encoding="utf-8")
        body = source[source.index("def handle_conversation_only"):source.index("def _notify_check_in")]
        assert "_exemption_reply" not in body

    def test_a_request_needs_a_reason_first(self, tmp_path) -> None:
        w = World(tmp_path)
        ident = w.service.start(WEARER, NOW).actions[0].action_id.split(":")[2]
        assert "no longer open" in w.service.choose_hours(WEARER, ident, 24, NOW).text
        assert w.notices("exemption_request") == []

    @pytest.mark.parametrize("bad", ["cat_weapons", "h5", "h99999", "nonsense"])
    def test_only_the_offered_options_exist(self, tmp_path, bad) -> None:
        w = World(tmp_path)
        ident = w.service.start(WEARER, NOW).actions[0].action_id.split(":")[2]
        w.dm.press(discord_user_id=WEARER_DISCORD, action="cat_family", ident=ident)
        assert "isn't one of the options" in w.dm.press(discord_user_id=WEARER_DISCORD, action=bad, ident=ident).text

    def test_a_stranger_cannot_use_someone_elses_card(self, tmp_path) -> None:
        w = World(tmp_path)
        ident = w.service.start(WEARER, NOW).actions[0].action_id.split(":")[2]
        assert "no longer open" in w.dm.press(discord_user_id=OWNER_DISCORD, action="cat_family", ident=ident).text
        assert w.dm.press(discord_user_id="999", action="cat_family", ident=ident).text == "This isn't yours to answer."

    def test_an_abandoned_draft_blocks_nothing(self, tmp_path) -> None:
        w = World(tmp_path)
        w.service.start(WEARER, NOW)
        w.service.start(WEARER, NOW)
        assert len(w.q("SELECT * FROM wear_exemptions")) == 1

    def test_a_second_request_points_at_the_first(self, tmp_path) -> None:
        w = World(tmp_path)
        w.request()
        assert "with your keyholder" in w.service.start(WEARER, NOW).text
        assert len(w.q("SELECT * FROM wear_exemptions")) == 1


class TestDecision:
    def test_the_keyholder_gets_a_card_at_once_even_at_night(self, tmp_path) -> None:
        w = World(tmp_path)
        w.request("work_school", 72)
        card = w.notices("exemption_request")[0]
        assert card["user_id"] == OWNER and card["immediate"] == 1 and "Work or school" in card["text"] and "3 days" in card["text"]

    def test_only_the_keyholder_decides(self, tmp_path) -> None:
        w = World(tmp_path)
        ident = w.request()
        assert w.dm.press(discord_user_id=WEARER_DISCORD, action="approve", ident=ident).text == "Only the keyholder can decide this."
        assert w.exemption()["status"] == "requested"

    def test_no_owner_configured_means_no_decision(self, tmp_path) -> None:
        w = World(tmp_path)
        ident = w.request()
        service = ExemptionService(core=w.core, owner_user_id=None, penalties=make_penalty_engine(w.core))
        assert service.decide(actor=OWNER, exemption_id=ident, approve=True, now=NOW) is ExemptionDecision.NOT_OWNER

    def test_a_decision_counts_once(self, tmp_path) -> None:
        w = World(tmp_path)
        ident = w.request()
        w.approve(ident)
        assert w.dm.press(discord_user_id=OWNER_DISCORD, action="decline", ident=ident).text == "This request was already decided."

    def test_a_decline_tells_the_wearer_and_touches_nothing(self, tmp_path) -> None:
        w = World(tmp_path)
        window = w.open_window()
        ident = w.request(at=NOW + timedelta(hours=1))
        w.dm.press(discord_user_id=OWNER_DISCORD, action="decline", ident=ident)
        w.run(NOW + timedelta(hours=1))
        assert w.window().id == window.id and w.window().status.value == "active"
        assert SAFETY_LINE in w.notices("exemption_decision")[0]["text"] and w.transport.calls == [(LOCK, True)]

    def test_the_card_says_nothing_about_a_penalty_window(self, tmp_path) -> None:
        """I7: the decision is about the request only, so the keyholder is not shown the window either."""
        w = World(tmp_path)
        w.open_window()
        w.request(at=NOW + timedelta(hours=1))
        text = w.notices("exemption_request")[0]["text"].lower()
        assert "window" not in text and "penalty" not in text.replace("nothing else", "")

    def test_the_decision_never_reads_the_penalty_window(self, tmp_path) -> None:
        """I7 by construction: with a Penalty Engine that fails on any touch, a decline still works and an approval is still recorded."""
        w = World(tmp_path)

        class Untouchable:
            def __getattr__(self, name):
                raise AssertionError(f"the decision touched the Penalty Engine: {name}")

        service = ExemptionService(core=w.core, owner_user_id=OWNER, penalties=Untouchable())
        ident = w.request()
        assert service.decide(actor=OWNER, exemption_id=ident, approve=False, now=NOW) is ExemptionDecision.DECLINED
        source = (ROOT / "exemptions" / "service.py").read_text(encoding="utf-8")
        body = source[source.index("    def decide("):source.index("    def tick(")]
        assert "_penalties" not in body and "penalty_windows" not in body

    def test_an_approval_inside_a_window_equals_one_outside(self, tmp_path) -> None:
        """3.6: 'if the system would approve the request outside a Penalty Window, it must approve it during one as well.'"""
        outside, inside = World(tmp_path / "a"), None
        (tmp_path / "b").mkdir()
        inside = World(tmp_path / "b")
        inside.open_window()
        results = []
        for w in (outside, inside):
            ident = w.request(at=NOW + timedelta(hours=1))
            results.append(w.service.decide(actor=OWNER, exemption_id=ident, approve=True, now=NOW + timedelta(hours=1)))
        assert results == [ExemptionDecision.APPROVED, ExemptionDecision.APPROVED]

    def test_the_model_has_no_way_in(self) -> None:
        """A person decides: nothing under conversation_engine or coach can reach exemptions."""
        for package in ("conversation_engine", "coach"):
            for path in (ROOT / package).rglob("*.py"):
                for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                    if isinstance(node, ast.ImportFrom):
                        assert not (node.module or "").startswith(("exemptions", "infractions", "system.dm_actions")), path


class TestEndToEnd:
    def test_submission_pauses_the_window_and_unfreezes_the_lock(self, tmp_path) -> None:
        w = World(tmp_path)
        window = w.open_window()
        assert w.transport.calls == [(LOCK, True)]
        w.lock["isFrozen"] = True
        at = NOW + timedelta(hours=1)
        ident = w.request("family", 24, at=at)
        assert w.approve(ident).text.startswith("Approved")
        assert w.window().status.value == "frozen"                                    # paused at once
        w.run(at)
        assert w.transport.calls[-1] == (LOCK, False)
        assert w.intents()[-1] == ("unfreeze", "exemption_started", "executed")
        assert PenaltyHold(w.core).is_active()                                        # redemption stays blocked: only the timer pauses
        assert window.id == w.window().id

    def test_no_lock_time_ever_changes(self, tmp_path) -> None:
        """Zero-Time-Modification: the only transport call of the whole flow is the frozen flag."""
        w = World(tmp_path)
        w.open_window()
        w.lock["isFrozen"] = True
        at = NOW + timedelta(hours=1)
        w.approve(w.request(at=at))
        w.run(at)
        assert all(isinstance(frozen, bool) and lock == LOCK for lock, frozen in w.transport.calls)
        names = {n.attr for n in ast.walk(ast.parse((ROOT / "exemptions" / "service.py").read_text(encoding="utf-8"))) if isinstance(n, ast.Attribute)}
        assert not names & {"unlock", "set_frozen", "set_extensions", "add_time", "remove_time"}

    def test_the_paused_time_does_not_count_and_the_window_continues(self, tmp_path) -> None:
        w = World(tmp_path)
        w.open_window()
        w.lock["isFrozen"] = True
        at = NOW + timedelta(hours=1)
        w.approve(w.request(hours=6, at=at))
        w.run(at)
        before = w.window().accumulated_active_hours
        later = at + timedelta(hours=6)
        w.lock["isFrozen"] = False
        w.run(later)
        window = w.window()
        assert window.status.value == "active" and window.accumulated_active_hours == before and window.active_period_started_at == later

    def test_expiry_ends_it_and_raises_the_barrier_again(self, tmp_path) -> None:
        w = World(tmp_path)
        w.open_window()
        w.lock["isFrozen"] = True
        at = NOW + timedelta(hours=1)
        w.approve(w.request(hours=6, at=at))
        w.run(at)
        w.lock["isFrozen"] = False
        w.run(at + timedelta(hours=6))
        assert w.exemption()["status"] == "ended" and w.exemption()["end_reason"] == "expired"
        assert w.transport.calls[-1] == (LOCK, True) and w.intents()[-1] == ("freeze", "exemption_ended", "executed")
        assert len(w.notices("exemption_ended")) == 1

    def test_im_back_ends_it_early(self, tmp_path) -> None:
        w = World(tmp_path)
        w.open_window()
        w.lock["isFrozen"] = True
        at = NOW + timedelta(hours=1)
        ident = w.request(at=at)
        w.approve(ident)
        w.run(at)
        w.lock["isFrozen"] = False
        back = at + timedelta(hours=2)
        w.at = back
        assert "Welcome back" in w.dm.press(discord_user_id=WEARER_DISCORD, action="end", ident=ident).text
        w.run(back)
        assert w.exemption()["end_reason"] == "wearer_returned" and w.window().status.value == "active" and w.transport.calls[-1] == (LOCK, True)

    def test_a_second_press_of_im_back_is_harmless(self, tmp_path) -> None:
        w = World(tmp_path)
        ident = w.request()
        w.approve(ident)
        w.dm.press(discord_user_id=WEARER_DISCORD, action="end", ident=ident)
        assert "already over" in w.dm.press(discord_user_id=WEARER_DISCORD, action="end", ident=ident).text

    def test_outside_a_window_it_is_recorded_and_touches_nothing(self, tmp_path) -> None:
        w = World(tmp_path)
        w.approve(w.request())
        w.run(NOW)
        assert w.exemption()["status"] == "approved" and w.q("SELECT * FROM freeze_periods") == [] and w.transport.calls == []

    def test_a_window_that_starts_during_an_exemption_is_paused_too(self, tmp_path) -> None:
        w = World(tmp_path)
        w.approve(w.request(hours=72))
        w.open_window(NOW + timedelta(hours=1))
        w.run(NOW + timedelta(hours=2))
        assert w.window().status.value == "frozen"
        assert len(w.q("SELECT * FROM freeze_periods WHERE reason = 'temporary_wear_exemption'")) == 1

    def test_a_halt_keeps_the_intent_queued(self, tmp_path) -> None:
        """The runtime halt still rules: nothing reaches Chaster, and the unfreeze waits."""
        w = World(tmp_path)
        w.open_window()
        calls = len(w.transport.calls)
        w.lock["isFrozen"] = True
        w.control.set_halted(True, reason="test", now=NOW)
        at = NOW + timedelta(hours=1)
        w.approve(w.request(at=at))
        w.run(at)
        assert len(w.transport.calls) == calls and w.intents()[-1][2] == "pending" and w.window().status.value == "frozen"

    def test_a_barrier_that_is_off_never_calls_chaster(self, tmp_path) -> None:
        w = World(tmp_path, enabled=False)
        w.open_window()
        at = NOW + timedelta(hours=1)
        w.approve(w.request(at=at))
        w.run(at)
        assert w.transport.calls == []

    def test_a_tick_is_idempotent(self, tmp_path) -> None:
        w = World(tmp_path)
        w.open_window()
        at = NOW + timedelta(hours=1)
        w.approve(w.request(at=at))
        for minute in range(3):
            w.service.tick(at + timedelta(minutes=minute))
        assert len(w.q("SELECT * FROM freeze_periods")) == 1

    def test_a_failing_penalty_engine_is_retried(self, tmp_path) -> None:
        w = World(tmp_path)
        window = w.open_window()
        real = w.service._penalties

        class Failing:
            def get_active_or_frozen_penalty_window(self):
                raise RuntimeError("database is locked")

        w.service._penalties = Failing()
        at = NOW + timedelta(hours=1)
        assert w.approve(w.request(at=at)).text.startswith("Approved")
        assert w.window().status.value == "active"
        w.service._penalties = real
        w.service.tick(at + timedelta(minutes=1))
        assert w.window().id == window.id and w.window().status.value == "frozen"

    def test_an_emergency_override_is_not_disturbed(self, tmp_path) -> None:
        """Ending an exemption closes only its own reason: an open override keeps the window paused."""
        w = World(tmp_path)
        window = w.open_window()
        at = NOW + timedelta(hours=1)
        make_penalty_engine(w.core).emergency_freeze(window.id, now=at)
        w.approve(w.request(hours=6, at=at))
        w.service.tick(at + timedelta(hours=7))
        assert w.window().status.value == "frozen"
        assert len(w.q("SELECT * FROM freeze_periods WHERE reason = ? AND ended_at IS NULL", (FreezeReason.EMERGENCY_OVERRIDE.value,))) == 1


class TestPenaltyNotices:
    def test_the_wearer_hears_about_a_warning_and_a_window_once(self, tmp_path) -> None:
        w = World(tmp_path)
        w.confirm_rejection()
        w.run(NOW)
        w.run(NOW + timedelta(seconds=30))
        assert len(w.notices("penalty_warning")) == 1
        w.confirm_rejection(NOW + timedelta(minutes=1))
        w.run(NOW + timedelta(minutes=1))
        started = w.notices("penalty_window")
        assert len(started) == 1 and started[0]["user_id"] == WEARER and "exemption" in started[0]["text"]

    def test_the_end_of_a_window_is_announced(self, tmp_path) -> None:
        from tests.system.test_penalty_pipeline import AFTER_ANY_WINDOW
        w = World(tmp_path)
        w.confirm_rejection()
        w.run(NOW)
        w.confirm_rejection(NOW + timedelta(minutes=1))
        w.run(NOW + timedelta(minutes=1))
        w.run(AFTER_ANY_WINDOW)
        assert any("has ended" in r["text"] for r in w.notices("penalty_window"))

    def test_no_notice_carries_trust(self) -> None:
        from system import penalty_notices
        for text in (penalty_notices.WARNING_TEXT, penalty_notices.WINDOW_STARTED_TEXT, penalty_notices.WINDOW_RESUMED_TEXT, penalty_notices.WINDOW_ENDED_TEXT):
            assert "trust" not in text.lower() and "score" not in text.lower()


class TestCheckInsNeverReachAWindow:
    """The owner's safeguard 3: a check-in failure is Trust and a Warning. It can never activate a Penalty Window or a freeze, whoever confirms it."""

    def _confirm_escalation(self, w, at):
        from supervision.escalation import RULE_GROUP_ID
        from tests.system.test_penalty_pipeline import DOMAIN
        from peer_review.wiring import PEER_REVIEW_EVIDENCE
        from trust_manager.models import ConfirmationSource, IncidentConfirmation
        incident = w.trust.register_incident_report(rule_group_id=RULE_GROUP_ID, trust_domain=DOMAIN, description="missed", evidence=PEER_REVIEW_EVIDENCE, now=at)
        w.trust.confirm_incident(incident.id, new_confirmation=IncidentConfirmation.CONFIRMED, source=ConfirmationSource.KEYHOLDER_REVIEW, evidence_description="k", now=at)

    def test_the_group_is_pinned(self) -> None:
        from penalty_engine.repository import WARNING_ONLY_RULE_GROUPS
        from supervision.escalation import RULE_GROUP_ID
        assert RULE_GROUP_ID in WARNING_ONLY_RULE_GROUPS

    def test_a_confirmed_escalation_stays_a_warning_however_often(self, tmp_path) -> None:
        w = World(tmp_path)
        for i in range(5):
            at = NOW + timedelta(days=i)
            self._confirm_escalation(w, at)
            w.run(at)
        assert w.window() is None and w.q("SELECT * FROM penalty_windows") == [] and len(w.q("SELECT * FROM incident_warnings")) == 5
        assert w.transport.calls == [] and w.intents() == []

    def test_it_cannot_extend_a_running_window_either(self, tmp_path) -> None:
        w = World(tmp_path)
        window = w.open_window()
        before = window.extensions_hours
        at = NOW + timedelta(hours=1)
        self._confirm_escalation(w, at)
        w.run(at)
        assert w.window().extensions_hours == before and len(w.q("SELECT * FROM incident_warnings")) == 2


def test_durations_are_bounded() -> None:
    assert max(DURATIONS) == 168 and min(DURATIONS) > 0
