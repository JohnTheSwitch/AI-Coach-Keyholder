"""
weekly_review/service.py

The weekly review (owner, 2026-11-13; roadmap slice 5; migration 065). On Sunday from SEND_AT local the wearer gets the week in plain facts:
commitments kept / partly / not kept / reported outside their time, spot checks, check-ins answered and tokens earned. Written by code from
stored facts: no model, no Trust scores, nothing that judges. It goes through the notification queue (so never into quiet hours).

- The week is the local Monday to Sunday. Commitments and spot checks count for all seven days; check-ins and tokens come from the daily
  evaluations, which exist only for days that are over, so they cover Monday to Saturday and the message says so.
- A Sunday inside a vacation gets no review (recorded as `vacation`, so it is not sent later that evening).
- Each week is decided once (`weekly_reviews`). A missed Sunday (the bot was down) is not sent on Monday.
- `weekly review off` stops it; no settings row means on.

Reads other modules only through the readers it is given (`Ports`), which the composition layer wires to their owners' `get_*` functions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Callable

from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.outbox import DomainEvent, write_event
from infrastructure.time_format import iso as _iso
from notifications.queue import enqueue_in_transaction

logger = logging.getLogger("ai_coach_keyholder.weekly_review")

__all__ = ["WeeklyReview", "Ports", "WeekFacts", "render_week", "SEND_AT"]

SEND_AT = time(19, 0)                                     # owner, 2026-11-13 (roadmap slice 5): Sunday at 19:00 local
OFF_HINT = "`weekly review off` stops this message."
_STATUS_KEYS = {"kept": "kept", "partly": "partly", "not_kept": "not_kept", "lapsed": "unanswered", "open": "open"}
_CHECK_KEYS = {"passed": "checks_passed", "failed": "checks_failed", "declined": "checks_declined", "retracted": "checks_declined"}


@dataclass(frozen=True)
class Ports:
    local: Callable[[datetime], datetime]                                 # UTC -> the wearer's local time
    paused: Callable[[datetime], bool]                                    # inside a vacation
    commitments: Callable[[date, date], list[dict]]                       # rows with status, report_timing for for_date in [start, end]
    evaluations: Callable[[date, date], list[dict]]                       # daily evaluation rows for local_date in [start, end]
    checks: Callable[[date, date], list[dict]] | None = None              # spot checks (status) for local_date in [start, end]; None: not running


@dataclass(frozen=True)
class WeekFacts:
    start: date
    end: date
    kept: int = 0
    partly: int = 0
    not_kept: int = 0
    unanswered: int = 0                                   # lapsed: the report window passed without an answer
    open: int = 0
    outside_time: int = 0                                 # Kept or Partly reported too late or too early, so it did not count
    unproven: int = 0                                     # Kept whose spot check was not passed
    checks: int | None = None
    checks_passed: int = 0
    checks_failed: int = 0
    checks_declined: int = 0                              # "I can't show it now" or "I didn't actually do it"
    checks_waiting: int = 0
    evaluated: int = 0
    vacation_days: int = 0
    check_ins_ok: int = 0
    check_ins_due: int = 0
    tokens: int = 0

    @property
    def commitments(self) -> int:
        return self.kept + self.partly + self.not_kept + self.unanswered + self.open


def _day(d: date) -> str:
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b')}"


def _it(n: int) -> str:
    return "it" if n == 1 else "they"


def render_week(f: WeekFacts) -> str:
    lines = [f"Your week, {_day(f.start)} to {_day(f.end)}."]
    if f.commitments:
        parts = [f"{f.kept} kept", f"{f.partly} partly", f"{f.not_kept} not kept"]
        if f.unanswered:
            parts.append(f"{f.unanswered} never answered")
        if f.open:
            parts.append(f"{f.open} still open")
        line = f"Commitments: {f.commitments} ({', '.join(parts)})."
        if f.outside_time:
            line += f" {f.outside_time} reported outside {'its' if f.outside_time == 1 else 'their'} time, so {_it(f.outside_time)} didn't count."
        if f.unproven:
            line += f" {f.unproven} kept but not shown in the spot check, so {_it(f.unproven)} didn't count."
        lines.append(line)
    else:
        lines.append("Commitments: none this week.")
    if f.checks is not None:
        if f.checks:
            parts = [f"{f.checks_passed} passed"]
            if f.checks_failed:
                parts.append(f"{f.checks_failed} not passed")
            if f.checks_declined:
                parts.append(f"{f.checks_declined} not shown")
            if f.checks_waiting:
                parts.append(f"{f.checks_waiting} still open")
            lines.append(f"Spot checks: {f.checks} ({', '.join(parts)}).")
        else:
            lines.append("Spot checks: none this week.")
    if f.evaluated:
        days = f"{_day(f.start)} to {_day(f.end - timedelta(days=1))}"
        lines.append(f"Check-ins ({days}): {f.check_ins_ok} of {f.check_ins_due} answered.")
        tokens = f"Tokens earned ({days}): {f.tokens}."
        if f.vacation_days:
            tokens += f" {f.vacation_days} day{'s' if f.vacation_days != 1 else ''} on vacation."
        lines.append(tokens)
    else:
        lines.append("Check-ins and tokens: no finished days to count yet.")
    lines.append(f"Today is counted after midnight. {OFF_HINT}")
    return "\n".join(lines)


class WeeklyReview:
    def __init__(self, core: CoreDatabase, *, user_id: str, ports: Ports) -> None:
        self._core = core
        self.user_id = user_id
        self._ports = ports

    # ------------------------------------------------------------------ the switch

    def is_on(self, user_id: str) -> bool:
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT enabled FROM weekly_review_settings WHERE user_id = ?", (user_id,))
        return row is None or bool(row["enabled"])

    def set(self, user_id: str, on: bool, *, now: datetime) -> bool:
        """True when the setting changed."""
        def write(tx: Transaction, _s: object) -> bool:
            row = tx.fetch_one("SELECT enabled FROM weekly_review_settings WHERE user_id = ?", (user_id,))
            if (row is None or bool(row["enabled"])) == on:
                return False
            tx.execute("INSERT INTO weekly_review_settings (user_id, enabled, updated_at) VALUES (?, ?, ?) "
                       "ON CONFLICT(user_id) DO UPDATE SET enabled = excluded.enabled, updated_at = excluded.updated_at",
                       (user_id, 1 if on else 0, _iso(now)))
            write_event(tx, DomainEvent(event_type="weekly_review.turned_on" if on else "weekly_review.turned_off", source_module="weekly_review",
                                        occurred_at=now, payload={"user_id": user_id}))
            return True

        return apply_transition(self._core, write=write)

    # ------------------------------------------------------------------ the facts

    def facts(self, week_start: date) -> WeekFacts:
        end = week_start + timedelta(days=6)
        counts: dict[str, int] = {}

        def add(key: str) -> None:
            counts[key] = counts.get(key, 0) + 1

        for row in self._ports.commitments(week_start, end):
            status, timing = row.get("status"), row.get("report_timing")
            if status in _STATUS_KEYS:
                add(_STATUS_KEYS[status])
            if status in ("kept", "partly") and timing in ("late", "too_soon"):
                add("outside_time")
            if status == "kept" and timing == "unproven":
                add("unproven")
        if self._ports.checks is not None:
            rows = [r for r in self._ports.checks(week_start, end) if r.get("status") != "cancelled"]
            counts["checks"] = len(rows)
            for r in rows:
                add(_CHECK_KEYS.get(r.get("status"), "checks_waiting"))
        evaluations = self._ports.evaluations(week_start, end - timedelta(days=1))
        active = [r for r in evaluations if not r.get("paused")]
        counts["evaluated"] = len(evaluations)
        counts["vacation_days"] = len(evaluations) - len(active)
        counts["check_ins_ok"] = sum(int(r.get("check_ins_ok") or 0) for r in active)
        counts["check_ins_due"] = sum(int(r.get("check_ins_due") or 0) for r in active)
        counts["tokens"] = sum(int(r.get("tokens_earned") or 0) for r in evaluations)
        return WeekFacts(start=week_start, end=end, **counts)

    # ------------------------------------------------------------------ Sunday evening

    def run(self, now: datetime) -> str | None:
        """Decides this week's review when it is due: 'sent', 'vacation', or None (not due, off, or already decided)."""
        local = self._ports.local(now)
        if local.weekday() != 6 or local.time() < SEND_AT or not self.is_on(self.user_id):
            return None
        week_start = local.date() - timedelta(days=6)
        with self._core.transaction() as tx:
            if tx.fetch_one("SELECT 1 FROM weekly_reviews WHERE user_id = ? AND week_start = ?", (self.user_id, week_start.isoformat())):
                return None
        outcome = "vacation" if self._ports.paused(now) else "sent"
        text = render_week(self.facts(week_start)) if outcome == "sent" else None

        def write(tx: Transaction, _s: object) -> bool:
            done = tx.execute("INSERT OR IGNORE INTO weekly_reviews (user_id, week_start, decided_at, outcome) VALUES (?, ?, ?, ?)",
                              (self.user_id, week_start.isoformat(), _iso(now), outcome)).rowcount
            if not done:
                return False
            if text is not None:
                enqueue_in_transaction(tx, user_id=self.user_id, kind="weekly_review", dedupe_key=f"weekly_review:{week_start.isoformat()}",
                                       text=text, now=now)
            write_event(tx, DomainEvent(event_type="weekly_review.week_decided", source_module="weekly_review", occurred_at=now,
                                        payload={"user_id": self.user_id, "week_start": week_start.isoformat(), "outcome": outcome}))
            return True

        if not apply_transition(self._core, write=write):
            return None
        logger.info("Weekly review for the week of %s: %s.", week_start.isoformat(), outcome)
        return outcome

    async def loop(self, clock, interval_seconds: float = 300.0) -> None:
        import asyncio
        while True:
            try:
                await asyncio.to_thread(self.run, clock.now())
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("The weekly review failed; will retry.")
            await asyncio.sleep(interval_seconds)
