"""
system/periodic.py

The periodic cycle (approved: every ~60 seconds), guarded by the same
single-instance lease as startup so two processes never run it at once:

    drain the outbox -> Rules Engine evaluates pending triggers -> executor pass -> behavioral telemetry (Phase G1)

A failure in any step is logged and never raises out of the loop.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from infrastructure.clock import Clock
from infrastructure.consumer_registry import process_pending_events
from infrastructure.database import Database as CoreDatabase
from infrastructure.startup_lease import acquire_system_startup_lease, release_system_startup_lease
from penalty_engine.repository import PenaltyEngine
from recovery_plan.repository import RecoveryPlanManager
from system.startup import build_consumer_registry
from system.penalty_policy import make_penalty_engine
from trust_manager.repository import TrustManager

logger = logging.getLogger("ai_coach_keyholder.system.periodic")

__all__ = ["run_periodic_cycle", "periodic_loop", "CYCLE_LEASE_DURATION"]

CYCLE_LEASE_DURATION = timedelta(minutes=2)


def run_periodic_cycle(core: CoreDatabase, process_id: str, clock: Clock, *, rules_engine, executor, supervision=None,
                       release_task=None, check_ins=None, daily=None, telemetry=None) -> bool:
    """Returns False when another instance holds the lease (nothing was done)."""
    now = clock.now()
    lease = acquire_system_startup_lease(core, process_id, now, CYCLE_LEASE_DURATION)
    if lease is None:
        return False
    try:
        registry = build_consumer_registry(
            TrustManager(core.db_path, core=core), make_penalty_engine(core),
            RecoveryPlanManager(core.db_path, core=core), rules_engine, supervision,
            release_engine=None if release_task is None else release_task.engine,
            release_user_id=None if release_task is None else release_task.user_id,
            conditioning=getattr(release_task, "conditioning", None),
        )
        process_pending_events(core, registry, claimant=process_id, now=now)
        if rules_engine is not None:
            rules_engine.evaluate_pending(now=now)
        if check_ins is not None:
            check_ins.tick(now)                      # first: the release gates read the latest check-in outcome
        if daily is not None:
            daily.run(now)                           # before the release tick, so a new token is seen at once
        if release_task is not None:
            release_task.run(now)
        if executor is not None:
            executor.run_once(now=now)
        if release_task is not None:
            release_task.sync(now)                   # reflect the execution result in the same cycle
    except Exception:
        logger.exception("Periodic cycle failed; will retry next interval.")
    try:
        if telemetry is not None:
            telemetry.run(now)                       # last and on its own: derived records only, never affects the steps above
    except Exception:
        logger.exception("Behavioral telemetry collection failed; will retry next interval.")
    finally:
        release_system_startup_lease(core, lease)
    return True


async def periodic_loop(core: CoreDatabase, process_id: str, clock: Clock, *, rules_engine, executor,
                        interval_seconds: int = 60, supervision=None,
                        release_task=None, check_ins=None, daily=None, telemetry=None) -> None:
    while True:
        try:
            await asyncio.to_thread(
                run_periodic_cycle, core, process_id, clock, rules_engine=rules_engine, executor=executor,
                supervision=supervision, release_task=release_task, check_ins=check_ins, daily=daily, telemetry=telemetry,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Periodic loop iteration failed.")
        await asyncio.sleep(interval_seconds)
