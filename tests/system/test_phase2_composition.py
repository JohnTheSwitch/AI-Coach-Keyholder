"""v0.39 Phase 2 composition: flags, fail-closed wiring, and the whole periodic cycle end to end (fake edges only)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from chaster.models import ChasterProviderStatus
from chaster.provider_observation_repository import ChasterProviderObservationRecorder
from chaster_execution.control import ExecutionControl
from chaster_execution.executor import ChasterExecutor
from infrastructure.clock import FrozenClock
from infrastructure.time_format import parse_iso
from supervision.checkins import SendOutcome
from system.composition import build_rules_and_execution, window_config_from
from system.periodic import run_periodic_cycle
from tests.chaster_execution.test_wiring import FakeTransport, _chaster_parts, core  # noqa: F401
from tests.system.test_release_tick import FakeTransport as BarrierTransport

PRAGUE = ZoneInfo("Europe/Prague")
MON = date(2026, 10, 12)


def local(d, hh, mm=0):
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=PRAGUE).astimezone(timezone.utc)


def config(core, **kw):
    base = dict(
        db_path=core.db_path, enable_rules_engine=True, enable_chaster_execution=False, enable_supervision=False,
        enable_release_engine=False, enable_check_ins=False, check_in_trust_domain=None, check_in_user_id=None,
        chaster_execution_wearer_user_id="w", chaster_execution_keyholder_user_id="k",
        app_timezone="Europe/Prague", quiet_hours_start="22:00", quiet_hours_end="07:00", workday_start="07:00",
        workday_end="16:30", holiday_country="CZ", weekend_release_start="10:00",
        unlock_window_min_offset_minutes=30, unlock_window_max_offset_minutes=210, max_barrier_hold_hours=72,
    )
    base.update(kw)
    return SimpleNamespace(**base)


class Sender:
    def __init__(self) -> None:
        self.sent = []

    def send(self, *, user_id, text):
        self.sent.append((user_id, text))
        return SendOutcome.DELIVERED


class TestFlags:
    def test_everything_is_off_by_default(self, core) -> None:
        w = build_rules_and_execution(config(core), core, **_chaster_parts(core))
        assert w.release_engine is None and w.release_task is None and w.check_ins is None
        assert w.release_phase("w") is None

    def test_defaults_in_the_real_config(self) -> None:
        from core.config import Config
        f = Config.__dataclass_fields__
        assert f["enable_release_engine"].default is False and f["enable_check_ins"].default is False
        assert f["weekend_release_start"].default == "10:00" and f["max_barrier_hold_hours"].default == 72

    def test_env_example_documents_the_flags(self) -> None:
        from pathlib import Path
        text = (Path(__file__).parent.parent.parent / ".env.example").read_text(encoding="utf-8")
        for line in ("ENABLE_RELEASE_ENGINE=false", "ENABLE_CHECK_INS=false", "WEEKEND_RELEASE_START=10:00",
                     "MAX_BARRIER_HOLD_HOURS=72"):
            assert line in text

    def test_release_needs_the_rules_engine(self, core) -> None:
        w = build_rules_and_execution(config(core, enable_rules_engine=False, enable_release_engine=True), core,
                                      **_chaster_parts(core))
        assert w.release_engine is None and any("NOT active" in n and "Rules Engine" in n for n in w.notes)

    def test_release_needs_chaster_and_a_wearer(self, core) -> None:
        w = build_rules_and_execution(config(core, enable_release_engine=True), core)
        assert w.release_engine is None and any("release engine is NOT active" in n for n in w.notes)

    def test_invalid_scheduling_config_is_reported_not_raised(self, core) -> None:
        w = build_rules_and_execution(config(core, enable_release_engine=True, app_timezone="Mars/Base"), core,
                                      **_chaster_parts(core))
        assert w.release_engine is None and any("invalid scheduling configuration" in n for n in w.notes)

    def test_offsets_that_reach_quiet_hours_are_rejected(self, core) -> None:
        w = build_rules_and_execution(config(core, enable_release_engine=True, unlock_window_max_offset_minutes=400),
                                      core, **_chaster_parts(core))
        assert w.release_engine is None and any("NOT active" in n for n in w.notes)

    def test_release_without_execution_is_planning_only(self, core) -> None:
        w = build_rules_and_execution(config(core, enable_release_engine=True), core, **_chaster_parts(core))
        assert w.release_engine is not None and w.executor is None
        assert w.release_task._can_execute is False

    def test_barrier_actions_are_enabled_only_together_with_the_release_engine(self, core) -> None:
        plain = build_rules_and_execution(config(core, enable_chaster_execution=True), core, transport=FakeTransport(),
                                          **_chaster_parts(core))
        with_release = build_rules_and_execution(config(core, enable_chaster_execution=True, enable_release_engine=True),
                                                 core, transport=BarrierTransport(), **_chaster_parts(core))
        assert plain.executor._allow_barrier is False and plain.executor._window_check is None
        assert with_release.executor._allow_barrier is True and with_release.executor._window_check is not None
        assert with_release.release_task._can_execute is True

    def test_release_phase_is_only_for_the_wearer_and_is_coarse(self, core) -> None:
        w = build_rules_and_execution(config(core, enable_release_engine=True), core, **_chaster_parts(core))
        assert w.release_phase("someone-else") is None
        assert w.release_phase("w") == "requirements_not_met"
        w.release_engine.earn_tokens(user_id="w", amount=1, reason_code="r", source_event_id="e", now=local(MON, 9))
        assert w.release_phase("w") == "requirements_met_pending"

    def test_window_config_from_the_app_config(self, core) -> None:
        cfg = window_config_from(config(core))
        assert str(cfg.tz) == "Europe/Prague" and cfg.weekend_release_start.hour == 10


class TestCheckInWiring:
    def test_needs_user_domain_and_sender(self, core) -> None:
        for kw, sender in ((dict(), None), (dict(check_in_trust_domain="d"), Sender()), (dict(), Sender())):
            w = build_rules_and_execution(config(core, enable_check_ins=True, chaster_execution_wearer_user_id=None, **kw),
                                          core, check_in_sender=sender)
            assert w.check_ins is None and any("check-ins are NOT active" in n for n in w.notes)

    def test_active_when_complete_and_defaults_to_the_wearer(self, core) -> None:
        w = build_rules_and_execution(config(core, enable_check_ins=True, check_in_trust_domain="d"), core,
                                      check_in_sender=Sender())
        assert w.check_ins is not None and w.check_ins._user == "w"

    def test_explicit_user_wins(self, core) -> None:
        w = build_rules_and_execution(config(core, enable_check_ins=True, check_in_trust_domain="d", check_in_user_id="x"),
                                      core, check_in_sender=Sender())
        assert w.check_ins._user == "x"

    def test_check_ins_work_without_the_rules_engine(self, core) -> None:
        w = build_rules_and_execution(config(core, enable_rules_engine=False, enable_check_ins=True,
                                             check_in_trust_domain="d"), core, check_in_sender=Sender())
        assert w.check_ins is not None and w.rules_engine is None


def full_world(core, transport, **overrides):
    parts = _chaster_parts(core)
    wiring = build_rules_and_execution(
        config(core, enable_chaster_execution=True, enable_release_engine=True, enable_supervision=True,
               enable_check_ins=True, check_in_trust_domain="d", **overrides),
        core, transport=transport, check_in_sender=Sender(), **parts,
    )
    ExecutionControl(core.db_path, core=core).set_halted(False, reason="test", now=local(MON, 9))
    return wiring


def record_obs(core, at, **kw):
    args = dict(connection_id="w", provider="chaster", chaster_lock_id="lock-5", status=ChasterProviderStatus.LOCKED,
                fetched_at=at, now=at, is_frozen=True, has_keyholder=True, frozen_at=at - timedelta(hours=2))
    args.update(kw)
    return ChasterProviderObservationRecorder(core.db_path, core=core).record(**args)


def cycle(core, w, at, name):
    return run_periodic_cycle(core, name, FrozenClock(at), rules_engine=w.rules_engine, executor=w.executor,
                              supervision=w.supervision, release_task=w.release_task, check_ins=w.check_ins)


def target(core):
    with core.transaction() as tx:
        return parse_iso(tx.fetch_one("SELECT target_at FROM release_plans WHERE status = 'planned'")["target_at"])


class TestWholePeriodicCycle:
    def _setup(self, core):
        from trust_manager.repository import TrustManager
        TrustManager(core.db_path, core=core).create_domain(
            domain_id="d", display_name="D", description="d", created_via_consent_id="c1", now=local(MON, 9))
        transport = BarrierTransport()
        return full_world(core, transport), transport

    def test_a_token_leads_to_a_secret_release_through_the_real_cycle(self, core) -> None:
        w, transport = self._setup(core)
        w.release_engine.earn_tokens(user_id="w", amount=1, reason_code="goal", source_event_id="e1", now=local(MON, 9))
        now = local(MON, 10)
        record_obs(core, now)
        assert cycle(core, w, now, "p1")
        t = target(core)
        assert transport.calls == []
        record_obs(core, t - timedelta(minutes=1))
        cycle(core, w, t, "p2")
        assert transport.calls == [("unfreeze", "lock-5")]
        assert w.release_engine.get_balance("w") == 0
        cycle(core, w, t + timedelta(minutes=1), "p3")
        assert len(transport.calls) == 1

    def test_without_a_fresh_observation_nothing_is_ever_sent(self, core) -> None:
        w, transport = self._setup(core)
        w.release_engine.earn_tokens(user_id="w", amount=1, reason_code="goal", source_event_id="e1", now=local(MON, 9))
        cycle(core, w, local(MON, 10), "p1")                      # no observation exists and the refresh edge is a mock
        assert transport.calls == [] and w.release_engine.get_balance("w") == 1

    def test_a_halted_system_changes_nothing(self, core) -> None:
        w, transport = self._setup(core)
        ExecutionControl(core.db_path, core=core).set_halted(True, reason="stop", now=local(MON, 9))
        w.release_engine.earn_tokens(user_id="w", amount=1, reason_code="goal", source_event_id="e1", now=local(MON, 9))
        now = local(MON, 10)
        record_obs(core, now)
        cycle(core, w, now, "p1")
        t = target(core) if w.release_engine.has_planned_release("w") else local(MON, 20)
        record_obs(core, t - timedelta(minutes=1))
        cycle(core, w, t, "p2")
        assert transport.calls == [] and w.release_engine.get_balance("w") == 1

    def test_a_missed_check_in_vetoes_a_pending_release(self, core) -> None:
        w, transport = self._setup(core)
        w.release_engine.earn_tokens(user_id="w", amount=1, reason_code="goal", source_event_id="e1", now=local(MON, 9))
        now = local(MON, 10)
        record_obs(core, now)
        cycle(core, w, now, "p1")
        assert w.release_engine.has_planned_release("w")
        with core.transaction() as tx:
            tx.execute("INSERT INTO check_ins (id, user_id, local_date, slot_index, level, due_at, status, created_at, updated_at) "
                       "VALUES ('c', 'w', '2026-10-12', 9, 2, ?, 'missed', ?, ?)", ((now - timedelta(hours=1)).isoformat(),) * 3)
        record_obs(core, now + timedelta(minutes=5))
        cycle(core, w, now + timedelta(minutes=5), "p2")
        assert not w.release_engine.has_planned_release("w") and transport.calls == []

    def test_check_ins_and_release_run_in_the_same_cycle_without_interference(self, core) -> None:
        w, transport = self._setup(core)
        now = local(MON, 10)
        record_obs(core, now)
        w.release_engine.earn_tokens(user_id="w", amount=1, reason_code="goal", source_event_id="e1", now=local(MON, 9))
        cycle(core, w, now, "p1")
        with core.transaction() as tx:
            n = tx.fetch_one("SELECT COUNT(*) AS n FROM check_ins")["n"]
        assert n == 2                                   # trust 0.6 = level 2: two check-ins today
        assert w.release_engine.has_planned_release("w")   # and the release plan was made in the same cycle
