"""
behavioral_telemetry/analysis.py

The owner's offline behavioural analysis (v0.39 Phase G3; `python -m cli research analyze`). See docs/architecture/phase_g_proposal.md section 6.

1. `summarize` reads `behavioral_telemetry` through a READ-ONLY SQLite connection (`mode=ro`: this module cannot write to any database) and condenses it
   into a short numeric text per user. No raw rows and no free text are sent to the model.
2. `analyze` sends that summary with ANALYST_PROMPT to a local model and returns the report with DISCLAIMER appended by code, whatever the model wrote.
3. `bot_is_live` reports whether the bot is running against a database (its startup lease is held now or was acquired within LIVE_WINDOW), so the CLI
   can refuse to compete with live replies for the GPU.

The report is for the owner only: the CLI prints it or writes it to a plain file. Nothing here stores it, and nothing that builds model context may import this
package (tests guard both).
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from infrastructure.time_format import iso as _iso
from infrastructure.time_format import parse_iso as _parse_iso

__all__ = ["UserSummary", "summarize", "analyze", "bot_is_live", "is_local_host", "ANALYST_PROMPT", "DISCLAIMER", "LIVE_WINDOW", "MAX_REPORT_CHARACTERS"]

DISCLAIMER = ("WARNING: Any recommended parameter changes are critical_changes and require explicit wearer consent before implementation.")
LIVE_WINDOW = timedelta(minutes=10)
MAX_REPORT_CHARACTERS = 8000

ANALYST_PROMPT = """You are a careful behavioral analyst. You receive a numeric summary of one person's records from a consensual chastity-coaching \
system (check-ins, a token economy, daily goal evaluations, a hidden progressive difficulty factor, and chat timing). Write a report titled \
"Psychological Profile & Recommendations" for the keyholder, in English, with these sections:
1. Overview (2-4 sentences).
2. Behavioral profile: engagement, reliability, responsiveness, consistency over time, and how performance changed as the difficulty factor rose \
(resilience). Cite the numbers you rely on.
3. Risk and wellbeing signals: sudden drops, long silences, rising misses. Treat them as reasons to check in on the person's wellbeing first.
4. Recommendations: at most 5, each concrete and limited to the system's existing settings (check-in frequency, goals, conditioning on or off, \
penalty windows). Mark every recommendation that changes a parameter as needing the wearer's consent.
5. Data limitations.
Rules: use only the numbers given and never invent data; say so when samples are small; this is a behavioral summary, not a diagnosis, so never use \
clinical or diagnostic labels; never recommend anything that bypasses consent, health or safety; no sexual content; plain text, no tables."""


def is_local_host(url: str) -> bool:
    """True when a model URL points at this machine (the summary must never leave it)."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host in ("localhost", "127.0.0.1", "::1") or host.endswith(".localhost")


def _connect_read_only(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def bot_is_live(db_paths, now: datetime) -> bool:
    """True when any of these databases has a startup lease that is held now or was acquired within LIVE_WINDOW (the periodic cycle takes it every
    cycle). A database that cannot be read counts as not live."""
    for path in db_paths:
        if not Path(path).exists():
            continue
        try:
            conn = _connect_read_only(path)
            try:
                row = conn.execute("SELECT expires_at, acquired_at FROM system_startup_lease WHERE id = 1").fetchone()
            finally:
                conn.close()
        except sqlite3.Error:
            continue
        if row is None:
            continue
        if row["expires_at"] and _parse_iso(row["expires_at"]) > now:
            return True
        if row["acquired_at"] and now - _parse_iso(row["acquired_at"]) < LIVE_WINDOW:
            return True
    return False


@dataclass(frozen=True)
class UserSummary:
    user_id: str
    records: int
    text: str


def _q(values, p):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    return values[min(len(values) - 1, int(round(p * (len(values) - 1))))]


def _fmt(value, digits=2):
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _pct(part, whole):
    return "n/a" if not whole else f"{100 * part / whole:.0f}%"


def summarize(db_path: str | Path, *, days: int, now: datetime) -> list[UserSummary]:
    """One dense numeric summary per user with records in the last `days` days (oldest user first)."""
    since = now - timedelta(days=days)
    conn = _connect_read_only(db_path)
    try:
        found = conn.execute("SELECT user_id, occurred_at, event_type, metrics FROM behavioral_telemetry WHERE occurred_at >= ? ORDER BY occurred_at",
                             (_iso(since),)).fetchall()
    finally:
        conn.close()
    by_user: dict[str, list] = defaultdict(list)
    for r in found:
        by_user[r["user_id"]].append((_parse_iso(r["occurred_at"]), r["event_type"], json.loads(r["metrics"])))
    return [UserSummary(user_id=u, records=len(rs), text=_summary_text(rs, days=days, since=since, now=now)) for u, rs in by_user.items()]


def _summary_text(rows, *, days: int, since: datetime, now: datetime) -> str:
    kind: dict[str, list] = defaultdict(list)
    for at, event_type, metrics in rows:
        kind[event_type].append((at, metrics))
    weeks = max(days / 7, 1 / 7)
    lines = [f"PERIOD: last {days} days ({since.date()} to {now.date()}); {len(rows)} records."]

    daily = [m for _, m in kind["daily_summary"]]
    if daily:
        n = len(daily)
        clean = sum(1 for m in daily if m.get("clean"))
        moods = Counter(m.get("bot_mood") for m in daily)
        modes = Counter(m.get("operating_mode") for m in daily)
        lines.append(
            f"DAILY ({n} evaluated days): clean {clean}/{n} ({_pct(clean, n)}); goals met {sum(m.get('goals_met') or 0 for m in daily)}/"
            f"{sum(m.get('goals_assigned') or 0 for m in daily)}; check-ins ok {sum(m.get('check_ins_ok') or 0 for m in daily)}/"
            f"{sum(m.get('check_ins_due') or 0 for m in daily)}; violations {sum(m.get('violations') or 0 for m in daily)}; "
            f"avg progress {_fmt(statistics.fmean(m.get('progress') or 0 for m in daily), 3)}; avg trust {_fmt(statistics.fmean(m.get('trust_used') or 0 for m in daily))}; "
            f"max streak {max(m.get('streak_after') or 0 for m in daily)}, final streak {daily[-1].get('streak_after')}; "
            f"tokens earned {sum(m.get('tokens_earned') or 0 for m in daily)}; bot stance {dict(moods)}; mode {dict(modes)}.")
    else:
        lines.append("DAILY: no evaluated days.")

    checks = [m for _, m in kind["response_delay"]]
    if checks:
        status = Counter(m.get("status") for m in checks)
        delays = [m.get("delay_seconds") for m in checks]
        lines.append(
            f"CHECK-INS ({len(checks)} concluded): {dict(status)}; miss rate {_pct(status.get('missed', 0), len(checks))}; reply delay seconds "
            f"median {_fmt(_q(delays, 0.5))}, p90 {_fmt(_q(delays, 0.9))}; levels {dict(Counter(m.get('level') for m in checks))}.")
    else:
        lines.append("CHECK-INS: none concluded.")

    burns = [m for _, m in kind["token_burn"]]
    lines.append(
        f"TOKENS: {len(burns)} spent ({_fmt(len(burns) / weeks)} per week); hours from last earn to spend median "
        f"{_fmt(_q([m.get('hours_since_last_earn') for m in burns], 0.5))}; balance after last spend {burns[-1].get('balance_after') if burns else 'n/a'}.")

    inc = [m for _, m in kind["inflation_resilience"]]
    factors = [m.get("training_inflation_factor") for m in daily if m.get("training_inflation_factor") is not None]
    if inc or factors:
        steps = "; ".join(f"factor {_fmt(m.get('training_inflation_factor'), 3)} ({m.get('outcome')}): prior 7d avg progress "
                          f"{_fmt(m.get('progress_avg_7d'), 3)}, clean days {m.get('clean_days_7d')}" for m in inc[-6:])
        lines.append(f"CONDITIONING: factor {_fmt(factors[0] if factors else None, 3)} -> {_fmt(factors[-1] if factors else None, 3)}; "
                     f"{len(inc)} confirmed releases considered {dict(Counter(m.get('outcome') for m in inc))}" + (f"; {steps}." if steps else "."))
    else:
        lines.append("CONDITIONING: no data.")

    gens = [m for _, m in kind["llm_generation"]]
    gaps = [m.get("seconds_since_last_reply") for _, m in kind["conversation_gap"]]
    if gens:
        structured = Counter(m.get("structured_status") for m in gens if m.get("structured_status"))
        lines.append(
            f"CHAT: {len(gens)} model turns ({_fmt(len(gens) / weeks)} per week); latency ms median {_fmt(_q([m.get('latency_ms') for m in gens], 0.5))}, "
            f"p90 {_fmt(_q([m.get('latency_ms') for m in gens], 0.9))}; outcomes {dict(Counter(m.get('outcome') for m in gens))}; structured {dict(structured)}; "
            f"untrusted channel {sum(1 for m in gens if m.get('trusted') is False)}; wearer reply gap hours median {_fmt(_hours(_q(gaps, 0.5)))}, "
            f"p90 {_fmt(_hours(_q(gaps, 0.9)))}, gaps over 24 h {sum(1 for g in gaps if g and g > 86400)}.")
    else:
        lines.append("CHAT: no model turns recorded.")

    trend: dict[str, dict] = defaultdict(lambda: {"days": 0, "clean": 0, "progress": 0.0, "checks": 0, "missed": 0, "turns": 0})
    for at, event_type, m in rows:
        week = f"{at.isocalendar().year}-W{at.isocalendar().week:02d}"
        t = trend[week]
        if event_type == "daily_summary":
            t["days"] += 1
            t["clean"] += 1 if m.get("clean") else 0
            t["progress"] += m.get("progress") or 0
        elif event_type == "response_delay":
            t["checks"] += 1
            t["missed"] += 1 if m.get("status") == "missed" else 0
        elif event_type == "llm_generation":
            t["turns"] += 1
    if trend:
        lines.append("WEEKLY: " + "; ".join(
            f"{w} clean {t['clean']}/{t['days']} avg progress {_fmt(t['progress'] / t['days'] if t['days'] else None, 3)} "
            f"check-ins missed {t['missed']}/{t['checks']} chat turns {t['turns']}" for w, t in sorted(trend.items())) + ".")
    return "\n".join(lines)


def _hours(seconds):
    return None if seconds is None else round(seconds / 3600, 2)


def analyze(model, summary: UserSummary) -> str:
    """The model's report plus the hard-coded consent disclaimer. `model` has `generate(request=...)` (conversation_engine's model protocol)."""
    from conversation_engine.model_types import ModelGenerationRequest, ModelMessage, ModelMessageRole

    request = ModelGenerationRequest(
        messages=(ModelMessage(role=ModelMessageRole.SYSTEM, content=ANALYST_PROMPT),
                  ModelMessage(role=ModelMessageRole.USER, content="Numeric summary:\n" + summary.text)),
        max_output_characters=MAX_REPORT_CHARACTERS)
    report = model.generate(request=request).strip()
    return f"{report}\n\n{DISCLAIMER}"
