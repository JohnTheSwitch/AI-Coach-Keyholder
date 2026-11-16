"""
tamper_watch/service.py

The tamper watch (owner, 2026-11-16: "the worst thing is ... that I can always go and unlock myself through the system"). He is the wearer
and the operator, so changing the system can never be prevented; it can be made visible. At every start the bot checks three things and
raises each finding on the detectors' one path (asked to explain; unexplained = SYSTEM_TAMPERING, an Honesty Incident):

- the code it starts with has uncommitted changes (`dirty`);
- commits since the last start that are not Claude's (no "Co-Authored-By: Claude" trailer);
- the history it keeps (domain events, Trust evidence) changed while the bot was off: a fingerprint of every row up to a row id, taken
  every PASS and at each start, no longer matches.

What it cannot see, and says so: an edit made while the bot runs and its fingerprint is retaken, a change to `.env` (the settings watch
covers the time settings and the halt), and anything outside this repository. A finding is evidence, never a verdict: an explanation
("I fixed X myself") clears it, as with every detector.

Table `tamper_watch_state` (migration 087), written only here. Never raises.
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.decision_log import decision
from infrastructure.time_format import iso as _iso

logger = logging.getLogger("ai_coach_keyholder.tamper_watch")

__all__ = ["TamperWatch", "Finding", "CLAUDE_TRAILER", "PASS"]

CLAUDE_TRAILER = "co-authored-by: claude"
PASS = timedelta(minutes=15)


@dataclass(frozen=True)
class Finding:
    subject_id: str
    detail: str
    wearer_text: str
    facts: str


def _git(repo: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, timeout=15, check=False)
    except Exception:
        return None
    return out.stdout if out.returncode == 0 else None


def _fingerprint(tx: Transaction, table: str, columns: str, max_rowid: int) -> str:
    digest = hashlib.sha256()
    for row in tx.fetch_all(f"SELECT {columns} FROM {table} WHERE rowid <= ? ORDER BY rowid", (max_rowid,)):
        digest.update("\x1f".join("" if v is None else str(v) for v in tuple(row)).encode("utf-8"))
        digest.update(b"\x1e")
    return digest.hexdigest()


_TABLES = {"events": ("domain_events", "rowid, id, event_type, source_module, payload_json, occurred_at"),
           "evidence": ("trust_evidence", "rowid, id, domain_id, created_at, evidence_type, raw_weight, evidence_confidence")}


class TamperWatch:
    def __init__(self, core: CoreDatabase, *, repo: str | Path, raise_finding: Callable[[Finding, datetime], object],
                 git: Callable[..., str | None] | None = None) -> None:
        self._core = core
        self._repo = Path(repo)
        self._raise = raise_finding
        self._git = git or (lambda *args: _git(self._repo, *args))

    # ------------------------------------------------------------------ at start

    def check_at_start(self, now: datetime) -> list[Finding]:
        try:
            findings = self._findings(now)
        except Exception:
            logger.exception("The tamper watch could not check this start; nothing is raised.")
            return []
        for finding in findings:
            try:
                self._raise(finding, now)
                decision("tamper_watch", "Tamper watch finding: asked to explain.", subject=finding.subject_id, detail=finding.detail)
            except Exception:
                logger.exception("A tamper finding could not be raised.")
        self.snapshot(now, commit=self._head())
        return findings

    def _head(self) -> str | None:
        head = self._git("rev-parse", "HEAD")
        return head.strip() if head else None

    def _findings(self, now: datetime) -> list[Finding]:
        out: list[Finding] = []
        with self._core.transaction() as tx:
            state = tx.fetch_one("SELECT * FROM tamper_watch_state WHERE id = 1")
        head = self._head()
        status = self._git("status", "--porcelain", "--untracked-files=no")
        # Only what the running bot executes counts: tests and documentation never change its behaviour.
        changed = [line[3:].strip() for line in (status or "").splitlines() if line.strip()]
        changed = [f for f in changed if not (f.startswith(("tests/", "docs/")) or f.endswith(".md"))]
        if changed:
            files = ", ".join(changed[:8])
            out.append(Finding(f"dirty:{head or 'unknown'}:{_iso(now)[:13]}", "The bot started with uncommitted changes to its code.",
                               "I started with code changes that aren't committed. Did you change me yourself? What and why?",
                               f"Uncommitted changes at start ({_iso(now)}): {files}."))
        if state is not None and state["last_commit"] and head and head != state["last_commit"]:
            log = self._git("log", "--format=%H%x1f%s%x1f%B%x1e", f"{state['last_commit']}..{head}")
            for entry in (log or "").split("\x1e"):
                parts = entry.strip().split("\x1f")
                if len(parts) < 3:
                    continue
                sha, subject, body = parts[0].strip(), parts[1].strip(), parts[2]
                if CLAUDE_TRAILER not in body.lower():
                    out.append(Finding(f"commit:{sha}", "A commit to the bot's code that was not Claude's.",
                                       f"There's a change to my code that wasn't made by Claude: \"{subject[:120]}\". What did you change, and why?",
                                       f"Commit {sha[:12]} ({subject[:200]}) has no Claude co-author line; it arrived since the last start."))
        if state is not None and state["events_max_rowid"] is not None:
            with self._core.transaction() as tx:
                changed = [name for name, (table, cols) in _TABLES.items()
                           if _fingerprint(tx, table, cols, int(state[f"{name}_max_rowid"] or 0)) != state[f"{name}_hash"]]
            if changed:
                what = " and ".join({"events": "the event history", "evidence": "the Trust evidence"}[n] for n in changed)
                out.append(Finding(f"history:{state['snapshot_at']}", "The bot's stored history changed while it was not running.",
                                   f"Something in {what} I keep changed while I was off. Did you edit my database? What and why?",
                                   f"Fingerprint of {what} up to the snapshot of {state['snapshot_at']} no longer matches at this start."))
        return out

    # ------------------------------------------------------------------ the fingerprint

    def snapshot(self, now: datetime, *, commit: str | None = None) -> None:
        """Every PASS while running, and at each start (after the check): the fingerprint of everything written so far."""
        def write(tx: Transaction, _s: object) -> None:
            values = {}
            for name, (table, cols) in _TABLES.items():
                top = tx.fetch_one(f"SELECT COALESCE(MAX(rowid), 0) AS m FROM {table}")["m"]
                values[name] = (int(top), _fingerprint(tx, table, cols, int(top)))
            tx.execute("INSERT INTO tamper_watch_state (id, last_commit, snapshot_at, events_max_rowid, events_hash, evidence_max_rowid, evidence_hash, "
                       "updated_at) VALUES (1, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET last_commit = COALESCE(excluded.last_commit, last_commit), "
                       "snapshot_at = excluded.snapshot_at, events_max_rowid = excluded.events_max_rowid, events_hash = excluded.events_hash, "
                       "evidence_max_rowid = excluded.evidence_max_rowid, evidence_hash = excluded.evidence_hash, updated_at = excluded.updated_at",
                       (commit, _iso(now), *values["events"], *values["evidence"], _iso(now)))

        try:
            apply_transition(self._core, write=write)
        except Exception:
            logger.exception("The tamper watch fingerprint could not be taken; tried again next pass.")

    async def loop(self, clock, *, initial_delay_seconds: float = 60.0) -> None:
        """The start check once (after the bot is up), then a fingerprint every PASS."""
        import asyncio
        await asyncio.sleep(initial_delay_seconds)
        await asyncio.to_thread(self.check_at_start, clock.now())
        while True:
            await asyncio.sleep(PASS.total_seconds())
            await asyncio.to_thread(self.snapshot, clock.now())
