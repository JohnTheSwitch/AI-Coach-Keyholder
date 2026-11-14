"""
lines/service.py

Lines (owner, 2026-11-14; brat taming B2; migration 066). Every confirmed incident gets lines: LINES_WARNING for a Warning-level incident,
LINES_WINDOW for one that opens or extends a Penalty Window, with a sentence that matches the rule broken. The wearer types them in the bot
DM; code counts them (no model): a line counts when it is the sentence exactly, ignoring case, spacing, a leading number ("12." / "12)")
and the closing full stop. Several lines per message, several messages per assignment; extra lines go to the next open assignment.

While lines are open (`open_lines`), the next planned hygiene break waits (exemptions/service.py) and, as release debt (roadmap B4), a token
cannot be spent on a release. A health need never waits for anything here (philosophy 3.9).

Assigned from the Penalty Engine's own events, in the same transaction that consumes them (a consumer: only the payload is used):
`penalty_warning.recorded` and `extension.decision_recorded`. One assignment per incident, whatever replays.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime

from application.models import OutgoingMessage
from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.decision_log import decision
from infrastructure.outbox import DomainEvent, write_event
from infrastructure.time_format import iso as _iso
from notifications.queue import enqueue_in_transaction

logger = logging.getLogger("ai_coach_keyholder.lines")

__all__ = ["LineService", "LINES_WARNING", "LINES_WINDOW", "SENTENCES", "sentence_for", "normalize"]

LINES_WARNING = 20              # owner, 2026-11-14
LINES_WINDOW = 40               # owner, 2026-11-14
DEFAULT_SENTENCE = "I follow the rules I agreed to."
SENTENCES = {
    "HYGIENE_MISUSE": "I use hygiene breaks only for hygiene.",
    "CHECKIN_MISSED_FIRST": "I answer my check-ins on time.",
    "CHECKIN_INSUFFICIENT": "I answer my check-ins properly.",
    "CHECKIN_REPEATED_NONCOMPLIANCE": "I answer my check-ins on time.",
    "DETECTION_UNANSWERED": "I answer my keyholder's questions.",
    "EXEMPTION_OVERRIDE": "A declined exemption means no.",
    "FALSE_REPORT": "I report only what I really did.",
    "EPISTEMIC_MISREPRESENTATION": "I tell the truth about my lock.",
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
        enqueue_in_transaction(tx, user_id=self.user_id, kind="lines", dedupe_key=f"lines:{incident_id}", now=event.occurred_at,
                               text=(f"Lines: write this {required} times, one per line, here in our chat:\n\"{sentence}\"\n"
                                     "Until they're done, your next planned hygiene break and any release wait. A health need never waits."))
        decision("lines", f"{required} lines assigned for a confirmed incident.", incident_id=incident_id, rule_group_id=rule)

    # ------------------------------------------------------------------ reads

    def open_lines(self, user_id: str) -> int:
        """How many lines are still owed (0 when none)."""
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT COALESCE(SUM(required - done), 0) AS n FROM line_assignments WHERE user_id = ? AND status = 'open'", (user_id,))
        return int(row["n"])

    def get_open_assignments(self, user_id: str) -> list[dict]:
        with self._core.transaction() as tx:
            return [dict(r) for r in tx.fetch_all("SELECT id, sentence, required, done FROM line_assignments WHERE user_id = ? AND status = 'open' "
                                                  "ORDER BY created_at, rowid", (user_id,))]

    # ------------------------------------------------------------------ the wearer writes them

    def take(self, user_id: str, text: str, now: datetime) -> OutgoingMessage | None:
        """Counts the lines in a DM. None when nothing is owed or the message holds no line at all (then it is an ordinary message)."""
        if user_id != self.user_id or not text:
            return None
        written = [normalize(line) for line in text.splitlines() if line.strip()]
        if not written:
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
