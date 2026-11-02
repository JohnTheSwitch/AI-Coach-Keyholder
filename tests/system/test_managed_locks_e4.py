"""v0.39 Phase E4: managed-lock observation through the keyholder search, the pairing check, and their isolation per tenant. No network."""

from __future__ import annotations

import asyncio
import io
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from chaster.models import ChasterProviderStatus
from cli.main import main
from system.lock_pairing import LockPairingService, PairOutcome
from system.managed_lock_source import LockCheck, ManagedLockObservationSource, verify_lock
from system.tenant_gateway import TEXTS
from tenancy.admin import TenantAdmin
from tests.system.test_tenant_factory import NOW, OWNER, World
from tests.system.test_tenant_gateway import G

BOT = "bot-acc"
WEARER = "wearer-acc"


def lock(lock_id="lock-1", *, wearer=WEARER, keyholder=BOT, status="locked", frozen=True):
    return {"_id": lock_id, "title": "Weekend", "status": status, "isFrozen": frozen, "frozenAt": "2026-10-12T10:00:00.000Z",
            "endDate": "2026-10-13T10:00:00.000Z", "user": {"_id": wearer, "username": "alex_c"},
            "keyholder": {"_id": keyholder, "username": "the_bot"}, "combination": "SECRET-COMBO"}


class Chaster:
    """Fake Chaster: what the keyholder search and the wearer's own read return, and whether they fail."""

    def __init__(self) -> None:
        self.keyholder_locks = [lock()]
        self.wearer_locks = [lock()]
        self.search_fails = False
        self.calls: list[str] = []

    def search_keyholder_locks(self, *, access_token, status):
        self.calls.append(f"search:{access_token}:{status}")
        if self.search_fails:
            raise RuntimeError("chaster down")
        return list(self.keyholder_locks)

    def list_active_locks(self, *, access_token):
        self.calls.append(f"wearer:{access_token}")
        return list(self.wearer_locks)


class Tokens:
    def call_with_token(self, user_id, *, now, operation):
        return operation(f"token-of-{user_id}")


class Connections:
    def __init__(self, entries) -> None:
        self.entries = entries

    def get(self, user_id):
        return self.entries.get(user_id)


def kh_connections(scopes=("locks", "keyholder")):
    return Connections({"bot": SimpleNamespace(chaster_account_id=BOT, granted_scopes=scopes)})


class World4:
    def __init__(self, tmp_path) -> None:
        self.w = World(tmp_path)
        self.reg = self.w.platform.registry
        self.chaster = Chaster()
        self.tenant = self.w.wearer("Alex", "2000002")
        self.runtime = self.w.factory.build(self.tenant.id)

    def source(self, *, scopes=("locks", "keyholder"), wearer_connected=False, tenant_id=None, runtime=None):
        runtime = runtime or self.runtime
        wearer_conns = Connections({runtime.wearer_user_id: SimpleNamespace(chaster_account_id=WEARER)}) if wearer_connected else None
        tid = tenant_id or self.tenant.id
        return ManagedLockObservationSource(
            runtime.db_path, core=runtime.core, get_tenant=lambda: self.reg.get(tid), wearer_user_id=runtime.wearer_user_id,
            keyholder_user_id="bot", keyholder_tokens=Tokens(), keyholder_connections=kh_connections(scopes), lock_client=self.chaster,
            clock=self.w.clock, wearer_tokens=Tokens() if wearer_connected else None, wearer_connections=wearer_conns)

    def pair(self, lock_id="lock-1", wearer=WEARER, confirm=True, tenant_id=None):
        tid = tenant_id or self.tenant.id
        self.reg.pair_lock(tid, lock_id=lock_id, chaster_account_id=wearer, actor="owner", now=NOW)
        if confirm:
            self.reg.confirm_pairing(tid, actor="wearer", now=NOW)

    def rows(self, runtime=None):
        with (runtime or self.runtime).core.transaction() as tx:
            return [dict(r) for r in tx.fetch_all("SELECT * FROM managed_lock_observations ORDER BY rowid")]

    def pairing(self, scopes=("locks", "keyholder"), wearer_account=None):
        return LockPairingService(registry=self.reg, lock_client=self.chaster, keyholder_tokens=Tokens(),
                                  keyholder_connections=kh_connections(scopes), keyholder_user_id="bot", clock=self.w.clock,
                                  wearer_account_of=lambda t: wearer_account)


@pytest.fixture
def e(tmp_path):
    return World4(tmp_path)


class TestVerify:
    def test_the_paired_lock_held_by_the_bot_is_observed(self) -> None:
        outcome, match = verify_lock([lock("x"), lock()], lock_id="lock-1", wearer_chaster_id=WEARER, bot_chaster_id=BOT)
        assert outcome == LockCheck.OBSERVED and match["_id"] == "lock-1"

    @pytest.mark.parametrize("raw,expected", [
        ([lock("other")], LockCheck.NOT_FOUND), ([], LockCheck.NOT_FOUND),
        ([lock(wearer="someone-else")], LockCheck.MISMATCH), ([lock(keyholder="another-keyholder")], LockCheck.MISMATCH),
        ([lock(status="unlocked")], LockCheck.NOT_FOUND), ([lock(status="deserted")], LockCheck.NOT_FOUND),
        (["junk", {"_id": "lock-1"}], LockCheck.MISMATCH)])
    def test_everything_else_fails_closed(self, raw, expected) -> None:
        assert verify_lock(raw, lock_id="lock-1", wearer_chaster_id=WEARER, bot_chaster_id=BOT)[0] == expected


class TestObservationSource:
    @pytest.mark.parametrize("state", ["unpaired", "unconfirmed", "suspended", "removed"])
    def test_nothing_is_read_or_returned_unless_the_lock_is_ready(self, e, state) -> None:
        if state != "unpaired":
            e.pair(confirm=False)
        if state == "suspended":
            e.reg.confirm_pairing(e.tenant.id, actor="w", now=NOW)
            e.reg.suspend(e.tenant.id, actor="owner", now=NOW)
        if state == "removed":
            e.reg.confirm_pairing(e.tenant.id, actor="w", now=NOW)
            e.reg.remove(e.tenant.id, actor="owner", now=NOW)
        src = e.source()
        assert src.refresh(NOW) is None and src.get_latest() is None and e.chaster.calls == [] and e.rows() == []

    def test_a_confirmed_pairing_yields_a_verified_observation(self, e) -> None:
        e.pair()
        src = e.source()
        assert src.refresh(NOW + timedelta(minutes=1)) == LockCheck.OBSERVED
        obs = src.get_latest()
        assert (obs.status, obs.chaster_lock_id, obs.connection_id, obs.has_keyholder, obs.is_frozen) == (
            ChasterProviderStatus.LOCKED, "lock-1", e.runtime.wearer_user_id, True, True)
        assert obs.frozen_at is not None and obs.end_date is not None
        assert e.chaster.calls == ["search:token-of-bot:locked"]                         # the KEYHOLDER token, the locked search

    def test_the_combination_is_never_stored(self, e) -> None:
        e.pair()
        e.source().refresh(NOW)
        assert "SECRET" not in repr(e.rows())

    def test_an_unlocked_or_missing_lock_makes_execution_impossible(self, e) -> None:
        e.pair()
        src = e.source()
        src.refresh(NOW)
        e.chaster.keyholder_locks = []                                                  # unlocked, archived or gone: not in the LOCKED search
        assert src.refresh(NOW + timedelta(minutes=2)) == LockCheck.NOT_FOUND and src.get_latest() is None
        assert [r["outcome"] for r in e.rows()] == ["observed", "not_found"]

    def test_a_lock_now_held_by_someone_else_is_a_mismatch(self, e) -> None:
        e.pair()
        e.chaster.keyholder_locks = [lock(keyholder="another-keyholder")]
        src = e.source()
        assert src.refresh(NOW) == LockCheck.MISMATCH and src.get_latest() is None

    def test_if_the_keyholder_search_fails_the_wearers_own_connection_is_used(self, e) -> None:
        e.pair()
        e.chaster.search_fails = True
        src = e.source(wearer_connected=True)
        assert src.refresh(NOW) == LockCheck.OBSERVED
        assert e.rows()[-1]["source"] == "wearer_connection" and src.get_latest() is not None

    def test_without_the_keyholder_scope_and_without_a_wearer_connection_nothing_is_recorded(self, e) -> None:
        e.pair()
        src = e.source(scopes=("locks",))
        assert src.refresh(NOW) is None and e.rows() == [] and src.get_latest() is None

    def test_a_failed_search_records_nothing_and_the_last_state_goes_stale(self, e) -> None:
        e.pair()
        src = e.source()
        src.refresh(NOW)
        e.chaster.search_fails = True
        assert src.refresh(NOW + timedelta(minutes=5)) is None and len(e.rows()) == 1
        assert src.get_latest().fetched_at == NOW                                      # the executor's freshness rule decides

    def test_re_pairing_invalidates_old_observations(self, e) -> None:
        e.pair()
        src = e.source()
        src.refresh(NOW)
        e.reg.pair_lock(e.tenant.id, lock_id="lock-1", chaster_account_id=WEARER, actor="owner", now=NOW + timedelta(minutes=1))
        assert src.get_latest() is None                                                 # pairing not confirmed again yet
        e.reg.confirm_pairing(e.tenant.id, actor="w", now=NOW + timedelta(minutes=2))
        assert src.get_latest() is None                                                 # the old row predates the new pairing
        src.refresh(NOW + timedelta(minutes=3))
        assert src.get_latest() is not None

    def test_a_registry_failure_counts_as_unknown(self, e) -> None:
        def boom():
            raise RuntimeError("platform db locked")
        src = e.source()
        src._get_tenant = boom
        assert src.get_latest() is None and src.refresh(NOW) is None


class TestTenantIsolation:
    def test_each_tenant_only_ever_sees_its_own_paired_lock(self, e) -> None:
        bo = e.w.wearer("Bo", "3000003")
        rb = e.w.factory.build(bo.id)
        e.chaster.keyholder_locks = [lock("lock-1"), lock("lock-2", wearer="bo-acc")]
        e.pair("lock-1", WEARER)
        e.pair("lock-2", "bo-acc", tenant_id=bo.id)
        sa, sb = e.source(), e.source(tenant_id=bo.id, runtime=rb)
        sa.refresh(NOW)
        sb.refresh(NOW)
        assert sa.get_latest().chaster_lock_id == "lock-1" and sb.get_latest().chaster_lock_id == "lock-2"
        assert {r["lock_id"] for r in e.rows()} == {"lock-1"} and {r["lock_id"] for r in e.rows(rb)} == {"lock-2"}

    def test_one_lock_can_never_be_paired_to_two_tenants(self, e) -> None:
        import sqlite3
        bo = e.w.wearer("Bo", "3000003")
        e.pair("lock-1", WEARER)
        with pytest.raises(sqlite3.IntegrityError):
            e.reg.pair_lock(bo.id, lock_id="lock-1", chaster_account_id="bo-acc", actor="owner", now=NOW)

    def test_the_tenant_runtime_uses_the_managed_lock_source(self, tmp_path) -> None:
        w = World(tmp_path)
        w.config = replace(w.config, enable_release_engine=True, chaster_execution_keyholder_user_id="bot")
        chaster = Chaster()
        w.shared = replace(w.shared, config=w.config, lock_client=chaster, keyholder_token_manager=Tokens(),
                           keyholder_connections=kh_connections())
        from system.tenant_factory import TenantFactory
        factory = TenantFactory(registry=w.platform.registry, shared=w.shared, tenant_dir=w.platform.tenant_dir)
        tenant = w.wearer("Alex", "2000002")
        runtime = factory.build(tenant.id)
        task = runtime.wiring.release_task
        assert task is not None and task._get_obs() is None                             # unpaired: the release tick sees nothing
        w.platform.registry.pair_lock(tenant.id, lock_id="lock-1", chaster_account_id=WEARER, actor="owner", now=NOW)
        w.platform.registry.confirm_pairing(tenant.id, actor="w", now=NOW)
        task._refresh(NOW)
        assert task._get_obs().chaster_lock_id == "lock-1" and chaster.calls == ["search:token-of-bot:locked"]


class TestPairing:
    def test_the_owner_pairs_and_the_wearer_must_confirm(self, e) -> None:
        result = e.pairing().pair(e.tenant.id, "lock-1", actor="owner")
        t = e.reg.get(e.tenant.id)
        assert result.outcome is PairOutcome.PAIRED_AWAITING_WEARER and (result.title, result.wearer_username) == ("Weekend", "alex_c")
        assert (t.lock_id, t.chaster_account_id, t.pairing_confirmed_at, t.lock_ready) == ("lock-1", WEARER, None, False)

    def test_the_wearers_own_connected_account_confirms_automatically(self, e) -> None:
        assert e.pairing(wearer_account=WEARER).pair(e.tenant.id, "lock-1", actor="owner").outcome is PairOutcome.PAIRED_CONFIRMED
        assert e.reg.get(e.tenant.id).lock_ready

    def test_a_different_own_account_does_not_confirm(self, e) -> None:
        assert e.pairing(wearer_account="other-acc").pair(e.tenant.id, "lock-1", actor="owner").outcome is PairOutcome.PAIRED_AWAITING_WEARER

    @pytest.mark.parametrize("locks,outcome", [([], PairOutcome.NOT_MANAGED), ([lock(keyholder="x")], PairOutcome.NOT_MANAGED)])
    def test_only_a_locked_lock_held_by_the_bot_can_be_paired(self, e, locks, outcome) -> None:
        e.chaster.keyholder_locks = locks
        assert e.pairing().pair(e.tenant.id, "lock-1", actor="owner").outcome is outcome and e.reg.get(e.tenant.id).lock_id is None

    def test_refusals(self, e) -> None:
        assert e.pairing(scopes=("locks",)).pair(e.tenant.id, "lock-1", actor="o").outcome is PairOutcome.UNAVAILABLE
        assert e.pairing().pair("tenant_0", "lock-1", actor="o").outcome is PairOutcome.NOT_A_TENANT
        bo = e.w.wearer("Bo", "3000003")
        e.pairing().pair(e.tenant.id, "lock-1", actor="o")
        assert e.pairing().pair(bo.id, "lock-1", actor="o").outcome is PairOutcome.ALREADY_PAIRED_ELSEWHERE


class TestWearerCommands:
    def _g(self, tmp_path):
        g = G(tmp_path)
        tenant = g.member()
        chaster = Chaster()
        g.gateway._pairing = LockPairingService(registry=g.registry, lock_client=chaster, keyholder_tokens=Tokens(),
                                                keyholder_connections=kh_connections(), keyholder_user_id="bot", clock=g.w.clock)
        return g, tenant, chaster

    def test_lock_confirm_and_reject(self, tmp_path) -> None:
        g, tenant, chaster = self._g(tmp_path)
        assert g.say("2000002", "lock").text == TEXTS["no_lock"]
        g.gateway._pairing.pair(tenant.id, "lock-1", actor="owner")
        shown = g.say("2000002", "lock").text
        assert "Weekend" in shown and "alex_c" in shown and "confirm lock" in shown
        assert g.say("2000002", "confirm lock").text == TEXTS["lock_confirmed"] and g.registry.get(tenant.id).lock_ready
        assert g.say("2000002", "confirm lock").text == TEXTS["nothing_to_confirm"]
        assert g.say("2000002", "reject lock").text == TEXTS["lock_rejected"] and g.registry.get(tenant.id).lock_id is None

    def test_confirm_is_refused_when_the_lock_is_not_held_right_now(self, tmp_path) -> None:
        g, tenant, chaster = self._g(tmp_path)
        g.gateway._pairing.pair(tenant.id, "lock-1", actor="owner")
        chaster.keyholder_locks = []
        assert g.say("2000002", "confirm lock").text == TEXTS["lock_not_managed"] and not g.registry.get(tenant.id).lock_ready
        chaster.search_fails = True
        assert g.say("2000002", "confirm lock").text == TEXTS["lock_unreachable"]

    def test_the_owner_has_no_lock_commands(self, tmp_path) -> None:
        g, _, _ = self._g(tmp_path)
        g.say("1000001", "confirm lock")
        assert g.zero_calls == ["confirm lock"]                                          # plain text for the owner's own system


class TestCli:
    def test_pair_and_unpair_are_owner_only(self, e) -> None:
        admin = TenantAdmin(registry=e.reg, halt=e.w.platform.halt, owner_user_id=OWNER, clock=e.w.clock)

        def cli(*args):
            out = io.StringIO()
            code = main(["tenants", *args], out=out, tenants_factory=lambda: (admin, e.w.platform), now=lambda: NOW,
                        pairing_factory=lambda services: e.pairing())
            return code, out.getvalue()

        assert cli("pair", e.tenant.id, "lock-1", "--owner-id", "mallory")[0] == 3 and e.reg.get(e.tenant.id).lock_id is None
        code, text = cli("pair", e.tenant.id, "lock-1", "--owner-id", OWNER)
        assert code == 0 and "confirm lock" in text and "alex_c" in text
        assert cli("unpair", e.tenant.id, "--owner-id", OWNER)[0] == 0 and e.reg.get(e.tenant.id).lock_id is None
        e.chaster.keyholder_locks = []
        assert cli("pair", e.tenant.id, "lock-1", "--owner-id", OWNER)[0] == 3
