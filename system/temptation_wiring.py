"""
system/temptation_wiring.py

Builds the temptations (owner, 2026-11-16; P5, queue B7) for the owner, and for another wearer with their own approved texts
(`offer_text`, owner 2026-11-17).
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal


def build_temptations(core, *, user_id, window_cfg, blocked, offer_text=None):
    from schedule_windows.windows import WindowKind, classify
    from system.daily_evaluation import get_daily_evaluations
    from temptations.service import Ports, Temptations

    def avg_daily():
        from infrastructure.clock import SystemClock
        today = window_cfg.local(SystemClock().now()).date()
        rows = [r for r in get_daily_evaluations(core, user_id, today - timedelta(days=14), today - timedelta(days=1)) if not r.get("paused")]
        if not rows:
            return None
        return sum((Decimal(str(r.get("progress") or 0)) for r in rows), Decimal(0)) / len(rows)

    return Temptations(core, user_id=user_id, ports=Ports(
        today=lambda now: window_cfg.local(now).date(),
        free_time=lambda now: classify(window_cfg, now) is WindowKind.DISCRETIONARY,
        blocked=blocked, avg_daily=avg_daily, local_clock=lambda t: window_cfg.local(t).strftime("%H:%M"), offer_text=offer_text))
