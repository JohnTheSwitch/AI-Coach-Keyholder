"""v0.39 Phase F4: Progressive Conditioning — the payout threshold, the only trigger (a Chaster-confirmed token release), the consent and owner gates,
the freeze on `conditioning off`, the cap, secrecy and the owner CLI. No network."""

from __future__ import annotations

import ast
import io
import json
import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from infrastructure.consumer_registry import process_pending_events
from infrastructure.database import Database as CoreDatabase
from penalty_engine.repository import PenaltyEngine
from recovery_plan.repository import RecoveryPlanManager
from release_engine.conditioning import (
    BASE_THRESHOLD, FACTOR_CAP, FACTOR_START, FACTOR_STEP, ConditioningPreferences, IncrementOutcome, ProgressiveConditioning,
)
from release_engine.service import ReleaseEngine, ReleaseGates
from release_engine.token_pool import apply_day
from schedule_windows.windows import WindowConfig
from system.startup import build_consumer_registry
from trust_manager.repository import TrustManager

ROOT = Path(__file__).parent.parent.parent
PRAGUE = ZoneInfo("Europe/Prague")
CFG = WindowConfig.build(timezone_name="Europe/Prague", quiet_start="22:00", quiet_end="07:00",
                         work_start="07:00", work_end="16:30", holiday_country="CZ")
U = "wearer"
MON = date(2026, 10, 12)
NOW = datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc)
D = Decimal


def local(d: date, hh: int, mm: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=PRAGUE).astimezone(timezone.utc)


class W:
    def __init__(self, tmp_path: Path) -> None:
        self.core = CoreDatabase(tmp_path / "t.db")
        with self.core.raw_connection() as conn:
            for p in sorted((ROOT / "database" / "migrations").glob("*.sql")):
                conn.executescript(p.read_text(encoding="utf-8"))
        self.engine = ReleaseEngine(self.core.db_path, core=self.core, window_config=CFG, min_offset_minutes=30, max_offset_minutes=210,
                                    max_hold_hours=72, seed_factory=lambda: bytes(range(32)))
        self.cond = ProgressiveConditioning(self.core.db_path, core=self.core)
        self.prefs = ConditioningPreferences(self.core.db_path, core=self.core)
        self.cond.set_owner_enabled(True, now=NOW)
        self.n = 0

    def release(self, *, outcome="confirmed", kind="token", day_offset=0):
        """Drives one real release through the release engine; returns the authorization id."""
        self.n += 1
        day = MON + timedelta(days=day_offset)
        if kind == "token":
            self.engine.earn_tokens(user_id=U, amount=1, reason_code="t", source_event_id=f"e{self.n}", now=local(day, 9))
            self.engine.ensure_plan(user_id=U, now=local(day, 9))
            with self.core.transaction() as tx:
                target = datetime.fromisoformat(tx.fetch_one("SELECT target_at FROM release_plans WHERE status = 'planned'")["target_at"].replace("Z", "+00:00"))
            auth = self.engine.commit_authorization(user_id=U, gates=ReleaseGates(lock_barrier_confirmed=True), now=target).authorization_id
        else:
            auth = self.engine.commit_backstop(user_id=U, barrier_since=local(day, 18) - timedelta(hours=80),
                                               gates=ReleaseGates(lock_barrier_confirmed=True), now=local(day, 18)).authorization_id
        assert auth is not None
        at = local(day, 21)
        self.engine.mark_provider_requested(authorization_id=auth, now=at)
        if outcome == "confirmed":
            self.engine.mark_provider_confirmed(authorization_id=auth, now=at)
        elif outcome == "failed":
            self.engine.mark_provider_failed(authorization_id=auth, detail="x", now=at)
        elif outcome == "unknown":
            self.engine.mark_physical_state_unknown(authorization_id=auth, detail="x", now=at)
        return auth

    def drain(self, conditioning="default"):
        registry = build_consumer_registry(
            TrustManager(self.core.db_path, core=self.core), PenaltyEngine(self.core.db_path, core=self.core),
            RecoveryPlanManager(self.core.db_path, core=self.core), conditioning=self.cond if conditioning == "default" else conditioning)
        process_pending_events(self.core, registry, claimant="t", now=NOW + timedelta(days=30))

    def outcomes(self):
        with self.core.transaction() as tx:
            return [r["outcome"] for r in tx.fetch_all("SELECT outcome FROM training_increments ORDER BY rowid")]

    def advanced(self):
        with self.core.transaction() as tx:
            tx.execute("UPDATE operating_mode_state SET current_mode = 'advanced' WHERE id = 1")


@pytest.fixture
def w(tmp_path):
    return W(tmp_path)


class TestThreshold:
    def test_base_times_factor_in_both_modes(self, w) -> None:
        with w.core.transaction() as tx:
            assert w.cond.threshold_in(tx, U) == D("1.0") * FACTOR_START
        w.advanced()
        with w.core.transaction() as tx:
            assert w.cond.threshold_in(tx, U) == D("2.0")
            tx.execute("INSERT INTO training_state (user_id, factor, updated_at) VALUES (?, '1.250', 'x')", (U,))
            assert w.cond.threshold_in(tx, U) == D("2.500")
        assert BASE_THRESHOLD == {"standard": D("1.0"), "advanced": D("2.0")}

    @pytest.mark.parametrize("threshold,tokens_per_week", [(D("1.0"), 2), (D("2.0"), 1), (D("1.5"), 1)])
    def test_the_threshold_scales_the_payout(self, threshold, tokens_per_week) -> None:
        pool, earned = D(0), 0
        for _ in range(7):
            result = apply_day(pool, 1, 1, 1.0, threshold)
            pool, earned = result.pool_after, earned + result.tokens_earned
        assert earned == tokens_per_week

    def test_about_three_and_a_half_and_seven_days_per_token(self) -> None:
        def days_per_token(threshold):
            pool, days = D(0), 0
            while True:
                days += 1
                result = apply_day(pool, 1, 1, 1.0, threshold)
                pool = result.pool_after
                if result.tokens_earned:
                    return days
        assert days_per_token(D("1.0")) == 4 and days_per_token(D("2.0")) == 7                 # 0.286/day: ~3.5 and exactly 7.0 days
        assert days_per_token(D("2.0") * FACTOR_CAP) == 11

    def test_the_daily_evaluation_uses_the_threshold(self, tmp_path) -> None:
        from system.daily_evaluation import DailyEvaluationTask
        w = W(tmp_path)
        tm = TrustManager(w.core.db_path, core=w.core)
        tm.create_domain(domain_id="routine", display_name="R", description="d", created_via_consent_id="c", now=NOW - timedelta(days=10))
        with w.core.transaction() as tx:
            tx.execute("INSERT INTO token_pool_state (user_id, pool, updated_at) VALUES (?, '0.9', 'x')", (U,))
            tx.execute("INSERT INTO training_state (user_id, factor, updated_at) VALUES (?, '1.500', 'x')", (U,))
        task = DailyEvaluationTask(w.core.db_path, core=w.core, window_config=CFG, user_id=U, trust_domain="routine", engine=w.engine,
                                   trust=tm, conditioning=w.cond)
        task.run(local(MON + timedelta(days=1), 1))
        assert w.engine.get_balance(U) == 0                                                 # 0.9 + ~0.2 < 1.5
        plain = DailyEvaluationTask(w.core.db_path, core=w.core, window_config=CFG, user_id=U, trust_domain="routine", engine=w.engine, trust=tm)
        plain.run(local(MON + timedelta(days=2), 1))
        assert w.engine.get_balance(U) == 1                                                 # without conditioning the threshold is 1.0


class TestTrigger:
    def test_a_confirmed_token_release_raises_the_factor_by_one_step(self, w) -> None:
        w.release()
        w.drain()
        assert w.cond.factor(U) == FACTOR_START + FACTOR_STEP and w.outcomes() == ["increased"]

    def test_redelivery_never_raises_it_twice(self, w) -> None:
        w.release()
        w.drain()
        with w.core.transaction() as tx:
            tx.execute("UPDATE domain_events SET published_at = NULL WHERE event_type = 'release.provider_confirmed'")
            tx.execute("DELETE FROM domain_event_consumers")
        w.drain()
        assert w.cond.factor(U) == D("1.025") and len(w.outcomes()) == 1

    def test_the_backstop_never_counts(self, w) -> None:
        w.release(kind="backstop")
        w.drain()
        assert w.cond.factor(U) == FACTOR_START and w.outcomes() == ["not_token"]

    @pytest.mark.parametrize("outcome", ["failed", "unknown", "requested"])
    def test_failed_ambiguous_or_unconfirmed_releases_never_count(self, w, outcome) -> None:
        w.release(outcome=outcome)
        w.drain()
        assert w.cond.factor(U) == FACTOR_START and w.outcomes() == []

    def test_earning_a_token_alone_never_counts(self, w) -> None:
        w.engine.earn_tokens(user_id=U, amount=3, reason_code="t", source_event_id="x", now=NOW)
        w.drain()
        assert w.cond.factor(U) == FACTOR_START and w.outcomes() == []

    def test_manual_freezes_and_emergency_unlocks_have_no_path_to_the_factor(self) -> None:
        # Neither creates a release authorization or a `release.provider_confirmed` event; only the consumer of that event raises the factor.
        for rel in ("chaster_execution/manual_freeze.py", "chaster/emergency_unlock_provider.py", "chaster/emergency_unlock.py"):
            text = (ROOT / rel).read_text(encoding="utf-8")
            assert "release_authorizations" not in text and "provider_confirmed" not in text and "conditioning" not in text, rel
        callers = [p for p in ROOT.rglob("*.py") if "tests" not in p.parts and "on_release_confirmed_in_transaction" in p.read_text(encoding="utf-8")]
        assert sorted(str(p.relative_to(ROOT)) for p in callers) == [str(Path("release_engine/conditioning.py")), str(Path("system/startup.py"))]

    def test_the_cap(self, w) -> None:
        with w.core.transaction() as tx:
            tx.execute("INSERT INTO training_state (user_id, factor, updated_at) VALUES (?, '1.475', 'x')", (U,))
        w.release(day_offset=0)
        w.drain()
        w.release(day_offset=1)
        w.drain()
        assert w.cond.factor(U) == FACTOR_CAP and w.outcomes() == ["increased", "capped"]

    def test_twenty_releases_reach_exactly_the_cap(self, w) -> None:
        for i in range(21):
            w.release(day_offset=i)
            w.drain()
        assert w.cond.factor(U) == D("1.500") and w.outcomes().count("increased") == 20 and w.outcomes()[-1] == "capped"


class TestConsentAndSwitches:
    def test_conditioning_off_freezes_the_factor_and_on_resumes_from_there(self, w) -> None:
        w.release(day_offset=0)
        w.drain()
        w.prefs.set_enabled(U, False, now=NOW)
        for i in (1, 2):
            w.release(day_offset=i)
            w.drain()
        assert w.cond.factor(U) == D("1.025") and w.outcomes() == ["increased", "wearer_off", "wearer_off"]   # frozen, not reset
        w.prefs.set_enabled(U, True, now=NOW)
        w.release(day_offset=3)
        w.drain()
        assert w.cond.factor(U) == D("1.050")

    def test_the_owner_switch_is_required(self, w) -> None:
        w.cond.set_owner_enabled(False, now=NOW)
        w.release()
        w.drain()
        assert w.cond.factor(U) == FACTOR_START and w.outcomes() == ["owner_off"]

    def test_without_the_global_flag_nothing_is_registered(self, w) -> None:
        w.release()
        w.drain(conditioning=None)
        assert w.cond.factor(U) == FACTOR_START and w.outcomes() == []

    def test_releases_while_off_are_not_paid_back_later(self, w) -> None:
        w.prefs.set_enabled(U, False, now=NOW)
        w.release()
        w.drain()
        w.prefs.set_enabled(U, True, now=NOW)
        w.drain()
        assert w.cond.factor(U) == FACTOR_START

    def test_the_composition_builds_it_only_with_the_global_flag(self, tmp_path) -> None:
        from system.composition import build_rules_and_execution
        from tests.chaster_execution.test_wiring import _chaster_parts, core as _unused  # noqa: F401
        from tests.system.test_phase2_composition import config
        core = CoreDatabase(tmp_path / "c.db")
        with core.raw_connection() as conn:
            for p in sorted((ROOT / "database" / "migrations").glob("*.sql")):
                conn.executescript(p.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO user_accounts (id, created_at, last_seen_at) VALUES ('w', ?, ?)", (NOW.isoformat(),) * 2)
            conn.execute("INSERT INTO user_accounts (id, created_at, last_seen_at) VALUES ('k', ?, ?)", (NOW.isoformat(),) * 2)
            conn.commit()
        on = build_rules_and_execution(config(core, enable_release_engine=True, enable_slave_training=True), core, **_chaster_parts(core))
        off = build_rules_and_execution(config(core, enable_release_engine=True), core, **_chaster_parts(core))
        assert on.release_task.conditioning is not None and on.daily._conditioning is on.release_task.conditioning
        assert off.release_task.conditioning is None and off.daily._conditioning is None


class TestSecrecy:
    def test_the_factor_never_appears_in_events_or_logs(self, w, caplog) -> None:
        caplog.set_level(logging.DEBUG)
        for i in range(2):
            w.release(day_offset=i)
            w.drain()
        with w.core.transaction() as tx:
            payloads = " ".join(r["payload_json"] for r in tx.fetch_all("SELECT payload_json FROM domain_events"))
        assert "1.025" not in payloads and "1.050" not in payloads and "factor" not in payloads
        assert "1.025" not in caplog.text and "1.050" not in caplog.text

    def test_the_model_facing_release_phase_is_unchanged(self, w) -> None:
        w.release()
        w.drain()
        assert w.engine.coarse_phase(U) in ("requirements_met_pending", "requirements_not_met")

    def test_nothing_on_the_model_or_wearer_path_reads_the_factor(self) -> None:
        for package in ("conversation_engine", "application", "coaching_memory", "idea_box", "messaging", "bot"):
            for path in (ROOT / package).rglob("*.py"):
                text = path.read_text(encoding="utf-8")
                for node in ast.walk(ast.parse(text)):
                    if isinstance(node, ast.ImportFrom) and node.module == "release_engine.conditioning":
                        assert all(a.name == "ConditioningPreferences" for a in node.names), path
                assert "training_state" not in text and "factor_in" not in text, path

    def test_the_wearers_status_reply_has_no_number(self, tmp_path) -> None:
        from application.models import IncomingMessage
        from application.service import ApplicationService
        from tests.application.test_chaster_llm_tool_integration import _apply_migrations, _onboard
        core = CoreDatabase(tmp_path / "s.db")
        _apply_migrations(core)
        service = ApplicationService(core.db_path, core=core, conditioning=ConditioningPreferences(core.db_path, core=core))
        _onboard(service)
        user = service.user_service.get_or_create_user("discord", "42", now=NOW)
        with core.transaction() as tx:
            tx.execute("INSERT INTO training_state (user_id, factor, updated_at) VALUES (?, '1.375', 'x')", (user.id,))
        reply = service.handle_message(IncomingMessage(channel="discord", external_user_id="42", text="conditioning status",
                                                       received_at=NOW, external_message_id="m")).text
        assert "1.375" not in reply and not any(ch.isdigit() for ch in reply)


class TestOwnerCli:
    def test_enable_disable_status_are_owner_only(self, tmp_path) -> None:
        from cli.main import main
        from tenancy.admin import TenantAdmin
        from tests.system.test_tenant_factory import OWNER, World
        world = World(tmp_path)
        runtime = world.factory.build(world.wearer("Alex", "2000002").id)
        admin = TenantAdmin(registry=world.platform.registry, halt=world.platform.halt, owner_user_id=OWNER, clock=world.clock)

        def cli(*args):
            out = io.StringIO()
            code = main(["tenants", "slave-training", *args], out=out, tenants_factory=lambda: (admin, world.platform), now=lambda: NOW)
            return code, out.getvalue()

        cond = ProgressiveConditioning(runtime.db_path, core=runtime.core)
        assert cli(runtime.tenant_id, "--enable", "--owner-id", "mallory")[0] == 3 and cond.owner_enabled() is False
        code, text = cli(runtime.tenant_id, "--enable", "--owner-id", OWNER)
        assert code == 0 and "on" in text and cond.owner_enabled() is True
        assert cli(runtime.tenant_id, "--disable", "--owner-id", OWNER)[0] == 0 and cond.owner_enabled() is False
        with runtime.core.transaction() as tx:
            tx.execute("INSERT INTO training_state (user_id, factor, updated_at) VALUES ('abcdef12-x', '1.100', 'x')")
        assert "factor 1.100" in cli(runtime.tenant_id, "--status", "--owner-id", OWNER)[1]          # the owner may see it
        assert cli("tenant_nope", "--status", "--owner-id", OWNER)[0] == 3
        with pytest.raises(SystemExit):
            cli(runtime.tenant_id, "--owner-id", OWNER)                                           # one of the three is required

    def test_flag_default(self) -> None:
        from core.config import Config
        assert Config.__dataclass_fields__["enable_slave_training"].default is False
        assert "ENABLE_SLAVE_TRAINING=false" in (ROOT / ".env.example").read_text(encoding="utf-8")
