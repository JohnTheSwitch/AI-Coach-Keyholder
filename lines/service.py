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
from application.models import ActionSpec
from notifications.queue import enqueue_in_transaction

logger = logging.getLogger("ai_coach_keyholder.lines")

__all__ = ["LineService", "PaperPort", "RELEASE_GROUP", "RELEASE_SENTENCE", "RELEASE_LINES", "LINES_WARNING", "LINES_WINDOW", "SENTENCES", "sentence_for", "normalize", "FALLBACK_AFTER"]

LINES_WARNING = 20              # owner, 2026-11-14
LINES_WINDOW = 40               # owner, 2026-11-14
RELEASE_GROUP = "RELEASE"          # owner, 2026-11-15 (earn the release): lines that earn a release, not a punishment
RELEASE_SENTENCE = "I am grateful to my keyholder for every release I earn."
RELEASE_LINES = 20
SWATS_WITHIN = timedelta(hours=2)      # owner, 2026-11-16 (B1): the swats photo is due this long after the task is in Chaster
REQUEST_MIN_WORDS = 30                 # B2: each of the written request's two answers (code counts words only)
RELEASE_OPTIONS = ("lines", "swats", "request")   # B2: his choice before a release, never the one he did last time
FALLBACK_AFTER = timedelta(hours=3)    # a sheet that waited this long for Chaster's task slot may be typed in the DM instead


@dataclass(frozen=True, kw_only=True)
class PaperPort:
    chaster: object                                  # proof_checks.chaster_tasks.ChasterTasks (one lock, one task slot)
    lock_id: Callable[[datetime], str | None]
    halted: Callable[[], bool]
    slot_taken: Callable[[], bool]                   # an open spot check or recovery task
# Owner, 2026-11-15: the more submissive wording he approved (counts unchanged; an open assignment keeps its own sentence).
DEFAULT_SENTENCE = "I obey the rules my keyholder sets for me."
SENTENCES = {
    "HYGIENE_MISUSE": "My keyholder trusts me with my breaks, and I use them only for hygiene.",
    "CHECKIN_MISSED_FIRST": "I answer my keyholder on time, every time.",
    "CHECKIN_INSUFFICIENT": "I give my keyholder full and honest answers.",
    "CHECKIN_REPEATED_NONCOMPLIANCE": "I do not make my keyholder wait for me.",
    "DETECTION_UNANSWERED": "When my keyholder asks, I answer.",
    "EXEMPTION_OVERRIDE": "My keyholder's no is final, and I accept it.",
    "FALSE_REPORT": "I never lie to my keyholder about what I did.",
    "OBEDIENCE_TASK_MISSED": "I obey my keyholder without excuses.",
    "HYGIENE_PICTURE_MISSING": "I prove to my keyholder that I am locked again.",
    "RELEASE_WINDOW_OVERRUN": "I relock on time when my keyholder says so.",
    "EPISTEMIC_MISREPRESENTATION": "I am honest with my keyholder about my cage.",
    "FALSE_HEALTH_CLAIM": "I never use my health to escape my keyholder.",
}
_NUMBER = re.compile(r"^\s*\d{1,3}\s*[.):-]\s*")
_QUOTES = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"'})


REQUEST_ASK = ("Write me a request, in two messages of at least 30 words each: first why you earned this release, then what you will do "
               "better this week. I keep what you promise.")
_OPTION_LABEL = {"lines": "Lines", "swats": "Swats instead", "request": "Written request"}


def swats_for(lines: int) -> int:
    """B1 (owner, 2026-11-15): one light swat for every two lines."""
    return max(1, (lines + 1) // 2)


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
                                     "Until they're done, your next planned hygiene break and any release wait. A health need never waits.\n"
                                     f"Or, your choice: {swats_for(required)} light swats instead."),
                               actions=(ActionSpec(action_id=f"lines:swats:{line_id}", label="Swats instead"),))
        decision("lines", f"{required} lines assigned for a confirmed incident.", incident_id=incident_id, rule_group_id=rule)

    # ------------------------------------------------------------------ reads

    def open_lines(self, user_id: str) -> int:
        """How many lines are still owed (0 when none)."""
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT COALESCE(SUM(required - done), 0) AS n FROM line_assignments WHERE user_id = ? AND status = 'open' "
                               "AND COALESCE(rule_group_id, '') != ?", (user_id, RELEASE_GROUP))
        return int(row["n"])             # release lines are not owed consequences: they never hold hygiene or count as debt

    def get_lines_between(self, start, end) -> list[dict]:
        """Work queue Q4 (weekly review): assignments created on local dates start..end (UTC dates are close enough for a weekly count)."""
        with self._core.transaction() as tx:
            return [dict(r) for r in tx.fetch_all("SELECT required, done, status FROM line_assignments WHERE user_id = ? AND substr(created_at, 1, 10) "
                                                  "BETWEEN ? AND ? AND COALESCE(rule_group_id, '') != ?",
                                                  (self.user_id, start.isoformat(), end.isoformat(), RELEASE_GROUP))]

    def assign_release(self, since: str, now: datetime) -> bool:
        """Earn the release (owner, 2026-11-15): RELEASE_LINES release lines, unless some were assigned since `since` (the last spent token).
        True when they were assigned now. On paper like any lines when `paper` is wired."""
        def write(tx: Transaction, _s: object) -> bool:
            if tx.fetch_one("SELECT 1 FROM line_assignments WHERE user_id = ? AND rule_group_id = ? AND created_at >= ?",
                            (self.user_id, RELEASE_GROUP, since)) is not None:
                return False
            line_id, key = str(uuid.uuid4()), f"release:{uuid.uuid4()}"
            allowed, default = self._release_choices(tx)
            tx.execute("INSERT INTO line_assignments (id, user_id, incident_id, rule_group_id, sentence, required, done, status, created_at, mode) "
                       "VALUES (?, ?, ?, ?, ?, ?, 0, 'open', ?, ?)", (line_id, self.user_id, key, RELEASE_GROUP, RELEASE_SENTENCE, RELEASE_LINES,
                                                                   _iso(now), None if default == "lines" else default))
            write_event(tx, DomainEvent(event_type="lines.assigned", source_module="lines", occurred_at=now,
                                        payload={"line_assignment_id": line_id, "incident_id": key, "required": RELEASE_LINES}))
            how = ("by hand on paper. A Chaster task will ask for a photo of the sheet" if self.paper is not None else "one per line, here in our chat")
            if default == "lines":
                text = (f"A release is within reach, but you earn it first: write this {RELEASE_LINES} times, {how}:\n"
                        f"\"{RELEASE_SENTENCE}\"\nOnce they're verified, your release is planned.")
            else:
                text = f"A release is within reach, but you earn it first. Lines were last time, so: {REQUEST_ASK}"
            others = [o for o in allowed if o != default]
            enqueue_in_transaction(tx, user_id=self.user_id, kind="lines", dedupe_key=f"lines:{key}", now=now,
                                   text=text + (" Or choose another way below." if others else ""),
                                   actions=tuple(ActionSpec(action_id=f"lines:{o}:{line_id}", label=_OPTION_LABEL[o]) for o in others))
            return True

        done = apply_transition(self._core, write=write)
        if done:
            decision("lines", "Release lines assigned: a token is ready, the release waits for them.")
        return bool(done)

    def _release_choices(self, tx: Transaction) -> tuple[tuple[str, ...], str]:
        """B2: the options before a release (never the one done last time) and the default (lines, or the written request after lines)."""
        last = tx.fetch_one("SELECT mode FROM line_assignments WHERE user_id = ? AND rule_group_id = ? AND status = 'done' "
                            "ORDER BY completed_at DESC, rowid DESC LIMIT 1", (self.user_id, RELEASE_GROUP))
        last_mode = None if last is None else (last["mode"] or "lines")
        allowed = tuple(o for o in RELEASE_OPTIONS if o != last_mode)
        return allowed, ("lines" if "lines" in allowed else "request")

    def get_promises_between(self, start: str, end: str) -> list[str]:
        """Read-only (B2, the Sunday review): what he wrote he would do better, in written requests completed in [start, end)."""
        with self._core.transaction() as tx:
            return [r["request_better"] for r in tx.fetch_all(
                "SELECT request_better FROM line_assignments WHERE user_id = ? AND mode = 'request' AND status = 'done' AND completed_at >= ? "
                "AND completed_at < ? ORDER BY completed_at", (self.user_id, start, end)) if r["request_better"]]

    # ------------------------------------------------------------------ his choice: swats (B1) or the written request (B2)

    def press(self, *, user_id: str, row_id: str, action: str, now: datetime) -> str:
        if user_id != self.user_id or action not in ("swats", "request", "lines"):
            return "That button isn't for you."
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT * FROM line_assignments WHERE id = ? AND user_id = ?", (row_id, user_id))
            allowed = self._release_choices(tx)[0] if row is not None and row["rule_group_id"] == RELEASE_GROUP else ("lines", "swats")
        if row is None or row["status"] != "open":
            return "Those lines are already done."
        mode = None if action == "lines" else action
        if action not in allowed or (row["mode"] or None) == mode:
            return "That choice isn't open for these."
        if row["task_id"] and self.paper is not None:
            try:
                current = self.paper.chaster.current_task(row["lock_id"])
                if current is not None and current.get("id") == row["task_id"]:
                    self.paper.chaster.unassign(row["lock_id"])
            except Exception:
                logger.exception("The old lines task could not be taken back in Chaster.")

        def write(tx: Transaction, _s: object) -> bool:
            done = tx.execute("UPDATE line_assignments SET mode = ?, mode_chosen_at = ?, task_id = NULL, review_status = NULL "
                              "WHERE id = ? AND status = 'open'", (mode, _iso(now), row_id)).rowcount
            if done:
                write_event(tx, DomainEvent(event_type="lines.mode_chosen", source_module="lines", occurred_at=now,
                                            payload={"line_assignment_id": row_id, "mode": action}))
            return bool(done)

        if not apply_transition(self._core, write=write):
            return "That choice isn't open for these."
        decision("lines", "He chose how to pay his lines.", line_assignment_id=row_id, mode=action)
        left = row["required"] - row["done"]
        if action == "swats":
            return (f"Swats instead: {swats_for(left)} light swats with your hand, each counted aloud with \"Thank you\". Red skin, never bruises; "
                    "stop if anything feels wrong. A Chaster task will ask for a photo of the skin only; you have 2 hours once it's there.")
        if action == "request":
            return REQUEST_ASK
        return f"Lines it is: \"{row['sentence']}\" {left} times."

    def _take_request(self, user_id: str, text: str, now: datetime) -> OutgoingMessage | None:
        """B2: an open written request takes his answers (at least REQUEST_MIN_WORDS words each); a shorter message is an ordinary one."""
        if len(text.split()) < REQUEST_MIN_WORDS:
            return None

        def write(tx: Transaction, _s: object):
            row = tx.fetch_one("SELECT * FROM line_assignments WHERE user_id = ? AND status = 'open' AND mode = 'request' ORDER BY created_at LIMIT 1",
                               (user_id,))
            if row is None:
                return None
            if not row["request_why"]:
                tx.execute("UPDATE line_assignments SET request_why = ? WHERE id = ?", (text[:2000], row["id"]))
                return "why"
            tx.execute("UPDATE line_assignments SET request_better = ?, done = required, status = 'done', completed_at = ? WHERE id = ? AND status = 'open'",
                       (text[:2000], _iso(now), row["id"]))
            write_event(tx, DomainEvent(event_type="lines.completed", source_module="lines", occurred_at=now,
                                        payload={"line_assignment_id": row["id"], "mode": "request"}))
            return "done"

        step = apply_transition(self._core, write=write)
        if step is None:
            return None
        if step == "why":
            return OutgoingMessage(text="Noted. Now the second part: what will you do better this week? At least 30 words.")
        decision("lines", "A written request before a release was completed.")
        return OutgoingMessage(text="Your request is accepted, and I'll hold you to what you wrote. Your release can be planned now.")

    def release_done_since(self, since: str) -> bool:
        """Read-only: release lines assigned since `since` and completed."""
        with self._core.transaction() as tx:
            return tx.fetch_one("SELECT 1 FROM line_assignments WHERE user_id = ? AND rule_group_id = ? AND created_at >= ? AND status = 'done'",
                                (self.user_id, RELEASE_GROUP, since)) is not None

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
        rows = [r for r in rows if r.get("mode") != "request"]      # B2: a written request never goes on Chaster
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
        text = (f"{swats_for(left)} light hand swats, counted aloud. Photo of the skin only." if row.get("mode") == "swats"
                else f"{left} handwritten lines: \"{row['sentence']}\" Photo of the sheet only.")
        task_id = port.chaster.assign(lock_id, text[:200])

        def write(tx: Transaction, _s: object) -> None:
            tx.execute("UPDATE line_assignments SET lock_id = ?, task_id = ?, task_assigned_at = ?, review_status = NULL WHERE id = ? AND status = 'open'",
                       (lock_id, task_id, _iso(now), row["id"]))
            write_event(tx, DomainEvent(event_type="lines.sheet_assigned", source_module="lines", occurred_at=now,
                                        payload={"line_assignment_id": row["id"]}))
            enqueue_in_transaction(tx, user_id=self.user_id, kind="lines", dedupe_key=f"lines:sheet:{row['id']}:{task_id}", now=now,
                                   text=(f"Your swats are in Chaster now: {swats_for(left)} light swats, counted aloud, then a photo of the skin only, "
                                         "within 2 hours." if row.get("mode") == "swats" else
                                         f"Your lines are in Chaster now: write \"{row['sentence']}\" {left} times by hand, then submit a photo of the "
                                         "sheet with Chaster's code in the task."))

        apply_transition(self._core, write=write)
        decision("lines", "A sheet of lines was put on Chaster as a task.", line_assignment_id=row["id"], lines=left)

    def _read_sheet(self, row: dict, now: datetime) -> None:
        port = self.paper
        entry = port.chaster.task_entry(row["lock_id"], row["task_id"])
        if entry is None:
            current = port.chaster.current_task(row["lock_id"])
            if current is not None and current.get("id") == row["task_id"]:
                if row.get("mode") == "swats" and row["task_assigned_at"] and now > _parse_iso(row["task_assigned_at"]) + SWATS_WITHIN:
                    self._swats_back_to_lines(row, now, "not done in 2 hours")
                return                                       # still open, nothing submitted yet
            status = "removed"
        elif entry.get("status") == "removed":
            status = "removed"
        else:
            review = entry.get("peerVerification") if isinstance(entry.get("peerVerification"), dict) else {}
            status = review.get("status")
        if status not in ("verified", "rejected", "removed"):
            if row.get("mode") == "swats" and entry is None and row["task_assigned_at"] and now > _parse_iso(row["task_assigned_at"]) + SWATS_WITHIN:
                self._swats_back_to_lines(row, now, "not done in 2 hours")
                return
            if status != row["review_status"]:
                self._set_review(row["id"], status)
            return
        if row.get("mode") == "swats" and status != "verified":
            self._swats_back_to_lines(row, now, "rejected" if status == "rejected" else "taken off")
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

    def _swats_back_to_lines(self, row: dict, now: datetime, why: str) -> None:
        """B1: swats not done or not accepted: the lines come back unchanged, no new incident."""
        try:
            current = self.paper.chaster.current_task(row["lock_id"])
            if current is not None and current.get("id") == row["task_id"]:
                self.paper.chaster.unassign(row["lock_id"])
        except Exception:
            logger.exception("The swats task could not be taken back in Chaster.")

        def write(tx: Transaction, _s: object) -> None:
            if tx.execute("UPDATE line_assignments SET mode = NULL, task_id = NULL, review_status = NULL WHERE id = ? AND status = 'open'",
                          (row["id"],)).rowcount:
                write_event(tx, DomainEvent(event_type="lines.mode_chosen", source_module="lines", occurred_at=now,
                                            payload={"line_assignment_id": row["id"], "mode": "lines", "why": why}))
                enqueue_in_transaction(tx, user_id=self.user_id, kind="lines", dedupe_key=f"lines:swats_back:{row['id']}:{row['task_id']}", now=now,
                                       immediate=True, text=f"Your swats were {why}, so the lines are back: \"{row['sentence']}\" "
                                                            f"{row['required'] - row['done']} times.")

        apply_transition(self._core, write=write)
        decision("lines", "Swats not accepted: back to lines.", line_assignment_id=row["id"], why=why)

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
        request = self._take_request(user_id, text, now)
        if request is not None:
            return request
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
