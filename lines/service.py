"""
lines/service.py

Lines (owner, 2026-11-14; brat taming B2; migration 066). Every confirmed incident gets lines: LINES_WARNING for a Warning-level incident,
LINES_WINDOW for one that opens or extends a Penalty Window, with a sentence that matches the rule broken. The wearer types them in the bot
DM; code counts them (no model): a line counts when it is the sentence exactly, ignoring case, spacing, a leading number ("12." / "12)")
and the closing full stop. Several lines per message, several messages per assignment; extra lines go to the next open assignment.

While lines are open (`open_lines`), the next planned hygiene break waits (exemptions/service.py) and, as release debt (roadmap B4), a token
cannot be spent on a release. A health need never waits for anything here (philosophy 3.9).

On paper (owner, 2026-11-14; migration 068): when `paper` is wired, the lines are written by hand and shown as a Chaster task ("Show: 20
handwritten lines: ..."; photo of the sheet with Chaster's code, community review). Verified -> done; rejected -> written again. Only one
Chaster task can be open, so a sheet waits for the slot (spot checks and recovery tasks); when it has waited FALLBACK_AFTER, typing the lines
in the DM counts too, so owed lines never hold hygiene forever.

Assigned from the Penalty Engine's own events, in the same transaction that consumes them (a consumer: only the payload is used):
`penalty_warning.recorded` and `extension.decision_recorded`. One assignment per incident, whatever replays.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from application.models import OutgoingMessage
from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.decision_log import decision
from infrastructure.outbox import DomainEvent, write_event
from infrastructure.time_format import iso as _iso
from infrastructure.time_format import parse_iso as _parse_iso
from notifications.queue import enqueue_in_transaction

logger = logging.getLogger("ai_coach_keyholder.lines")

__all__ = ["LineService", "PaperPort", "LINES_WARNING", "LINES_WINDOW", "SENTENCES", "sentence_for", "normalize", "FALLBACK_AFTER"]

LINES_WARNING = 20              # owner, 2026-11-14
LINES_WINDOW = 40               # owner, 2026-11-14
FALLBACK_AFTER = timedelta(hours=3)    # a sheet that waited this long for Chaster's task slot may be typed in the DM instead


@dataclass(frozen=True, kw_only=True)
class PaperPort:
    chaster: object                                  # proof_checks.chaster_tasks.ChasterTasks (one lock, one task slot)
    lock_id: Callable[[datetime], str | None]
    halted: Callable[[], bool]
    slot_taken: Callable[[], bool]                   # an open spot check or recovery task
DEFAULT_SENTENCE = "I follow the rules I agreed to."
SENTENCES = {
    "HYGIENE_MISUSE": "I use hygiene breaks only for hygiene.",
    "CHECKIN_MISSED_FIRST": "I answer my check-ins on time.",
    "CHECKIN_INSUFFICIENT": "I answer my check-ins properly.",
    "CHECKIN_REPEATED_NONCOMPLIANCE": "I answer my check-ins on time.",
    "DETECTION_UNANSWERED": "I answer my keyholder's questions.",
    "EXEMPTION_OVERRIDE": "A declined exemption means no.",
    "FALSE_REPORT": "I report only what I really did.",
    "OBEDIENCE_TASK_MISSED": "I obey my keyholder's tasks.",
    "HYGIENE_PICTURE_MISSING": "I send my relock picture every time.",
    "EPISTEMIC_MISREPRESENTATION": "I tell the truth about my lock.",
    "FALSE_HEALTH_CLAIM": "I claim a health need only when it is real.",
}
_NUMBER = re.compile(r"^\s*\d{1,3}\s*[.):-]\s*")
_QUOTES = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"'})


def sentence_for(rule_group_id: str | None) -> str:
    return SENTENCES.get(rule_group_id or "", DEFAULT_SENTENCE)


def normalize(line: str) -> str:
    text = _NUMBER.sub("", line.translate(_QUOTES)).strip().strip('"').strip()
    text = re.sub(r"\s+", " ", text).lower()
    return text.rstrip(".!").rstrip()


class LineService:
    def __init__(self, core: CoreDatabase, *, user_id: str) -> None:
        self._core = core
        self.user_id = user_id
        self.paper: PaperPort | None = None          # set by the wiring when Chaster tasks work (lines on paper)

    # ------------------------------------------------------------------ assignment (outbox consumers)

    def consumers(self) -> dict:
        """event type -> handler, for `build_consumer_registry(extra_consumers={"lines": ...})`."""
        return {
            "penalty_warning.recorded": lambda tx, event: self._assign(tx, event, LINES_WARNING),
            "extension.decision_recorded": lambda tx, event: self._assign(tx, event, LINES_WINDOW),
        }

    def _assign(self, tx: Transaction, event, required: int) -> None:
        incident_id = event.payload.get("incident_id")
        if not incident_id:
            return
        rule = event.payload.get("rule_group_id")
        sentence = sentence_for(rule)
        line_id = str(uuid.uuid4())
        done = tx.execute("INSERT OR IGNORE INTO line_assignments (id, user_id, incident_id, rule_group_id, sentence, required, done, status, created_at) "
                          "VALUES (?, ?, ?, ?, ?, ?, 0, 'open', ?)", (line_id, self.user_id, incident_id, rule, sentence, required,
                                                                      _iso(event.occurred_at))).rowcount
        if not done:
            return
        write_event(tx, DomainEvent(event_type="lines.assigned", source_module="lines", occurred_at=event.occurred_at,
                                    payload={"line_assignment_id": line_id, "incident_id": incident_id, "required": required}))
        how = ("by hand on paper. A Chaster task will ask for a photo of the sheet with Chaster's code" if self.paper is not None
               else "one per line, here in our chat")
        enqueue_in_transaction(tx, user_id=self.user_id, kind="lines", dedupe_key=f"lines:{incident_id}", now=event.occurred_at,
                               text=(f"Lines: write this {required} times, {how}:\n\"{sentence}\"\n"
                                     "Until they're done, your next planned hygiene break and any release wait. A health need never waits."))
        decision("lines", f"{required} lines assigned for a confirmed incident.", incident_id=incident_id, rule_group_id=rule)

    # ------------------------------------------------------------------ reads

    def open_lines(self, user_id: str) -> int:
        """How many lines are still owed (0 when none)."""
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT COALESCE(SUM(required - done), 0) AS n FROM line_assignments WHERE user_id = ? AND status = 'open'", (user_id,))
        return int(row["n"])

    def get_lines_between(self, start, end) -> list[dict]:
        """Work queue Q4 (weekly review): assignments created on local dates start..end (UTC dates are close enough for a weekly count)."""
        with self._core.transaction() as tx:
            return [dict(r) for r in tx.fetch_all("SELECT required, done, status FROM line_assignments WHERE user_id = ? AND substr(created_at, 1, 10) "
                                                  "BETWEEN ? AND ?", (self.user_id, start.isoformat(), end.isoformat()))]

    def get_open_assignments(self, user_id: str) -> list[dict]:
        with self._core.transaction() as tx:
            return [dict(r) for r in tx.fetch_all("SELECT id, sentence, required, done FROM line_assignments WHERE user_id = ? AND status = 'open' "
                                                  "ORDER BY created_at, rowid", (user_id,))]

    # ------------------------------------------------------------------ on paper (Chaster task)

    def _typing_allowed(self, user_id: str, now: datetime) -> bool:
        """Typing counts when the oldest owed sheet has waited FALLBACK_AFTER for Chaster's task slot without getting a task."""
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT created_at, task_id FROM line_assignments WHERE user_id = ? AND status = 'open' ORDER BY created_at, rowid LIMIT 1",
                               (user_id,))
        return row is not None and row["task_id"] is None and now - _parse_iso(row["created_at"]) >= FALLBACK_AFTER

    def is_lines_task(self, task_id: str | None) -> bool:
        """For the community review watch: a sheet's review is decided here, never as a rejected verification photo."""
        if not task_id:
            return False
        with self._core.transaction() as tx:
            return tx.fetch_one("SELECT 1 FROM line_assignments WHERE task_id = ?", (task_id,)) is not None

    def has_open_task(self) -> bool:
        with self._core.transaction() as tx:
            return tx.fetch_one("SELECT 1 FROM line_assignments WHERE user_id = ? AND status = 'open' AND task_id IS NOT NULL",
                                (self.user_id,)) is not None

    def paper_tick(self, now: datetime) -> None:
        """Reads the open sheet's task, or puts the oldest owed sheet on Chaster when the slot is free."""
        port = self.paper
        if port is None:
            return
        with self._core.transaction() as tx:
            rows = [dict(r) for r in tx.fetch_all("SELECT * FROM line_assignments WHERE user_id = ? AND status = 'open' ORDER BY created_at, rowid",
                                                  (self.user_id,))]
        if not rows:
            return
        with_task = next((r for r in rows if r["task_id"]), None)
        if with_task is not None:
            self._read_sheet(with_task, now)
            return
        lock_id = port.lock_id(now)
        if lock_id is None or port.halted() or port.slot_taken() or port.chaster.current_task(lock_id) is not None:
            return
        row = rows[0]
        left = row["required"] - row["done"]
        task_id = port.chaster.assign(lock_id, f"Show: {left} handwritten lines: {row['sentence']}"[:200])

        def write(tx: Transaction, _s: object) -> None:
            tx.execute("UPDATE line_assignments SET lock_id = ?, task_id = ?, task_assigned_at = ?, review_status = NULL WHERE id = ? AND status = 'open'",
                       (lock_id, task_id, _iso(now), row["id"]))
            write_event(tx, DomainEvent(event_type="lines.sheet_assigned", source_module="lines", occurred_at=now,
                                        payload={"line_assignment_id": row["id"]}))
            enqueue_in_transaction(tx, user_id=self.user_id, kind="lines", dedupe_key=f"lines:sheet:{row['id']}:{task_id}", now=now,
                                   text=(f"Your lines are in Chaster now: write \"{row['sentence']}\" {left} times by hand, then submit a photo of the "
                                         "sheet with Chaster's code in the task."))

        apply_transition(self._core, write=write)
        decision("lines", "A sheet of lines was put on Chaster as a task.", line_assignment_id=row["id"], lines=left)

    def _read_sheet(self, row: dict, now: datetime) -> None:
        port = self.paper
        entry = port.chaster.task_entry(row["lock_id"], row["task_id"])
        if entry is None:
            current = port.chaster.current_task(row["lock_id"])
            if current is not None and current.get("id") == row["task_id"]:
                return                                       # still open, nothing submitted yet
            status = "removed"
        elif entry.get("status") == "removed":
            status = "removed"
        else:
            review = entry.get("peerVerification") if isinstance(entry.get("peerVerification"), dict) else {}
            status = review.get("status")
        if status not in ("verified", "rejected", "removed"):
            if status != row["review_status"]:
                self._set_review(row["id"], status)
            return
        if status == "verified":
            def write(tx: Transaction, _s: object) -> bool:
                done = tx.execute("UPDATE line_assignments SET done = required, status = 'done', completed_at = ?, review_status = 'verified' "
                                  "WHERE id = ? AND status = 'open'", (_iso(now), row["id"])).rowcount
                if done:
                    write_event(tx, DomainEvent(event_type="lines.completed", source_module="lines", occurred_at=now,
                                                payload={"line_assignment_id": row["id"]}))
                    enqueue_in_transaction(tx, user_id=self.user_id, kind="lines", dedupe_key=f"lines:verified:{row['id']}", now=now, immediate=True,
                                           text="Your sheet of lines was verified. That's done.")
                return bool(done)

            if apply_transition(self._core, write=write):
                decision("lines", "A sheet of lines was verified.", line_assignment_id=row["id"])
            return

        def reset(tx: Transaction, _s: object) -> None:
            # Rejected or taken off the lock: still owed; a new sheet is asked for when the slot is free again.
            tx.execute("UPDATE line_assignments SET task_id = NULL, review_status = ? WHERE id = ? AND status = 'open'", (status, row["id"]))
            write_event(tx, DomainEvent(event_type="lines.sheet_not_accepted", source_module="lines", occurred_at=now,
                                        payload={"line_assignment_id": row["id"], "status": status}))
            if status == "rejected":
                enqueue_in_transaction(tx, user_id=self.user_id, kind="lines", dedupe_key=f"lines:rejected:{row['id']}:{row['task_id']}", now=now,
                                       immediate=True, text="Your sheet of lines was rejected. Write them again; a new task follows.")

        apply_transition(self._core, write=reset)
        decision("lines", "A sheet of lines was not accepted; a new one will be asked for.", line_assignment_id=row["id"], status=status)

    def _set_review(self, row_id: str, status: str | None) -> None:
        def write(tx: Transaction, _s: object) -> None:
            tx.execute("UPDATE line_assignments SET review_status = ? WHERE id = ?", (status, row_id))

        apply_transition(self._core, write=write)

    async def paper_loop(self, clock, *, interval_seconds: float = 120.0) -> None:
        import asyncio
        while True:
            try:
                await asyncio.to_thread(self.paper_tick, clock.now())
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("The lines-on-paper pass failed; it continues.")
            await asyncio.sleep(interval_seconds)

    # ------------------------------------------------------------------ the wearer writes them (in the DM: no paper, or the fallback)

    def take(self, user_id: str, text: str, now: datetime) -> OutgoingMessage | None:
        """Counts the lines in a DM. None when nothing is owed or the message holds no line at all (then it is an ordinary message)."""
        if user_id != self.user_id or not text:
            return None
        written = [normalize(line) for line in text.splitlines() if line.strip()]
        if not written:
            return None
        if self.paper is not None and not self._typing_allowed(user_id, now):
            owed = {normalize(r["sentence"]) for r in self.get_open_assignments(user_id)}
            if any(w in owed for w in written):
                return OutgoingMessage(text="Lines go on paper: write them by hand and send the photo in the Chaster task. Typed ones don't count.")
            return None

        def write(tx: Transaction, _s: object):
            rows = [dict(r) for r in tx.fetch_all("SELECT id, sentence, required, done FROM line_assignments WHERE user_id = ? AND status = 'open' "
                                                  "ORDER BY created_at, rowid", (user_id,))]
            if not rows:
                return None
            wanted = {normalize(r["sentence"]) for r in rows}
            if not any(w in wanted for w in written):
                return None
            matched, wrong, finished = 0, 0, []
            remaining = list(written)
            for row in rows:
                target = normalize(row["sentence"])
                need = row["required"] - row["done"]
                hits = 0
                rest = []
                for w in remaining:
                    if w == target and hits < need:
                        hits += 1
                    else:
                        rest.append(w)
                remaining = rest
                if hits:
                    matched += hits
                    complete = row["done"] + hits >= row["required"]
                    tx.execute("UPDATE line_assignments SET done = done + ?, status = ?, completed_at = ? WHERE id = ? AND status = 'open'",
                               (hits, "done" if complete else "open", _iso(now) if complete else None, row["id"]))
                    if complete:
                        finished.append(row["id"])
                        write_event(tx, DomainEvent(event_type="lines.completed", source_module="lines", occurred_at=now,
                                                    payload={"line_assignment_id": row["id"]}))
            wrong = sum(1 for w in remaining if w not in wanted)
            left = tx.fetch_one("SELECT COALESCE(SUM(required - done), 0) AS n FROM line_assignments WHERE user_id = ? AND status = 'open'",
                                (user_id,))["n"]
            return matched, wrong, finished, int(left)

        result = apply_transition(self._core, write=write)
        if result is None:
            return None
        matched, wrong, finished, left = result
        note = f" {wrong} line{'s' if wrong != 1 else ''} didn't match exactly and didn't count." if wrong else ""
        if left == 0:
            decision("lines", "All lines done.", assignments=len(finished))
            return OutgoingMessage(text=f"{matched} counted. That's all of them. Good.{note}")
        return OutgoingMessage(text=f"{matched} counted, {left} to go.{note}")
