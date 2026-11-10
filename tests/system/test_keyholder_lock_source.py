"""RELEASE_LOCK_ID: the owner's own keyholder lock observed as its keyholder, so the release engine needs no wearer connection. Fake edges only, no network."""

from __future__ import annotations

from datetime import timedelta

import pytest

from chaster.lock_client import ChasterLockApiError
from infrastructure.clock import FrozenClock
from system.composition import build_rules_and_execution
from system.keyholder_lock_source import KeyholderLockObservationSource, check_keyholder_lock
from tests.chaster_execution.test_wiring import _chaster_parts, core  # noqa: F401
from tests.system.test_phase2_composition import MON, Sender, config, cycle, full_world, local, target
from tests.system.test_release_tick import FakeTransport as BarrierTransport

LOCK = "lock-9"


def body(**kw):
    raw = {"_id": LOCK, "role": "keyholder", "status": "locked", "isFrozen": True, "frozenAt": "2026-10-12T06:00:00Z",
           "endDate": "2026-10-20T06:00:00Z", "user": {"_id": "wearer-acct"}, "keyholder": {"_id": "kh-acct"}}
    raw.update(kw)
    return raw


class Tokens:
    def call_with_token(self, user_id, *, now, operation):
        return operation("token")


class Client:
    def __init__(self, raw=None, error=None) -> None:
        self.raw, self.error, self.calls = raw, error, []

    def get_lock(self, *, access_token, lock_id):
        self.calls.append(lock_id)
        if self.error is not None:
            raise self.error
        return self.raw


def source(core, client):  # noqa: F811
    return KeyholderLockObservationSource(core.db_path, core=core, lock_id=LOCK, wearer_user_id="w", keyholder_user_id="k",
                                          keyholder_tokens=Tokens(), lock_client=client, clock=FrozenClock(local(MON, 10)))


class TestTheCheck:
    def test_a_locked_lock_this_account_keyholds(self) -> None:
        assert check_keyholder_lock(body(), lock_id=LOCK) == "observed"

    @pytest.mark.parametrize("raw", [body(role="wearer"), body(_id="other"), None, "x", body(role=None)])
    def test_not_ours_to_act_on(self, raw) -> None:
        assert check_keyholder_lock(raw, lock_id=LOCK) == "mismatch"

    def test_not_locked(self) -> None:
        assert check_keyholder_lock(body(status="unlocked"), lock_id=LOCK) == "not_found"


class TestTheSource:
    def test_an_observation_carries_the_frozen_state(self, core) -> None:  # noqa: F811
        s = source(core, Client(body()))
        assert s.refresh(local(MON, 10)) == "observed"
        obs = s.get_latest()
        assert obs.chaster_lock_id == LOCK and obs.is_frozen is True and obs.has_keyholder is True and obs.connection_id == "w"

    def test_unfrozen(self, core) -> None:  # noqa: F811
        s = source(core, Client(body(isFrozen=False)))
        s.refresh(local(MON, 10))
        assert s.get_latest().is_frozen is False

    def test_a_lock_this_account_only_wears_is_never_handed_on(self, core) -> None:  # noqa: F811
        s = source(core, Client(body()))
        s.refresh(local(MON, 10))
        s._client = Client(body(role="wearer"))
        assert s.refresh(local(MON, 11)) == "mismatch" and s.get_latest() is None

    def test_a_failed_read_records_nothing(self, core) -> None:  # noqa: F811
        s = source(core, Client(error=ChasterLockApiError("x", status_code=500)))
        assert s.refresh(local(MON, 10)) is None and s.get_latest() is None

    def test_no_reading_no_observation(self, core) -> None:  # noqa: F811
        assert source(core, Client(body())).get_latest() is None


class TestWiring:
    def test_the_release_engine_runs_without_a_wearer_connection(self, core) -> None:  # noqa: F811
        w = build_rules_and_execution(config(core, enable_release_engine=True, chaster_execution_wearer_user_id=None, owner_user_id="o",
                                             release_lock_id=LOCK), core, **_chaster_parts(core))
        assert w.release_engine is not None and w.release_user_id == "o" and any(f"Release lock {LOCK}" in n for n in w.notes)

    def test_an_explicit_wearer_still_wins(self, core) -> None:  # noqa: F811
        w = build_rules_and_execution(config(core, enable_release_engine=True, owner_user_id="o", release_lock_id=LOCK), core, **_chaster_parts(core))
        assert w.release_user_id == "w"

    def test_unset_changes_nothing(self, core) -> None:  # noqa: F811
        w = build_rules_and_execution(config(core, enable_release_engine=True, chaster_execution_wearer_user_id=None, owner_user_id="o"), core,
                                      **_chaster_parts(core))
        assert w.release_engine is None


class TestTheWholeChain:
    def test_a_token_unfreezes_the_configured_lock_and_only_that(self, core) -> None:  # noqa: F811
        from trust_manager.repository import TrustManager
        TrustManager(core.db_path, core=core).create_domain(domain_id="d", display_name="D", description="d", created_via_consent_id="c1", now=local(MON, 9))
        transport = BarrierTransport()
        parts = _chaster_parts(core)
        parts["lock_client"].get_lock.return_value = body()
        w = build_rules_and_execution(
            config(core, enable_chaster_execution=True, enable_release_engine=True, enable_check_ins=True, check_in_trust_domain="d",
                   chaster_execution_wearer_user_id=None, owner_user_id="w", release_lock_id=LOCK),
            core, transport=transport, check_in_sender=Sender(), keyholder_token_manager=Tokens(), **parts)
        from chaster_execution.control import ExecutionControl
        ExecutionControl(core.db_path, core=core).set_halted(False, reason="test", now=local(MON, 9))
        w.release_engine.earn_tokens(user_id="w", amount=1, reason_code="test", source_event_id="e1", now=local(MON, 9))
        cycle(core, w, local(MON, 10), "p1")
        t = target(core)
        cycle(core, w, t, "p2")                                     # told first; today's check-in is requested
        assert transport.calls == []
        assert w.check_ins.on_user_message("w", "all good today", t + timedelta(minutes=1))
        cycle(core, w, t + timedelta(minutes=2), "p3")
        assert transport.calls == [("unfreeze", LOCK)] and w.release_engine.get_balance("w") == 0
