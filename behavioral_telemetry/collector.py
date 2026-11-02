"""
behavioral_telemetry/collector.py

TelemetryCollector (v0.39 Phase G1, Tier A of docs/architecture/phase_g_proposal.md section 4). Derives Behavioral Telemetry from tables the system already
writes, as the LAST step of the periodic cycle; nothing here runs in the reply path.

Each source has a composite watermark (timestamp, id) in `telemetry_watermarks`. A run reads at most BATCH rows per source after the watermark and, in ONE
transaction per source, records them (`INSERT OR IGNORE` on `source_key`) and moves the watermark, so a re-run, a restart or two instances never duplicate a
row. Facts older than the retention period are never derived. Old rows are pruned once per day. The sources are only read.

Phase G2: the run first drains the runtime buffer (behavioral_telemetry.runtime) into this database in ONE transaction; without consent the samples
are discarded, and samples of a user who ran `forget all` after they were taken are dropped.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal

from behavioral_telemetry.store import RETENTION_DAYS, TelemetryStore, record_in
from infrastructure.database import Transaction, apply_transition
from infrastructure.time_format import iso as _iso
from infrastructure.time_format import parse_iso as _parse_iso

logger = logging.getLogger("ai_coach_keyholder.behavioral_telemetry")

__all__ = ["TelemetryCollector", "BATCH", "bot_mood"]

BATCH = 500
_CHECK_IN_CONCLUDED = ("answered", "answered_uncertain", "insufficient", "missed")
_FACTOR_START = "1.000"


def _num(text) -> float | None:
    return None if text is None else round(float(Decimal(str(text))), 4)


def _seconds(start: str | None, end: str | None) -> int | None:
    if not start or not end:
        return None
    return int((_parse_iso(end) - _parse_iso(start)).total_seconds())


def bot_mood(*, strict: bool, clean: bool, streak: int) -> str:
    """The engine's stance, derived from state (never generated): strict under a penalty window or supervision, else from the day's result."""
    if strict:
        return "strict"
    if not clean:
        return "demanding"
    return "approving" if streak >= 3 else "neutral"


class TelemetryCollector:
    def __init__(self, store: TelemetryStore, *, buffer=None) -> None:
        self._store = store
        self._buffer = buffer
        self._core = store.core
        self._pruned_on: date | None = None

    def run(self, now: datetime) -> int:
        """One collection pass; returns the number of new records."""
        added = self._drain(now)
        with self._core.transaction() as tx:
            if not self._store.recording_in(tx):
                return added
        for source in (self._check_ins, self._token_burn, self._increments, self._daily):
            added += source(now)
        if self._pruned_on != now.date():
            self._store.prune(now)
            self._pruned_on = now.date()
        if added:
            logger.debug("Behavioral telemetry: %d new record(s).", added)
        return added

    def _drain(self, now: datetime) -> int:
        if self._buffer is None:
            return 0
        samples = self._buffer.take()
        if not samples:
            return 0

        def write(tx: Transaction, _s: object) -> int:
            if not self._store.recording_in(tx):
                return 0
            count = 0
            for seq, user_id, kind, metrics, at in samples:
                if self._buffer.is_forgotten(user_id, seq):
                    continue
                try:
                    count += record_in(tx, user_id=user_id, occurred_at=at, event_type=kind, metrics=metrics, source_key=f"runtime:{uuid.uuid4()}", now=now)
                except ValueError:
                    logger.warning("A runtime telemetry sample was refused (type %s).", kind)
            return count

        return apply_transition(self._core, write=write)

    # ------------------------------------------------------------------ the generic pass

    def _pass(self, now: datetime, *, source: str, sql: str, at: str, ident: str, derive) -> int:
        """`sql` selects the source rows (with `{after}` for the watermark condition and a trailing ORDER BY / LIMIT); `derive(tx, row)` returns
        (user_id, occurred_at, event_type, metrics)."""
        floor = (_iso(now - timedelta(days=RETENTION_DAYS)), "")

        def write(tx: Transaction, _s: object) -> int:
            mark = tx.fetch_one("SELECT position_at, position_id FROM telemetry_watermarks WHERE source = ?", (source,))
            pos = max(floor, (mark["position_at"], mark["position_id"])) if mark else floor
            after = f"({at} > ? OR ({at} = ? AND {ident} > ?))"
            rows = tx.fetch_all(sql.format(after=after) + f" ORDER BY {at}, {ident} LIMIT {BATCH}", (pos[0], pos[0], pos[1]))
            count = 0
            for row in rows:
                user_id, occurred_at, event_type, metrics = derive(tx, row)
                count += record_in(tx, user_id=user_id, occurred_at=occurred_at, event_type=event_type, metrics=metrics,
                                   source_key=f"{source}:{row['pos_id']}", now=now)
            if rows:
                last = rows[-1]
                tx.execute("INSERT INTO telemetry_watermarks (source, position_at, position_id, updated_at) VALUES (?, ?, ?, ?) "
                           "ON CONFLICT(source) DO UPDATE SET position_at = excluded.position_at, position_id = excluded.position_id, "
                           "updated_at = excluded.updated_at", (source, last["pos_at"], last["pos_id"], _iso(now)))
            return count

        return apply_transition(self._core, write=write)

    # ------------------------------------------------------------------ sources

    def _check_ins(self, now: datetime) -> int:
        marks = ",".join(f"'{s}'" for s in _CHECK_IN_CONCLUDED)

        def derive(_tx, r):
            metrics = {"level": r["level"], "slot_index": r["slot_index"], "status": r["status"], "verdict": r["verdict"],
                       "delay_seconds": _seconds(r["sent_at"], r["responded_at"]) if r["status"] != "missed" else None,
                       "window_seconds": _seconds(r["sent_at"], r["deadline_at"])}
            occurred = r["responded_at"] or r["deadline_at"] or r["pos_at"]
            return r["user_id"], _parse_iso(occurred), "response_delay", metrics

        return self._pass(
            now, source="check_ins", at="updated_at", ident="id", derive=derive,
            sql="SELECT id AS pos_id, updated_at AS pos_at, user_id, level, slot_index, status, verdict, sent_at, deadline_at, responded_at "
                f"FROM check_ins WHERE status IN ({marks}) AND {{after}}")

    def _token_burn(self, now: datetime) -> int:
        def derive(tx, r):
            balance = tx.fetch_one("SELECT COALESCE(SUM(delta), 0) AS b FROM token_ledger WHERE user_id = ? AND created_at <= ?",
                                   (r["user_id"], r["pos_at"]))["b"]
            earn = tx.fetch_one("SELECT MAX(created_at) AS t FROM token_ledger WHERE user_id = ? AND entry_type = 'earn' AND created_at <= ?",
                                (r["user_id"], r["pos_at"]))["t"]
            hours = None if earn is None else round(_seconds(earn, r["pos_at"]) / 3600, 2)
            return r["user_id"], _parse_iso(r["pos_at"]), "token_burn", {"amount": abs(int(r["delta"])), "balance_after": int(balance),
                                                                          "hours_since_last_earn": hours}

        return self._pass(now, source="token_ledger", at="created_at", ident="id", derive=derive,
                          sql="SELECT id AS pos_id, created_at AS pos_at, user_id, delta FROM token_ledger WHERE entry_type = 'spend' AND {after}")

    def _increments(self, now: datetime) -> int:
        def derive(tx, r):
            prev = tx.fetch_one("SELECT MAX(created_at) AS t FROM training_increments WHERE user_id = ? AND created_at < ?",
                                (r["user_id"], r["pos_at"]))["t"]
            day = _parse_iso(r["pos_at"]).date()
            week = tx.fetch_one(
                "SELECT AVG(CAST(progress AS REAL)) AS p, COALESCE(SUM(clean), 0) AS c FROM daily_evaluations "
                "WHERE user_id = ? AND local_date < ? AND local_date >= ?",
                (r["user_id"], day.isoformat(), (day - timedelta(days=7)).isoformat()))
            metrics = {"outcome": r["outcome"], "training_inflation_factor": _num(r["factor_after"]), "factor_before": _num(r["factor_before"]),
                       "days_since_previous_increment": None if prev is None else round(_seconds(prev, r["pos_at"]) / 86400, 2),
                       "progress_avg_7d": None if week["p"] is None else round(float(week["p"]), 4), "clean_days_7d": int(week["c"])}
            return r["user_id"], _parse_iso(r["pos_at"]), "inflation_resilience", metrics

        return self._pass(now, source="training_increments", at="created_at", ident="id", derive=derive,
                          sql="SELECT id AS pos_id, created_at AS pos_at, user_id, outcome, factor_before, factor_after FROM training_increments WHERE {after}")

    def _daily(self, now: datetime) -> int:
        def derive(tx, r):
            factor = tx.fetch_one("SELECT factor_after FROM training_increments WHERE user_id = ? AND created_at <= ? ORDER BY created_at DESC, id DESC LIMIT 1",
                                  (r["user_id"], r["pos_at"]))
            mode = tx.fetch_one("SELECT current_mode FROM operating_mode_state WHERE id = 1")
            strict = (tx.fetch_one("SELECT 1 FROM penalty_windows WHERE status IN ('active', 'frozen') LIMIT 1") is not None
                      or tx.fetch_one("SELECT 1 FROM supervision_states WHERE active = 1 LIMIT 1") is not None)
            clean, streak = bool(r["clean"]), int(r["streak_after"])
            metrics = {"local_date": r["local_date"], "goals_assigned": r["goals_assigned"], "goals_met": r["goals_met"],
                       "check_ins_due": r["check_ins_due"], "check_ins_ok": r["check_ins_ok"], "violations": r["violations"], "clean": clean,
                       "streak_after": streak, "tokens_earned": r["tokens_earned"], "progress": _num(r["progress"]), "pool_after": _num(r["pool_after"]),
                       "trust_used": _num(r["trust_used"]), "training_inflation_factor": _num(factor["factor_after"] if factor else _FACTOR_START),
                       "operating_mode": mode["current_mode"] if mode else "standard", "bot_mood": bot_mood(strict=strict, clean=clean, streak=streak)}
            return r["user_id"], _parse_iso(r["pos_at"]), "daily_summary", metrics

        return self._pass(
            now, source="daily_evaluations", at="created_at", ident="(user_id || '|' || local_date)", derive=derive,
            sql="SELECT (user_id || '|' || local_date) AS pos_id, created_at AS pos_at, user_id, local_date, goals_assigned, goals_met, check_ins_due, "
                "check_ins_ok, violations, clean, streak_after, tokens_earned, progress, pool_after, trust_used FROM daily_evaluations WHERE {after}")
