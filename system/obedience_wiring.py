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

__all__ = ["build_obedience"]

_REASONS = {"missed": "no photo arrived by the deadline", "late": "the photo came after the deadline",
            "rejected": "the community rejected the photo"}


def build_obedience(config, core, *, proof_checks, signals, window_cfg, notes: list, exemptions=None) -> ObedienceTasks | None:
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

    tasks = ObedienceTasks(core, user_id=user_id, chaster=proof_checks.chaster,
                           ports=Ports(lock_id=p.lock_id, halted=p.halted, paused=p.paused, free_time=free_time, today=p.today,
                                       local_clock=p.local_clock, slot_taken=lambda: proof_checks.get_open_check() is not None,
                                       raise_failure=raise_failure,
                                       in_exemption=(lambda: False) if exemptions is None else (lambda: exemptions.wear_exemption_in_force(user_id))))
    notes.append("Daily obedience tasks are active: one a day at a random moment in free time, shown in Chaster.")
    return tasks
