"""
system/tenant_loops.py

Staggered periodic loops for the active tenants (v0.39 Phase E2). One supervisor reconciles the registry with the running loops:

- a tenant that is ACTIVE gets a runtime (built on a worker thread) and ONE loop; a suspended, removed or failed tenant has its loop stopped and its runtime dropped;
- loop start offsets are spread evenly over the interval (`interval * index / count`), so N tenants never hit Chaster in the same second;
- before every cycle the loop re-checks the registry: the moment a tenant stops being managed (the wearer leaves, the keyholder suspends or removes) its next
  cycle does not run and the loop ends. A runtime that cannot be built is retried after a back-off and never affects the other tenants;
- one tenant's exception stays inside its own loop.

Tenant zero is not handled here (it keeps the existing loop).
"""

from __future__ import annotations

import asyncio
import logging

from system.tenant_factory import TenantFactory, TenantRuntime
from tenancy.registry import TenantRegistry

logger = logging.getLogger("ai_coach_keyholder.system.tenant_loops")

__all__ = ["TenantLoopSupervisor", "stagger_offsets"]

BUILD_RETRY_SECONDS = 300


def stagger_offsets(count: int, interval_seconds: float) -> list[float]:
    """Start offsets that spread `count` loops evenly over one interval."""
    if count < 1:
        return []
    return [interval_seconds * i / count for i in range(count)]


class TenantLoopSupervisor:
    def __init__(self, *, registry: TenantRegistry, factory: TenantFactory, interval_seconds: float = 60.0,
                 reconcile_seconds: float = 30.0) -> None:
        if interval_seconds <= 0 or reconcile_seconds <= 0:
            raise ValueError("intervals must be positive")
        self._registry = registry
        self._factory = factory
        self._interval = float(interval_seconds)
        self._reconcile_seconds = float(reconcile_seconds)
        self._tasks: dict[str, asyncio.Task] = {}
        self.runtimes: dict[str, TenantRuntime] = {}
        self._retry_at: dict[str, float] = {}

    async def run(self) -> None:
        try:
            while True:
                try:
                    await self.reconcile()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Reconciling the tenant loops failed; will retry.")
                await asyncio.sleep(self._reconcile_seconds)
        finally:
            await self.stop_all()

    async def reconcile(self) -> None:
        """Brings the running loops in line with the registry (callable on its own; the supervisor also calls it periodically)."""
        desired = [t for t in self._registry.managed_tenants() if not t.is_tenant_zero]
        wanted_ids = {t.id for t in desired}
        for tenant_id in [i for i in self._tasks if i not in wanted_ids or self._tasks[i].done()]:
            await self._stop(tenant_id)
        new = [t for t in desired if t.id not in self._tasks]
        loop_time = asyncio.get_running_loop().time()
        offsets = stagger_offsets(len(new), self._interval)
        for tenant, offset in zip(sorted(new, key=lambda t: t.id), offsets):
            if self._retry_at.get(tenant.id, 0.0) > loop_time:
                continue
            try:
                runtime = await asyncio.to_thread(self._factory.build, tenant.id)
            except Exception:
                logger.exception("Tenant %s could not be started; retrying later.", tenant.id)
                self._retry_at[tenant.id] = loop_time + BUILD_RETRY_SECONDS
                continue
            self.runtimes[tenant.id] = runtime
            self._tasks[tenant.id] = asyncio.create_task(self._loop(runtime, offset), name=f"tenant-{tenant.id}")

    async def stop_all(self) -> None:
        for tenant_id in list(self._tasks):
            await self._stop(tenant_id)

    @property
    def running(self) -> list[str]:
        return sorted(i for i, t in self._tasks.items() if not t.done())

    # ------------------------------------------------------------------ internals

    async def _loop(self, runtime: TenantRuntime, offset: float) -> None:
        await asyncio.sleep(offset)
        while True:
            if not self._registry.is_managed(runtime.tenant_id):         # immediate disable: no further cycle for this tenant
                logger.info("Tenant %s is no longer managed; its loop ends.", runtime.tenant_id)
                return
            try:
                await asyncio.to_thread(runtime.cycle)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("A cycle of tenant %s failed; will retry next interval.", runtime.tenant_id)
            await asyncio.sleep(self._interval)

    async def _stop(self, tenant_id: str) -> None:
        task = self._tasks.pop(tenant_id, None)
        self.runtimes.pop(tenant_id, None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
