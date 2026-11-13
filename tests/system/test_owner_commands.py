"""Owner, 2026-11-13: the one-time test release (spends a token despite supervision, every other gate intact) and the reset of the long-term
Trust score for a new lock. Built for the owner, never run by the system on its own. A real database; a fake Chaster transport; no network."""

from __future__ import annotations

import io
from datetime import timedelta
from types import SimpleNamespace

import pytest

from cli.main import main, run_release_test_override
from release_engine.service import OWNER_TEST_VALIDITY
from tests.system.test_release_tick import MON, U, World, local
from trust_manager.domain_admin import TrustDomainAdmin
from trust_manager.repository import DEFAULT_NEW_DOMAIN_SCORE, TrustManager


def supervised(tmp_path, **kw):
    world = World(tmp_path, supervised=True, frozen_since=timedelta(hours=2), **kw)
    world.at(local(MON, 18, 0))
    world.tokens()
    return world


def ledger(world):
    with world.core.transaction() as tx:
        return [dict(r) for r in tx.fetch_all("SELECT delta, reason_code FROM token_ledger ORDER BY rowid")]


def requests(world):
    with world.core.transaction() as tx:
        return [dict(r) for r in tx.fetch_all("SELECT status, outcome FROM release_owner_requests ORDER BY rowid")]


class TestTheTestRelease:
    def test_without_a_request_supervision_holds_it(self, tmp_path) -> None:
        world = supervised(tmp_path)
        report = world.cycle()
        assert "release_held:supervision_active" in report.notes and world.transport.calls == []

    def test_a_request_spends_one_token_and_releases_despite_supervision(self, tmp_path) -> None:
        world = supervised(tmp_path)
        assert world.engine.request_owner_test(user_id=U, now=world.now)
        report = world.cycle()
        assert "owner_test:authorized" in report.notes and world.transport.calls == [("unfreeze", "lock-1")]
        assert world.engine.get_balance(U) == 0 and ledger(world)[-1] == {"delta": -1, "reason_code": "owner_test_release"}
        assert requests(world) == [{"status": "done", "outcome": "authorized"}] and world.auth_states() == ["provider_confirmed"]

    def test_only_once(self, tmp_path) -> None:
        world = supervised(tmp_path)
        world.tokens(2)
        world.engine.request_owner_test(user_id=U, now=world.now)
        world.cycle()
        world.cycle(local(MON, 18, 30))
        assert world.transport.calls == [("unfreeze", "lock-1")] and world.engine.get_balance(U) == 2

    def test_one_pending_request_at_a_time(self, tmp_path) -> None:
        world = supervised(tmp_path)
        assert world.engine.request_owner_test(user_id=U, now=world.now)
        assert world.engine.request_owner_test(user_id=U, now=world.now) is None

    def test_outside_free_time_it_waits_and_then_expires_unused(self, tmp_path) -> None:
        world = supervised(tmp_path)
        world.at(local(MON, 10, 0))                                 # work hours
        world.engine.request_owner_test(user_id=U, now=world.now)
        assert "owner_test:window_closed" in world.cycle().notes and world.transport.calls == []
        world.at(local(MON, 10, 0) + OWNER_TEST_VALIDITY + timedelta(minutes=1))
        world.cycle()
        assert requests(world)[0]["status"] == "expired" and world.engine.get_balance(U) == 1

    def test_never_without_a_confirmed_barrier(self, tmp_path) -> None:
        world = supervised(tmp_path)
        world.frozen = False
        world.engine.request_owner_test(user_id=U, now=world.now)
        assert "owner_test:gate_not_met" in world.cycle().notes and world.transport.calls == [] and world.engine.get_balance(U) == 1

    def test_never_without_a_token(self, tmp_path) -> None:
        world = World(tmp_path, supervised=True, frozen_since=timedelta(hours=2))
        world.at(local(MON, 18, 0))
        world.engine.request_owner_test(user_id=U, now=world.now)
        world.cycle()
        assert world.transport.calls == [] and requests(world) == [{"status": "refused", "outcome": "insufficient_tokens"}]

    def test_never_while_halted(self, tmp_path) -> None:
        world = supervised(tmp_path)
        world.control.set_halted(True, reason="test", now=world.now)
        world.engine.request_owner_test(user_id=U, now=world.now)
        world.cycle()
        assert world.transport.calls == [] and requests(world)[0]["status"] == "pending"

    def test_the_barrier_comes_back_after_the_session(self, tmp_path) -> None:
        world = supervised(tmp_path)
        world.engine.request_owner_test(user_id=U, now=world.now)
        world.cycle()
        world.frozen = False                                         # Chaster really lowered it
        report = world.cycle(local(MON, 22, 30))                     # the free-time session is over
        assert any(n.startswith("barrier_refreeze") for n in report.notes)


class TestTheCommand:
    def run(self, world, *args, owner="owner"):
        out = io.StringIO()
        code = run_release_test_override(SimpleNamespace(owner_id=args[0] if args else owner, yes="--yes" in args), out=out,
                                         now=lambda: world.now, factory=lambda: (world.engine, owner, U))
        return code, out.getvalue()

    def test_owner_only_and_yes(self, tmp_path) -> None:
        world = supervised(tmp_path)
        assert self.run(world, "stranger", "--yes")[0] == 3
        code, text = self.run(world, "owner")
        assert code == 3 and "add --yes" in text and requests(world) == []
        code, text = self.run(world, "owner", "--yes")
        assert code == 0 and "never changes lock time" in text and requests(world)[0]["status"] == "pending"

    def test_it_only_files_a_request(self, tmp_path) -> None:
        world = supervised(tmp_path)
        self.run(world, "owner", "--yes")
        assert world.transport.calls == [] and world.engine.get_balance(U) == 1

    def test_it_is_in_the_parser(self) -> None:
        out = io.StringIO()
        with pytest.raises(SystemExit):
            main(["release-test-override", "--help"], out=out)


class TestTheTrustReset:
    @pytest.fixture
    def admin(self, tmp_path):
        from tests.supervision.test_supervision import NOW
        world = World(tmp_path)
        tm = TrustManager(world.core.db_path, core=world.core)
        tm.create_domain(domain_id="routine", display_name="R", description="d", created_via_consent_id="c", now=NOW)
        with world.core.transaction() as tx:
            tx.execute("UPDATE trust_domain_state SET score = 0.0, confidence = 0.99 WHERE domain_id = 'routine'")
            tx.execute("INSERT INTO trust_evidence (id, domain_id, created_at, evidence_type, source_entity_type, source_entity_id, raw_weight, "
                       "evidence_confidence, explanation) VALUES ('e1', 'routine', ?, 'checkin_missed', 't', 's', -0.06, 1.0, 'e')", ("2026-01-01T00:00:00Z",))
        return SimpleNamespace(world=world, tm=tm, admin=TrustDomainAdmin(world.core.db_path, core=world.core, owner_user_id="owner"), now=NOW)

    def test_owner_only_and_yes(self, admin) -> None:
        assert admin.admin.reset(actor="x", domain_id="routine", confirmed=True, now=admin.now)[0].value == "not_owner"
        assert admin.admin.reset(actor="owner", domain_id="routine", confirmed=False, now=admin.now)[0].value == "not_confirmed"
        assert admin.admin.reset(actor="owner", domain_id="nope", confirmed=True, now=admin.now)[0].value == "not_found"
        assert admin.tm.get_domain_state("routine").score == 0.0

    def test_long_term_only(self, admin) -> None:
        outcome, scores = admin.admin.reset(actor="owner", domain_id="routine", confirmed=True, now=admin.now)
        state = admin.tm.get_domain_state("routine")
        assert outcome.value == "reset" and scores == (0.0, DEFAULT_NEW_DOMAIN_SCORE) and state.score == DEFAULT_NEW_DOMAIN_SCORE
        assert state.confidence == pytest.approx(0.99)
        with admin.world.core.transaction() as tx:
            assert tx.fetch_one("SELECT COUNT(*) AS n FROM trust_evidence")["n"] == 1                 # the history stays
            consent = tx.fetch_one("SELECT target_id, action, user_comment FROM consent_log WHERE user_comment LIKE '%reset%'")
            recalc = tx.fetch_one("SELECT triggered_by, explanation FROM trust_recalculations WHERE triggered_by = 'manual'")
            event = tx.fetch_one("SELECT payload_json FROM domain_events WHERE event_type = 'trust_domain.recalculated' ORDER BY rowid DESC")
        assert consent["target_id"] == "trust_domain:routine" and consent["action"] == "approved"
        assert "consent" in recalc["explanation"] and '"triggered_by": "manual"' in event["payload_json"]
