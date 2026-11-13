"""
cli/economy.py

`python -m cli economy` (owner, 2026-11-13; roadmap slice 4): how fast the wearer earns tokens, read-only. For each window (7, 14 and 30
local days): days evaluated, tokens earned, the average daily progress, the days per token that pace means at the current payout threshold,
and what held progress back (days zeroed by a confirmed incident, check-ins missed, goals missed, vacation days). Nothing is written and nothing
is shown to the wearer; it is the basis for the review on 2026-11-27 (a change to earning rates is proposed there, never made here).

Note: an incident counts on the day it is CONFIRMED (after the question, the judge and the appeal window), which can be the day after the act.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

__all__ = ["EconomyWindow", "economy_report", "render_economy"]

WINDOWS = (7, 14, 30)


@dataclass(frozen=True)
class EconomyWindow:
    days: int
    evaluated: int
    paused: int
    tokens: int
    progress: Decimal
    zeroed_by_incident: int
    check_ins_ok: int
    check_ins_due: int
    goals_met: int
    goals_assigned: int

    @property
    def per_day(self) -> Decimal:
        active = self.evaluated - self.paused
        return self.progress / active if active > 0 else Decimal(0)


def economy_report(core, user_id: str, today: date) -> tuple[list[EconomyWindow], Decimal, Decimal]:
    """(windows, current pool, current payout threshold). Reads `daily_evaluations`, `token_pool_state` and the conditioning threshold."""
    from release_engine.conditioning import ProgressiveConditioning
    with core.transaction() as tx:
        rows = [dict(r) for r in tx.fetch_all("SELECT * FROM daily_evaluations WHERE user_id = ? AND local_date >= ? ORDER BY local_date",
                                              (user_id, (today - timedelta(days=max(WINDOWS))).isoformat()))]
        pool_row = tx.fetch_one("SELECT pool FROM token_pool_state WHERE user_id = ?", (user_id,))
        threshold = ProgressiveConditioning(core.db_path, core=core).threshold_in(tx, user_id)
    windows = []
    for days in WINDOWS:
        since = (today - timedelta(days=days)).isoformat()
        part = [r for r in rows if r["local_date"] >= since]
        windows.append(EconomyWindow(
            days=days, evaluated=len(part), paused=sum(1 for r in part if r.get("paused")), tokens=sum(int(r["tokens_earned"] or 0) for r in part),
            progress=sum((Decimal(str(r["progress"] or 0)) for r in part), Decimal(0)),
            zeroed_by_incident=sum(1 for r in part if not r.get("paused") and Decimal(str(r["progress"] or 0)) == 0 and (r["check_ins_ok"] or r["goals_met"])),
            check_ins_ok=sum(r["check_ins_ok"] or 0 for r in part), check_ins_due=sum(r["check_ins_due"] or 0 for r in part),
            goals_met=sum(r["goals_met"] or 0 for r in part), goals_assigned=sum(r["goals_assigned"] or 0 for r in part)))
    return windows, Decimal(pool_row["pool"]) if pool_row else Decimal(0), Decimal(str(threshold))


def render_economy(windows: list[EconomyWindow], pool: Decimal, threshold: Decimal) -> str:
    lines = [f"Token pool now: {pool:.3f} of {threshold:.3f} for the next token.", ""]
    for w in windows:
        per_day = w.per_day
        pace = f"{(threshold / per_day):.1f} days per token" if per_day > 0 else "no progress, so no token at this pace"
        lines.append(f"Last {w.days} days: {w.evaluated} evaluated ({w.paused} on vacation), {w.tokens} token(s) earned, "
                     f"average progress {per_day:.3f} a day -> {pace}.")
        lines.append(f"  Held back by: {w.zeroed_by_incident} day(s) zeroed by a confirmed incident; check-ins {w.check_ins_ok}/{w.check_ins_due} "
                     f"answered; goals {w.goals_met}/{w.goals_assigned} met.")
    lines.append("")
    lines.append("Design pace: about 2 tokens a week at 100 % and Trust 1.0 (3.5 days per token at threshold 1.0).")
    return "\n".join(lines)
