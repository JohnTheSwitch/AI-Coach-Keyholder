"""v0.39 Phase I3: the wearer's consent to community photo review, the consent gate on the forward write, the revert direction and the
`review-consent` / `extension-review --revert` / `extension-config` CLI. No network."""

from __future__ import annotations

import copy
import io
from datetime import timedelta

import pytest

from chaster.extension_config import community_exposed, parse_lock_configuration
from chaster.extension_plan import plan_review_revert
from chaster_execution.extension_review import ConsentOutcome as C
from chaster_execution.extension_review import ExtensionReviewOutcome as O
from chaster_execution.review_consent import (NOTICE_TEXT, NOTICE_VERSION, ConsentDecision, ConsentState, ReviewConsentLedger,
                                              consent_target)
from cli.main import main, render_configuration
from tests.chaster_execution.test_extension_review import (LOCK, NOW, OWNER, FakeReader, World, on_target, plan_for,  # noqa: F401
                                                           raw_lock)


def wearer(lock):
    """The same lock, worn by the connected account (the owner's own lock) with an edit grant for the wearer."""
    lock = copy.deepcopy(lock)
    lock["role"] = "wearer"
    lock["permissions"]["grants"][0]["subjects"]["wearer"] = ["edit"]
    return lock


def private(lock):
    """`on_target` but with visibility back to keyholder (what a revert produces)."""
    lock = copy.deepcopy(lock)
    for e in lock["extensions"]:
        if e["slug"] in ("tasks", "verification-picture"):
            e["config"]["peerVerification"]["visibility"] = "keyholder"
    return lock


# ---------------------------------------------------------------------------------------------------------------- the ledger

class TestLedger:
    def test_no_record_is_none(self, tmp_path) -> None:
        assert World(tmp_path, consent=False).ledger.state(LOCK).state is ConsentState.NONE

    def test_the_latest_record_decides_and_is_specific_to_the_lock(self, tmp_path) -> None:
        w = World(tmp_path, consent=False)
        w.ledger.record(lock_id=LOCK, decision=ConsentDecision.APPROVED, comment="a", now=NOW)
        assert w.ledger.state(LOCK).approved and w.ledger.state("otherlock").state is ConsentState.NONE
        w.ledger.record(lock_id=LOCK, decision=ConsentDecision.REVOKED, comment="b", now=NOW)        # same timestamp: insertion order
        assert w.ledger.state(LOCK).state is ConsentState.REVOKED
        w.ledger.record(lock_id=LOCK, decision=ConsentDecision.REJECTED, comment="c", now=NOW + timedelta(seconds=1))
        assert w.ledger.state(LOCK).state is ConsentState.DECLINED

    def test_the_record_holds_the_exact_notice_and_its_version(self, tmp_path) -> None:
        w = World(tmp_path, consent=False)
        w.ledger.record(lock_id=LOCK, decision=ConsentDecision.APPROVED, comment="x", now=NOW)
        with w.core.transaction() as tx:
            row = tx.fetch_one("SELECT * FROM consent_log WHERE target_id = ?", (consent_target(LOCK),))
        assert (row["target_type"], row["target_version"], row["explanation_shown"], row["action"]) == \
               ("integration", NOTICE_VERSION, NOTICE_TEXT, "approved")

    def test_consent_to_an_older_notice_is_stale(self, tmp_path) -> None:
        w = World(tmp_path, consent=False)
        with w.core.transaction() as tx:
            tx.execute("INSERT INTO consent_log (id, created_at, target_type, target_id, target_version, action) "
                       "VALUES ('x', '2026-11-01T00:00:00+00:00', 'integration', ?, 'oldversion00', 'approved')", (consent_target(LOCK),))
        state = w.ledger.state(LOCK)
        assert state.state is ConsentState.STALE and not state.approved

    def test_the_notice_says_what_matters(self) -> None:
        for phrase in ("Any logged-in Chaster member", "no longer adds time", "UNCONFIRMED incident", "revoke it at any time",
                       "this lock only"):
            assert phrase in NOTICE_TEXT
        assert len(NOTICE_VERSION) == 12


# ---------------------------------------------------------------------------------------------------------------- the revert plan

class TestRevertPlan:
    def test_only_visibility_changes_back_to_keyholder(self) -> None:
        raw = on_target(raw_lock())
        plan = plan_review_revert(parse_lock_configuration(raw))
        assert [(c.slug, c.field, c.before, c.after) for c in plan.changes] == [
            ("tasks", "peerVerification.visibility", "all_members", "keyholder"),
            ("verification-picture", "peerVerification.visibility", "all_members", "keyholder")]
        sent = {e["slug"]: e for e in plan.body["extensions"]}
        assert sent["tasks"]["config"]["peerVerification"] == {"enabled": True, "visibility": "keyholder", "punishments": [],
                                                               "futureField": "kept"}
        assert sent["tasks"]["config"]["tasks"][0]["task"] == "SECRET TASK TEXT" and plan.refusal is None

    def test_a_private_lock_has_nothing_to_revert(self) -> None:
        assert not plan_review_revert(parse_lock_configuration(private(on_target(raw_lock())))).has_changes

    def test_a_missing_peer_verification_object_is_not_created(self) -> None:
        raw = on_target(raw_lock())
        del raw["extensions"][2]["config"]["peerVerification"]
        plan = plan_review_revert(parse_lock_configuration(raw))
        assert "peerVerification" not in plan.body["extensions"][2]["config"] and [c.slug for c in plan.changes] == ["tasks"]

    def test_exposure(self) -> None:
        assert community_exposed(parse_lock_configuration(on_target(raw_lock())))
        assert not community_exposed(parse_lock_configuration(raw_lock()))
        assert not community_exposed(parse_lock_configuration(private(on_target(raw_lock()))))


# ---------------------------------------------------------------------------------------------------------------- the gate

class TestConsentGate:
    @pytest.mark.parametrize("decision", [None, ConsentDecision.REJECTED, ConsentDecision.REVOKED])
    def test_the_forward_write_needs_approved_consent(self, tmp_path, decision) -> None:
        w = World(tmp_path, consent=False)
        if decision:
            w.ledger.record(lock_id=LOCK, decision=decision, comment="t", now=NOW)
        result = w.apply()
        assert result.outcome is O.NO_CONSENT and result.refused and w.transport.calls == []
        assert [r["outcome"] for r in w.audit()] == ["no_consent"]

    def test_the_halt_still_comes_first(self, tmp_path) -> None:
        w = World(tmp_path, consent=False, halted=True)
        assert w.apply().outcome is O.HALTED and w.reader.calls == []

    def test_the_dry_run_shows_the_consent_state(self, tmp_path) -> None:
        result = World(tmp_path, consent=False).service.preview(actor=OWNER, lock_id=LOCK)
        assert result.consent.state is ConsentState.NONE and any("consent" in b for b in result.blockers)
        (tmp_path / "x").mkdir()
        assert World(tmp_path / "x").service.preview(actor=OWNER, lock_id=LOCK).blockers == ()


class TestRevert:
    def test_the_revert_needs_no_consent_nor_plan_id_and_is_verified(self, tmp_path) -> None:
        w = World(tmp_path, consent=False, reader=FakeReader(on_target(raw_lock()), private(on_target(raw_lock()))))
        result = w.service.apply(actor=OWNER, lock_id=LOCK, plan_id=None, confirmed=True, revert=True)
        assert result.outcome is O.APPLIED and result.revert and len(w.transport.calls) == 1
        body = w.transport.calls[0][1]
        assert {e["slug"]: e["config"].get("peerVerification", {}).get("visibility") for e in body["extensions"]} == \
               {"pillory": None, "tasks": "keyholder", "verification-picture": "keyholder"}
        row, = w.audit()
        assert row["outcome"] == "applied" and row["plan_id"] == plan_review_revert(parse_lock_configuration(on_target(raw_lock()))).plan_id

    def test_a_revert_that_does_not_take_is_unverified(self, tmp_path) -> None:
        w = World(tmp_path, reader=FakeReader(on_target(raw_lock()), on_target(raw_lock())))
        result = w.service.apply(actor=OWNER, lock_id=LOCK, plan_id=None, confirmed=True, revert=True)
        assert result.outcome is O.APPLIED_UNVERIFIED and result.remaining_gaps == ("community review is still on",)

    @pytest.mark.parametrize("kw, outcome", [({"confirmed": False}, O.NOT_CONFIRMED), ({"actor": "x"}, O.NOT_OWNER),
                                             ({"plan_id": "0" * 16}, O.PLAN_MISMATCH), ({"plan_id": "zz"}, O.INVALID_INPUT)])
    def test_the_revert_keeps_every_other_guard(self, tmp_path, kw, outcome) -> None:
        w = World(tmp_path, reader=FakeReader(on_target(raw_lock())))
        args = {"actor": OWNER, "lock_id": LOCK, "plan_id": None, "confirmed": True, "revert": True, **kw}
        assert w.service.apply(**args).outcome is outcome and w.transport.calls == []

    def test_the_halt_does_not_stop_a_revert_but_still_stops_the_forward_write(self, tmp_path) -> None:
        w = World(tmp_path, halted=True, reader=FakeReader(on_target(raw_lock()), private(on_target(raw_lock()))))
        assert w.service.apply(actor=OWNER, lock_id=LOCK, plan_id=None, confirmed=True, revert=True).outcome is O.APPLIED
        assert w.service.preview(actor=OWNER, lock_id=LOCK, revert=True).blockers == ()
        (tmp_path / "f").mkdir()
        assert World(tmp_path / "f", halted=True).apply().outcome is O.HALTED

    def test_nothing_to_revert(self, tmp_path) -> None:
        w = World(tmp_path, reader=FakeReader(raw_lock()))
        assert w.service.apply(actor=OWNER, lock_id=LOCK, plan_id=None, confirmed=True, revert=True).outcome is O.NOTHING_TO_REVERT
        assert w.service.preview(actor=OWNER, lock_id=LOCK, revert=True).outcome is O.NOTHING_TO_REVERT


# ---------------------------------------------------------------------------------------------------------------- owner decisions

class TestDecideConsent:
    def test_showing_changes_nothing(self, tmp_path) -> None:
        w = World(tmp_path, consent=False)
        result = w.service.decide_consent(actor=OWNER, lock_id=LOCK, decision=None, confirmed=False)
        assert result.outcome is C.SHOWN and result.consent.state is ConsentState.NONE and result.exposed is False
        assert w.ledger.state(LOCK).state is ConsentState.NONE and w.transport.calls == []

    def test_the_owner_records_consent_for_a_lock_the_owner_wears(self, tmp_path) -> None:
        w = World(tmp_path, consent=False, reader=FakeReader(wearer(raw_lock())))
        result = w.service.decide_consent(actor=OWNER, lock_id=LOCK, decision=ConsentDecision.APPROVED, confirmed=True)
        assert result.outcome is C.RECORDED and result.consent.approved and result.revert is None and w.transport.calls == []

    def test_a_lock_worn_by_another_account_is_refused(self, tmp_path) -> None:
        w = World(tmp_path, consent=False)                                         # role keyholder: someone else wears it
        result = w.service.decide_consent(actor=OWNER, lock_id=LOCK, decision=ConsentDecision.APPROVED, confirmed=True)
        assert result.outcome is C.WEARER_IS_ANOTHER_ACCOUNT and w.ledger.state(LOCK).state is ConsentState.NONE

    @pytest.mark.parametrize("decision", [ConsentDecision.REJECTED, ConsentDecision.REVOKED])
    def test_declining_or_revoking_reverts_an_exposed_lock_at_once(self, tmp_path, decision) -> None:
        exposed = wearer(on_target(raw_lock()))
        w = World(tmp_path, reader=FakeReader(exposed, exposed, private(exposed)))
        result = w.service.decide_consent(actor=OWNER, lock_id=LOCK, decision=decision, confirmed=True)
        assert result.outcome is C.RECORDED and result.consent.state is not ConsentState.APPROVED
        assert result.revert.outcome is O.APPLIED and len(w.transport.calls) == 1

    def test_a_revocation_reverts_even_while_halted(self, tmp_path) -> None:
        exposed = wearer(on_target(raw_lock()))
        w = World(tmp_path, halted=True, reader=FakeReader(exposed, exposed, private(exposed)))
        result = w.service.decide_consent(actor=OWNER, lock_id=LOCK, decision=ConsentDecision.REVOKED, confirmed=True)
        assert result.consent.state is ConsentState.REVOKED and result.revert.outcome is O.APPLIED and len(w.transport.calls) == 1

    def test_the_owner_may_attest_that_another_wearer_account_is_the_owners_own(self, tmp_path) -> None:
        w = World(tmp_path, consent=False)                                         # role keyholder
        result = w.service.decide_consent(actor=OWNER, lock_id=LOCK, decision=ConsentDecision.APPROVED, confirmed=True,
                                          attest_own_account=True)
        assert result.outcome is C.RECORDED and result.consent.approved
        with w.core.transaction() as tx:
            row = tx.fetch_one("SELECT user_comment FROM consent_log WHERE target_id = ?", (consent_target(LOCK),))
        assert row["user_comment"] == "owner cli: approved; the owner attests the wearer account is the owner's own"

    def test_a_private_lock_is_not_written_on_decline(self, tmp_path) -> None:
        w = World(tmp_path, reader=FakeReader(wearer(raw_lock())))
        result = w.service.decide_consent(actor=OWNER, lock_id=LOCK, decision=ConsentDecision.REJECTED, confirmed=True)
        assert result.revert is None and w.transport.calls == [] and w.audit() == []

    @pytest.mark.parametrize("kw, outcome", [({"actor": "x"}, C.NOT_OWNER), ({"lock_id": "../x"}, C.INVALID_INPUT),
                                             ({"confirmed": False}, C.NOT_CONFIRMED)])
    def test_guards(self, tmp_path, kw, outcome) -> None:
        w = World(tmp_path, consent=False, reader=FakeReader(wearer(raw_lock())))
        args = {"actor": OWNER, "lock_id": LOCK, "decision": ConsentDecision.APPROVED, "confirmed": True, **kw}
        assert w.service.decide_consent(**args).outcome is outcome and w.ledger.state(LOCK).state is ConsentState.NONE

    def test_a_read_failure_records_nothing(self, tmp_path) -> None:
        w = World(tmp_path, consent=False, reader=FakeReader(problem="api_error:500"))
        result = w.service.decide_consent(actor=OWNER, lock_id=LOCK, decision=ConsentDecision.APPROVED, confirmed=True)
        assert result.outcome is C.READ_FAILED and w.ledger.state(LOCK).state is ConsentState.NONE


# ---------------------------------------------------------------------------------------------------------------- the CLI

class TestCli:
    def _run(self, world, *args):
        out = io.StringIO()
        code = main(list(args), out=out, review_factory=lambda: world.service, now=lambda: NOW)
        return code, out.getvalue()

    def test_review_consent_shows_the_exact_notice(self, tmp_path) -> None:
        code, text = self._run(World(tmp_path, consent=False), "review-consent", LOCK, "--owner-id", OWNER)
        assert code == 0 and NOTICE_TEXT in text and f"notice {NOTICE_VERSION}" in text and "Consent: none" in text
        assert "--accept|--decline|--revoke --yes" in text

    def test_review_consent_accept(self, tmp_path) -> None:
        w = World(tmp_path, consent=False, reader=FakeReader(wearer(raw_lock())))
        code, text = self._run(w, "review-consent", LOCK, "--owner-id", OWNER, "--accept", "--yes")
        assert code == 0 and text.startswith("Recorded.") and "Consent: approved" in text

    def test_review_consent_refusals_exit_3(self, tmp_path) -> None:
        code, text = self._run(World(tmp_path, consent=False), "review-consent", LOCK, "--owner-id", OWNER, "--accept", "--yes")
        assert code == 3 and "only that wearer can give or refuse consent" in text
        with pytest.raises(SystemExit):                                              # one decision at a time
            main(["review-consent", LOCK, "--owner-id", OWNER, "--accept", "--revoke"], out=io.StringIO(), review_factory=lambda: None)

    def test_a_revoke_shows_the_revert(self, tmp_path) -> None:
        exposed = wearer(on_target(raw_lock()))
        w = World(tmp_path, reader=FakeReader(exposed, exposed, private(exposed)))
        code, text = self._run(w, "review-consent", LOCK, "--owner-id", OWNER, "--revoke", "--yes")
        assert code == 0 and "Automatic revert" in text and "Direction: REVERT" in text and "outcome=applied" in text

    def test_a_revoke_that_does_not_take_exits_4(self, tmp_path) -> None:
        exposed = wearer(on_target(raw_lock()))
        w = World(tmp_path, halted=True, reader=FakeReader(exposed))
        code, text = self._run(w, "review-consent", LOCK, "--owner-id", OWNER, "--revoke", "--yes")
        assert code == 4 and "Consent: revoked" in text and "outcome=applied_unverified" in text

    def test_the_attestation_flag(self, tmp_path) -> None:
        w = World(tmp_path, consent=False)
        code, text = self._run(w, "review-consent", LOCK, "--owner-id", OWNER, "--accept", "--yes", "--wearer-is-my-account")
        assert code == 0 and "Consent: approved" in text

    def test_extension_review_revert_dry_run_and_apply(self, tmp_path) -> None:
        w = World(tmp_path, consent=False, reader=FakeReader(on_target(raw_lock()), on_target(raw_lock()), private(on_target(raw_lock()))))
        code, text = self._run(w, "extension-review", LOCK, "--owner-id", OWNER, "--revert")
        assert code == 0 and "Direction: REVERT" in text and "all_members  ->  keyholder" in text and "--revert --apply --plan" in text
        assert w.transport.calls == []
        code, text = self._run(w, "extension-review", LOCK, "--owner-id", OWNER, "--revert", "--apply", "--yes")
        assert code == 0 and "outcome=applied" in text and len(w.transport.calls) == 1

    def test_the_forward_apply_without_consent_is_refused(self, tmp_path) -> None:
        w = World(tmp_path, consent=False)
        code, text = self._run(w, "extension-review", LOCK, "--owner-id", OWNER, "--apply", "--plan", plan_for().plan_id, "--yes")
        assert code == 3 and "has not approved community review" in text and w.transport.calls == []

    def test_every_consent_outcome_has_a_description(self) -> None:
        from cli.main import _CONSENT_TEXT
        assert set(_CONSENT_TEXT) == {o.value for o in C}

    def test_extension_config_flags_exposure_without_consent(self, tmp_path) -> None:
        from chaster.extension_config import ConfigurationReport
        from chaster.lock_directory import LockListing
        w = World(tmp_path, consent=False)
        report = ConfigurationReport()
        report.configurations.append(parse_lock_configuration(on_target(raw_lock())))
        text = render_configuration(LockListing(), report, w.service.consent_states([LOCK]))
        assert "wearer consent to community review: none" in text and "WARNING: community review is ON without" in text
        w.ledger.record(lock_id=LOCK, decision=ConsentDecision.APPROVED, comment="t", now=NOW)
        text = render_configuration(LockListing(), report, w.service.consent_states([LOCK]))
        assert "consent to community review: approved" in text and "WARNING" not in text


class TestIsolation:
    def test_the_bot_never_records_review_consent(self) -> None:
        from pathlib import Path
        root = Path(__file__).parent.parent.parent
        for name in ("system/composition.py", "system/tenant_factory.py", "system/tenant_gateway.py", "bot/discord_bot.py"):
            text = (root / name).read_text(encoding="utf-8")
            assert "review_consent" not in text and "decide_consent" not in text
