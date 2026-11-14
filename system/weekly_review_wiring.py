"""
system/weekly_review_wiring.py

Wires the weekly review (roadmap slice 5) to its readers: the coach desk's commitments, the spot checks and the daily evaluations. The single
wearer only, like the coach it reports on; tenants are not wired.
"""

from __future__ import annotations

from weekly_review.service import Ports, WeeklyReview

__all__ = ["build_weekly_review"]


def build_weekly_review(core, *, coach_dm, proof_checks, vacation, window_cfg, notes: list) -> WeeklyReview | None:
    from system.daily_evaluation import get_daily_evaluations
    if coach_dm is None:
        notes.append("The weekly review is NOT active: it reports on the coach's commitments, and the coach is not active.")
        return None
    desk = coach_dm.desk
    user_id = desk.user_id
    ports = Ports(local=window_cfg.local, paused=lambda now: vacation.is_paused(user_id, now),
                  commitments=desk.get_commitments_between,
                  evaluations=lambda start, end: get_daily_evaluations(core, user_id, start, end),
                  checks=None if proof_checks is None else proof_checks.get_checks_between)
    notes.append("The weekly review is active (Sunday 19:00 local).")
    return WeeklyReview(core, user_id=user_id, ports=ports)
