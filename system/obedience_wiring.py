"""
system/obedience_wiring.py

Wires the daily obedience tasks (owner, 2026-11-14; work queue Q4b) for the owner's own database: the spot checks' Chaster tasks port and lock
(one lock, one task slot; a spot check goes first), the wearer's free time and local date, and the infractions path for a missed or rejected
task (OBEDIENCE_TASK_MISSED, warning only). None, with a note, when the spot checks or the detector questions are not active.
"""

from __future__ import annotations

from datetime import datetime

from infractions.signals import SignalKind
from obedience.service import ObedienceTasks, Ports
from schedule_windows.windows import WindowKind, classify

__all__ = ["build_obedience", "build_task_board"]

_REASONS = {"missed": "no photo arrived by the deadline", "late": "the photo came after the deadline",
            "rejected": "the community rejected the photo"}


def build_task_board(core, obedience: ObedienceTasks, window_cfg):
    """P1, the task board (owner, 2026-11-15), on top of the obedience tasks: the same wearer, free time, gear, Chaster path and miss path."""
    from datetime import time, timedelta
    from obedience.service import ALONE, effective, get_obedience_state, plan_board_task
    from task_board.service import EROTIC_DAYS, BoardTask, TaskBoard, erotic_reward
    p = obedience._ports

    def day_end(d):
        """The last free-time moment of local day `d` (a board deadline)."""
        start, at = window_cfg.to_utc(d, time(0, 0)), window_cfg.to_utc(d, time(23, 45))
        while at > start and not p.free_time(at):
            at -= timedelta(minutes=15)
        return at

    def erotic_pick(day):
        """One task from the obedience catalogue, the full (alone) version; never one whose instructions live only in the DM (the gag)."""
        for _ in range(8):
            task = obedience._pick(day, ALONE)
            if task.kind == "erotic" and not task.dm_note:
                return BoardTask(f"e:{task.key}", task.text, erotic_reward(task.key, task.needs), EROTIC_DAYS, "erotic", task.needs)
        return None

    training = getattr(obedience, "training", None)
    return TaskBoard(core, user_id=obedience.user_id, erotic_pick=erotic_pick,
                     training_today=(lambda now: training.is_training_day_now(now)) if training is not None else (lambda now: False), owned=lambda: effective(obedience.owned_gear()), free_time=p.free_time, today=p.today,
                     day_end=day_end, local_clock=p.local_clock, paused=p.paused,
                     start_task=lambda row, now: plan_board_task(core, obedience.user_id, p.today(now), row["task_key"], row["text"], now),
                     task_state=lambda ident: get_obedience_state(core, ident),
                     raise_missed=lambda row, now: p.raise_failure(row, "missed", now))


def build_obedience(config, core, *, proof_checks, signals, window_cfg, notes: list, exemptions=None, features=None) -> ObedienceTasks | None:
    if proof_checks is None or signals is None:
        notes.append("Daily obedience tasks are NOT active: they need the spot checks' Chaster task setup and the detector questions.")
        return None
    p = proof_checks.ports
    user_id = proof_checks.user_id
    domain = getattr(config, "check_in_trust_domain", None) or "routine"

    def free_time(at: datetime) -> bool:
        return classify(window_cfg, at) is WindowKind.DISCRETIONARY

    def raise_failure(row: dict, reason: str, now: datetime) -> None:
        why = _REASONS.get(reason, reason)
        signals.raise_signal(user_id=user_id, kind=SignalKind.OBEDIENCE_MISSED, subject_id=row["id"], trust_domain=domain, now=now,
                             detail=f"The daily obedience task failed: {why}.",
                             wearer_text=f"Today's task failed: {why}. What happened?",
                             facts=f"Task: {row['text']} Assigned; {reason}.",
                             window=(datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")), now))

    from training.service import TrainingPlan
    plan = TrainingPlan(core, user_id=user_id, free_time=free_time, today=p.today)
    tasks = ObedienceTasks(core, user_id=user_id, chaster=proof_checks.chaster,
                           ports=Ports(lock_id=p.lock_id, halted=p.halted, paused=p.paused, free_time=free_time, today=p.today,
                                       local_clock=p.local_clock, slot_taken=lambda: proof_checks.get_open_check() is not None,
                                       raise_failure=raise_failure,
                                       in_exemption=(lambda: False) if exemptions is None else (lambda: exemptions.wear_exemption_in_force(user_id)),
                                       training=plan.day_task, training_tick=plan.tick,
                                       feature_on=(lambda f: True) if features is None else (lambda f: features.is_on(user_id, f))))
    tasks.training = plan                                     # work queue Q4c: the bot wires its `log` and its Monday buttons
    plan.after_today = tasks.take_training_today              # bug fix 2026-11-16: "Today" switches a task that is only planned
    plan.today_open = tasks.today_open                        # `extra training` only while today's task is not out yet
    notes.append("Daily obedience tasks are active: one a day at a random moment in free time, shown in Chaster.")
    return tasks
