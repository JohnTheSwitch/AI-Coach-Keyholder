"""
system/recovery_wiring.py

Wires the verified recovery tasks (owner, 2026-11-14; brat taming B3) for the owner's own database: the same Chaster tasks port and lock as the
spot checks (one lock, one task slot), the Penalty Engine's active window, the Recovery Plan, and the community review watch (a recovery task's
review is decided here, never as a rejected verification photo). None, with a note, when the spot checks are not active.
"""

from __future__ import annotations

import asyncio
import logging

from penalty_engine.models import PenaltyWindowStatus
from recovery_plan.repository import RecoveryPlanManager
from recovery_tasks.service import Ports, RecoveryTasks
from system.penalty_policy import make_penalty_engine

logger = logging.getLogger("ai_coach_keyholder.system.recovery_wiring")

__all__ = ["build_recovery_tasks", "recovery_loop", "recovery_tick"]


def build_recovery_tasks(core, *, proof_checks, window_cfg, notes: list) -> RecoveryTasks | None:
    if proof_checks is None:
        notes.append("Verified recovery tasks are NOT active: they use the spot checks' Chaster task setup, which is not active.")
        return None
    plans = RecoveryPlanManager(core.db_path, core=core)
    penalties = make_penalty_engine(core)
    p = proof_checks.ports

    def active_window() -> str | None:
        window = penalties.get_active_or_frozen_penalty_window()
        return window.id if window is not None and window.status is PenaltyWindowStatus.ACTIVE else None

    def plan_for(window_id: str) -> str | None:
        plan = plans.get_recovery_plan_for_window(window_id)
        return None if plan is None else plan.id

    tasks = RecoveryTasks(core, user_id=proof_checks.user_id, chaster=proof_checks.chaster, plans=plans,
                          ports=Ports(lock_id=p.lock_id, halted=p.halted, paused=p.paused, quiet_between=p.quiet_between, active_window=active_window,
                                      plan_for=plan_for, slot_taken=lambda: proof_checks.get_open_check() is not None, local_clock=p.local_clock))
    notes.append("Verified recovery tasks are active: during a Penalty Window, a chore shown in Chaster earns 2 hours off it.")
    return tasks


def recovery_tick(tasks: RecoveryTasks, now, enabled=lambda: True) -> None:
    """One pass: an open task is always followed to its end; a new one is offered only while the wearer's `recovery tasks` switch is on."""
    tasks.poll(now)
    if enabled():
        tasks.offer(now)


async def recovery_loop(tasks: RecoveryTasks, clock, *, interval_seconds: float = 120.0, enabled=lambda: True) -> None:
    while True:
        try:
            await asyncio.to_thread(recovery_tick, tasks, clock.now(), enabled)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("The recovery-task loop failed a pass; it continues.")
        await asyncio.sleep(interval_seconds)
