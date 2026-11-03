"""
application/progress.py

Phase J1 (Zero-Command UX): "Show my progress" / "How am I doing?" answered from real data, deterministically and before any model.

- `is_progress_query(text)`: a precise, side-effect-free matcher (English and Czech, case, diacritics and punctuation ignored; short messages
  only). Like the lock-status matcher it prefers missing a phrasing (the conversation model still answers it) over a false match.
- `read_progress(...)`: plain reads of the user's own records: the local task (active one, tasks completed in the last 7 days), a running
  or planned vacation, and, ONLY for the owner (whose locks the bot watches), the community photo reviews of the last 7 days and how many
  rejections still wait for the keyholder's decision. Whether a penalty window runs is added by the caller, also owner-only.
- `render_progress(...)`: one short English reply.

Never shown, by design (Phase J principle 2, philosophy 2.6/2.22): the Trust score or level, or any internal identifier. This module does
not read Trust Manager state at all.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from infrastructure.database import Database as CoreDatabase
from infrastructure.time_format import iso as _iso

__all__ = ["PROGRESS_WINDOW_DAYS", "ReviewCounts", "ProgressSnapshot", "is_progress_query", "read_progress", "render_progress"]

PROGRESS_WINDOW_DAYS = 7
MAX_PROGRESS_QUERY_CHARACTERS = 80
_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")
_PROGRESS_WORDS = frozenset({"progress", "pokrok", "pokroky", "pokroku", "statistics", "stats", "statistiky", "statistika", "souhrn",
                             "summary", "prehled"})
_SELF_WORDS = frozenset({"my", "me", "i", "im", "muj", "moje", "moji", "mi", "mne", "jsem", "show", "ukaz", "zobraz"})


def _tokens(text: str) -> list[str]:
    decomposed = unicodedata.normalize("NFKD", text.lower())
    ascii_text = "".join(ch for ch in decomposed if not unicodedata.combining(ch) and unicodedata.category(ch) != "Cf")
    return [t for t in _TOKEN_SPLIT.split(ascii_text) if t]


def is_progress_query(message: str) -> bool:
    """`show my progress`, `my progress`, `how am I doing?`, `how's it going with me?`, `jak mi to jde?`, `jak se mi daří?`, `můj pokrok`,
    `ukaž můj přehled`. Pure, never raises for a str."""
    if not isinstance(message, str) or not message.strip() or len(message) > MAX_PROGRESS_QUERY_CHARACTERS:
        return False
    tokens = _tokens(message)
    if not tokens:
        return False
    token_set = set(tokens)
    pairs = list(zip(tokens, tokens[1:]))
    if token_set & _PROGRESS_WORDS and token_set & _SELF_WORDS:
        return True
    if ("how", "am") in pairs and ("i", "doing") in pairs:
        return True                                                     # how am I doing
    if ("jak", "mi") in pairs and ({"jde", "dari"} & token_set):
        return True                                                     # jak mi to jde / jak se mi dari
    return ("jak", "se") in pairs and "dari" in token_set and "mi" in token_set


@dataclass(frozen=True, kw_only=True)
class ReviewCounts:
    verified: int
    rejected: int
    ongoing: int
    awaiting_decision: int


@dataclass(frozen=True, kw_only=True)
class ProgressSnapshot:
    active_task_title: str | None
    tasks_completed: int
    vacation_running_until: date | None = None
    vacation_planned: tuple[date, date] | None = None
    reviews: ReviewCounts | None = None             # owner only
    penalty_running: bool | None = None             # owner only


def read_progress(core: CoreDatabase, *, user_id: str, now: datetime, active_task_title: str | None, include_reviews: bool,
                  vacation=None) -> ProgressSnapshot:
    since = _iso(now - timedelta(days=PROGRESS_WINDOW_DAYS))
    with core.transaction() as tx:
        completed = tx.fetch_one("SELECT COUNT(*) AS n FROM task_assignments WHERE user_id = ? AND status = 'completed' AND resolved_at >= ?",
                                 (user_id, since))["n"]
        reviews = None
        if include_reviews:
            rows = {r["status"]: r["n"] for r in tx.fetch_all(
                "SELECT status, COUNT(*) AS n FROM peer_reviews WHERE baseline = 0 AND updated_at >= ? GROUP BY status", (since,))}
            ongoing = tx.fetch_one("SELECT COUNT(*) AS n FROM peer_reviews WHERE status = 'ongoing'")["n"]
            waiting = tx.fetch_one("SELECT COUNT(*) AS n FROM peer_reviews WHERE incident_registered_at IS NOT NULL "
                                   "AND owner_decision IS NULL")["n"]
            reviews = ReviewCounts(verified=rows.get("verified", 0), rejected=rows.get("rejected", 0), ongoing=ongoing,
                                   awaiting_decision=waiting)
    running = planned = None
    v = vacation.current_or_next(user_id, now) if vacation is not None else None
    if v is not None:
        if v.running(now):
            running = v.end_date
        else:
            planned = (v.start_date, v.end_date)
    return ProgressSnapshot(active_task_title=active_task_title, tasks_completed=int(completed), vacation_running_until=running,
                            vacation_planned=planned, reviews=reviews)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def render_progress(p: ProgressSnapshot) -> str:
    lines = [f"Here is how you are doing (last {PROGRESS_WINDOW_DAYS} days):"]
    task = f'one active ("{p.active_task_title}")' if p.active_task_title else "none active right now"
    lines.append(f"- Tasks: {task}; {_plural(p.tasks_completed, 'task')} completed.")
    if p.reviews is not None:
        r = p.reviews
        lines.append(f"- Community photo reviews: {r.verified} verified, {r.rejected} rejected, {r.ongoing} still being voted on.")
        if r.awaiting_decision:
            lines.append(f"- {_plural(r.awaiting_decision, 'rejection')} still {'waits' if r.awaiting_decision == 1 else 'wait'} "
                         f"for the keyholder's decision; nothing has been deducted for {'it' if r.awaiting_decision == 1 else 'them'}.")
    if p.penalty_running is not None:
        lines.append("- A penalty window is running." if p.penalty_running else "- No penalty window is running.")
    if p.vacation_running_until is not None:
        lines.append(f"- You are on vacation until {p.vacation_running_until.isoformat()}.")
    elif p.vacation_planned is not None:
        lines.append(f"- A vacation is planned: {p.vacation_planned[0].isoformat()} to {p.vacation_planned[1].isoformat()}.")
    return "\n".join(lines)
