"""v0.39 Phase E5: the shared keyholder API budget and per-tenant Chaster chat, with no cross-talk between tenants. No network."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from chaster.api_budget import ApiBudget, ApiBudgetExceeded, BudgetedTokenManager, Lane
from chaster.lock_client import ChasterLockApiError
from messaging.models import ChannelType
from messaging.routes import MessageRouteRepository
from system.tenant_factory import TenantFactory
from tests.system.test_managed_locks_e4 import BOT, Chaster, Tokens, World4, kh_connections, lock
from tests.system.test_tenant_factory import NOW, World


def run(coro):
    return asyncio.run(coro)


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class TestBudget:
    def test_background_is_refused_when_the_window_is_full_and_recovers_as_it_slides(self) -> None:
        clock = Clock()
        budget = ApiBudget(requests_per_minute=3, monotonic=clock)
        assert [budget.try_acquire() for _ in range(4)] == [True, True, True, False]
        assert budget.refused == 1 and budget.used == 3
        clock.t += 30
        assert budget.try_acquire() is False
        clock.t += 31
        assert budget.try_acquire() is True and budget.used == 1

    def test_essential_is_never_refused_but_counted(self) -> None:
        budget = ApiBudget(requests_per_minute=2, monotonic=Clock())
        assert budget.try_acquire(Lane.BACKGROUND) and budget.try_acquire(Lane.BACKGROUND)
        assert all(budget.try_acquire(Lane.ESSENTIAL) for _ in range(5)) and budget.used == 7
        assert budget.try_acquire(Lane.BACKGROUND) is False

    def test_essential_traffic_makes_background_back_off(self) -> None:
        budget = ApiBudget(requests_per_minute=2, monotonic=Clock())
        budget.try_acquire(Lane.ESSENTIAL)
        budget.try_acquire(Lane.ESSENTIAL)
        assert budget.try_acquire(Lane.BACKGROUND) is False

    @pytest.mark.parametrize("bad", [0, -1, True, 2.5])
    def test_invalid_limits(self, bad) -> None:
        with pytest.raises(ValueError):
            ApiBudget(requests_per_minute=bad)

    def test_the_budget_is_safe_across_threads(self) -> None:
        import threading
        budget = ApiBudget(requests_per_minute=50, monotonic=Clock())
        results: list[bool] = []
        lock_ = threading.Lock()

        def worker():
            for _ in range(20):
                ok = budget.try_acquire()
                with lock_:
                    results.append(ok)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert results.count(True) == 50 and len(results) == 160


class TestBudgetedTokens:
    def _manager(self, limit=1, lane=Lane.BACKGROUND):
        calls = []

        class Inner:
            def call_with_token(self, user_id, *, now, operation):
                calls.append(user_id)
                return operation("tok")

            def some_other_method(self):
                return "passthrough"

        budget = ApiBudget(requests_per_minute=limit, monotonic=Clock())
        return BudgetedTokenManager(Inner(), budget, account_user_id="bot", lane=lane), calls, budget

    def test_the_keyholder_account_is_charged_and_refused_when_empty(self) -> None:
        tokens, calls, _ = self._manager(limit=1)
        assert tokens.call_with_token("bot", now=NOW, operation=lambda t: t) == "tok"
        with pytest.raises(ApiBudgetExceeded) as err:
            tokens.call_with_token("bot", now=NOW, operation=lambda t: t)
        assert isinstance(err.value, ChasterLockApiError) and err.value.status_code is None and calls == ["bot"]

    def test_other_accounts_are_not_budgeted(self) -> None:
        tokens, calls, budget = self._manager(limit=1)
        for _ in range(5):
            tokens.call_with_token("wearer", now=NOW, operation=lambda t: t)
        assert calls == ["wearer"] * 5 and budget.used == 0

    def test_the_essential_lane_never_raises(self) -> None:
        tokens, calls, _ = self._manager(limit=1, lane=Lane.ESSENTIAL)
        for _ in range(3):
            tokens.call_with_token("bot", now=NOW, operation=lambda t: t)
        assert calls == ["bot"] * 3

    def test_other_methods_pass_through(self) -> None:
        tokens, _, _ = self._manager()
        assert tokens.some_other_method() == "passthrough"


class TestBudgetProtectsTheKeyholder:
    def test_an_exhausted_budget_makes_observation_go_stale_not_wrong(self, tmp_path) -> None:
        e = World4(tmp_path)
        e.pair()
        budget = ApiBudget(requests_per_minute=1, monotonic=Clock())
        src = e.source()
        src._kh_tokens = BudgetedTokenManager(Tokens(), budget, account_user_id="bot", lane=Lane.BACKGROUND)
        assert src.refresh(NOW) == "observed"
        assert src.refresh(NOW + timedelta(minutes=1)) is None and len(e.rows()) == 1          # refused: nothing recorded
        assert src.get_latest().fetched_at == NOW

    def test_one_budget_is_shared_by_all_tenants(self, tmp_path) -> None:
        e = World4(tmp_path)
        bo = e.w.wearer("Bo", "3000003")
        rb = e.w.factory.build(bo.id)
        e.chaster.keyholder_locks = [lock("lock-1"), lock("lock-2", wearer="bo-acc")]
        e.pair("lock-1")
        e.pair("lock-2", "bo-acc", tenant_id=bo.id)
        budget = ApiBudget(requests_per_minute=1, monotonic=Clock())
        shared = BudgetedTokenManager(Tokens(), budget, account_user_id="bot", lane=Lane.BACKGROUND)
        sa, sb = e.source(), e.source(tenant_id=bo.id, runtime=rb)
        sa._kh_tokens = sb._kh_tokens = shared
        assert sa.refresh(NOW) == "observed" and sb.refresh(NOW) is None


class Conversations:
    """Fake Chaster chat: one conversation per Chaster account; every call is recorded."""

    def __init__(self) -> None:
        self.pages: dict[str, list[dict]] = {}
        self.sent: list[tuple[str, str]] = []
        self.polled: list[str] = []

    def find_conversation_by_user(self, *, access_token, chaster_user_id):
        return {"_id": f"conv-{chaster_user_id}"}

    def list_messages(self, *, access_token, conversation_id, limit=50):
        self.polled.append(conversation_id)
        return list(self.pages.get(conversation_id, []))

    def send_message(self, *, access_token, conversation_id, message):
        self.sent.append((conversation_id, message))
        return {"_id": "out"}


def msg(mid, sender, text, minutes=1):
    return {"_id": mid, "user": sender, "message": text, "type": "message",
            "createdAt": (NOW + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")}


class ChatWorld:
    def __init__(self, tmp_path) -> None:
        self.w = World(tmp_path)
        self.conv = Conversations()
        self.w.config = replace(self.w.config, chaster_execution_keyholder_user_id="bot")
        self.w.shared = replace(self.w.shared, config=self.w.config, lock_client=Chaster(), keyholder_token_manager=Tokens(),
                                keyholder_connections=kh_connections(("locks", "keyholder", "messaging")), chat_enabled=True,
                                conversation_client=self.conv)
        self.factory = TenantFactory(registry=self.w.platform.registry, shared=self.w.shared, tenant_dir=self.w.platform.tenant_dir)
        self.reg = self.w.platform.registry
        self.a = self._tenant("Alex", "2000002", "lock-a", "acc-a")
        self.b = self._tenant("Bo", "3000003", "lock-b", "acc-b")

    def _tenant(self, name, discord, lock_id, account):
        t = self.w.wearer(name, discord)
        self.reg.pair_lock(t.id, lock_id=lock_id, chaster_account_id=account, actor="owner", now=NOW)
        self.reg.confirm_pairing(t.id, actor="w", now=NOW)
        return self.factory.build(t.id)

    def opt_in(self, runtime, account):
        MessageRouteRepository(runtime.db_path, core=runtime.core).set_route(
            user_id=runtime.wearer_user_id, channel=ChannelType.CHASTER, chaster_conversation_id=f"conv-{account}", now=NOW)


@pytest.fixture
def c(tmp_path):
    return ChatWorld(tmp_path)


class TestPerTenantChat:
    def test_each_tenant_gets_its_own_chat_stack_over_its_own_database(self, c) -> None:
        assert c.a.chat_poller is not None and c.b.chat_poller is not None and c.a.chat_poller is not c.b.chat_poller
        assert c.a.chat_poller.db_path == c.a.db_path and c.b.chat_poller.db_path == c.b.db_path

    def test_chat_is_off_unless_enabled(self, tmp_path) -> None:
        w = World(tmp_path)
        runtime = w.factory.build(w.wearer("Alex", "2000002").id)
        assert runtime.chat_poller is None and runtime.chat_adapter is None

    def test_a_tenant_polls_only_its_own_opted_in_conversation(self, c) -> None:
        c.opt_in(c.a, "acc-a")
        c.a.chat_poller.poll_once()
        c.b.chat_poller.poll_once()
        assert c.conv.polled == ["conv-acc-a"]                                              # B has not opted in: never polled

    def test_messages_never_cross_tenants(self, c) -> None:
        c.opt_in(c.a, "acc-a")
        c.opt_in(c.b, "acc-b")
        c.a.chat_poller.poll_once()
        c.b.chat_poller.poll_once()                                                        # both armed
        c.conv.pages = {"conv-acc-a": [msg("a1", "acc-a", "from Alex"), msg("x1", "acc-b", "Bo writing into Alex's chat")],
                        "conv-acc-b": [msg("b1", "acc-b", "from Bo")]}
        got_a = [(m.user_id, m.content) for m in c.a.chat_poller.poll_once()]
        got_b = [(m.user_id, m.content) for m in c.b.chat_poller.poll_once()]
        assert got_a == [(c.a.wearer_user_id, "from Alex")] and got_b == [(c.b.wearer_user_id, "from Bo")]
        with c.a.core.transaction() as tx:
            seen_a = {r["message_id"] for r in tx.fetch_all("SELECT message_id FROM chaster_chat_seen")}
        with c.b.core.transaction() as tx:
            seen_b = {r["message_id"] for r in tx.fetch_all("SELECT message_id FROM chaster_chat_seen")}
        assert seen_a == {"a1", "x1"} and seen_b == {"b1"}

    def test_an_unconfirmed_pairing_gives_no_chat_identity(self, c) -> None:
        c.opt_in(c.a, "acc-a")
        c.reg.pair_lock(c.a.tenant_id, lock_id="lock-a", chaster_account_id="acc-a", actor="owner", now=NOW)   # confirmation cleared
        c.a.chat_poller.poll_once()
        assert c.conv.polled == []                                                         # no confirmed identity: not polled

    def test_replies_go_to_the_tenants_own_conversation(self, c) -> None:
        for text in ("first", "english", "neutral", "alex"):
            c.w.say(c.a, "2000002", text)                                                  # onboarding done on Discord first
        c.opt_in(c.a, "acc-a")
        c.a.chat_poller.poll_once()
        c.conv.pages = {"conv-acc-a": [msg("a1", "acc-a", "hello")]}
        for m in c.a.chat_poller.poll_once():
            run(c.a.chat_adapter.handle_incoming_message(m))
        assert c.conv.sent == [("conv-acc-a", "a reply")]

    def test_opting_in_uses_the_confirmed_pairing_and_stays_in_the_tenant(self, c) -> None:
        service = c.a.application_service
        for text in ("first", "english", "neutral", "alex"):
            c.w.say(c.a, "2000002", text)
        reply = c.w.say(c.a, "2000002", "channel chaster")
        assert "Chaster chat" in reply
        assert MessageRouteRepository(c.a.db_path, core=c.a.core).get_route(c.a.wearer_user_id).chaster_conversation_id == "conv-acc-a"
        assert MessageRouteRepository(c.b.db_path, core=c.b.core).list_chaster_conversations() == []
        assert service is c.a.application_service

    def test_the_supervisor_polls_chat_after_each_cycle_and_never_for_unmanaged_tenants(self, c) -> None:
        from system.tenant_loops import TenantLoopSupervisor
        handled = []

        class Poller:
            def poll_once(self):
                return ["m"]

        class Adapter:
            async def handle_incoming_message(self, m):
                handled.append(m)

        runtime = SimpleNamespace(tenant_id=c.a.tenant_id, chat_poller=Poller(), chat_adapter=Adapter(), cycle=lambda: True)
        sup = TenantLoopSupervisor(registry=c.reg, factory=None, interval_seconds=60)
        run(sup._poll_chat(runtime))
        assert handled == ["m"]
        c.reg.suspend(c.a.tenant_id, actor="owner", now=NOW)
        run(sup._poll_chat(runtime))
        assert handled == ["m"]
