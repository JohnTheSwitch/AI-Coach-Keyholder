"""Multi-wearer parity 3 (owner, 2026-11-13): an additional wearer's own notification queue, delivered only to that wearer, in their own quiet
hours, with penalty notices, check-in warnings and release notices going through it. No network, no Discord."""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import datetime, timezone
from types import SimpleNamespace

from notifications.queue import DeliveryResult, NotificationQueue
from system.tenant_factory import TenantFactory
from system.tenant_loops import TenantLoopSupervisor
from tests.system.test_tenant_factory import NOW, World, w  # noqa: F401


def with_delivery(w, delivered: list, calls: list):  # noqa: F811
    def factory(wearer_id, discord_id):
        calls.append((wearer_id, discord_id))

        async def deliver(notification):
            delivered.append((discord_id, notification.user_id, notification.text))
            return DeliveryResult.DELIVERED
        return deliver

    shared = dataclasses.replace(w.shared, notification_delivery_factory=factory)
    return TenantFactory(registry=w.platform.registry, shared=shared, tenant_dir=w.platform.tenant_dir)


def test_without_the_factory_a_tenant_has_no_queue(w) -> None:  # noqa: F811
    runtime = w.factory.build(w.wearer("Alex", "2000002").id)
    assert runtime.notifications is None and runtime.penalty_notices is None


def test_each_tenant_gets_its_own_queue_delivered_only_to_its_wearer(w) -> None:  # noqa: F811
    delivered, calls = [], []
    factory = with_delivery(w, delivered, calls)
    a, b = factory.build(w.wearer("Alex", "2000002").id), factory.build(w.wearer("Bo", "3000003").id)
    assert sorted(calls) == sorted([(a.wearer_user_id, "2000002"), (b.wearer_user_id, "3000003")])
    NotificationQueue(a.db_path, core=a.core).enqueue(user_id=a.wearer_user_id, kind="test", dedupe_key="k1", text="for Alex", now=NOW,
                                                      immediate=True)
    assert asyncio.run(a.notifications.run_once(NOW)) == 1 and asyncio.run(b.notifications.run_once(NOW)) == 0
    assert delivered == [("2000002", a.wearer_user_id, "for Alex")]
    assert set(a.penalty_notices) >= {"penalty_window.started", "penalty_warning.recorded"}


def test_routine_notices_wait_for_the_wearers_own_quiet_hours(w) -> None:  # noqa: F811
    delivered = []
    factory = with_delivery(w, delivered, [])
    runtime = factory.build(w.wearer("Alex", "2000002").id)
    night = datetime(2026, 10, 12, 23, 30, tzinfo=timezone.utc)               # the owner's default quiet hours, UTC here
    NotificationQueue(runtime.db_path, core=runtime.core).enqueue(user_id=runtime.wearer_user_id, kind="test", dedupe_key="k", text="later",
                                                                  now=night)
    assert asyncio.run(runtime.notifications.run_once(night)) == 0 and delivered == []


def test_the_bot_delivery_reaches_only_the_tenants_wearer() -> None:
    from bot import discord_bot
    source = open(discord_bot.__file__, encoding="utf-8").read()
    assert "discord_id_for=lambda user_id, w=wearer_id, d=discord_id: d if user_id == w else None" in source


class _Runtime:
    def __init__(self, tenant_id) -> None:
        self.tenant_id, self.cycles, self.passes = tenant_id, 0, 0
        self.clock = SimpleNamespace(now=lambda: NOW)
        runtime = self

        class _Dispatcher:
            async def run_once(self, now):
                runtime.passes += 1
                return 0
        self.notifications = _Dispatcher()

    def cycle(self):
        self.cycles += 1
        return True


def test_the_loop_delivers_once_per_cycle_and_stops_with_the_tenant(w) -> None:  # noqa: F811
    tenant = w.wearer("Alex", "2000002")
    factory = SimpleNamespace(build=lambda tenant_id: _Runtime(tenant_id))
    sup = TenantLoopSupervisor(registry=w.platform.registry, factory=factory, interval_seconds=0.05, reconcile_seconds=0.05)

    async def go():
        await sup.reconcile()
        runtime = sup.runtimes[tenant.id]
        await asyncio.sleep(0.12)
        w.platform.registry.remove(tenant.id, actor="o", now=NOW)
        passes = runtime.passes
        await asyncio.sleep(0.15)
        await sup.stop_all()
        return passes, runtime.passes, runtime.cycles

    passes, after, cycles = asyncio.run(go())
    assert passes >= 2 and after <= passes + 1 and abs(cycles - after) <= 1
