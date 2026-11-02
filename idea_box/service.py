"""
idea_box/service.py

IdeaBox (v0.39 Phase F3; table `ai_suggestions`, migration 037). A one-way channel from the model to the keyholder.

WRITE side (`submit`, `sink`): the conversation engine's structured-output sink hands it a validated `submit_suggestion` (type rule_tweak | new_punishment |
system_flaw, at most 500 characters), only for trusted turns. Code-level checks: nothing touching health, medical matters, trauma, self-harm or distress (in the
suggestion or in the user's message of that turn) is stored; 5 per user per day; duplicates suppressed.

READ side (`list`, `review`): for the OWNER only, through the local CLI. Nothing that builds model context may import this package or query the table; tests
guard that. So a suggestion can never come back to the model and be mistaken for an active rule. Reviewing ("accepted") changes nothing by itself: adopting an
idea is a separate change that needs the affected wearer's consent.
"""

from __future__ import annotations

import logging
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path

from coaching_memory.service import excluded_text
from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.time_format import iso as _iso
from infrastructure.time_format import parse_iso as _parse_iso

logger = logging.getLogger("ai_coach_keyholder.idea_box")

__all__ = ["IdeaBox", "Idea", "SubmitOutcome", "SUGGESTION_TYPES", "STATUSES", "MAX_CONTENT", "MAX_PER_DAY"]

SUGGESTION_TYPES = ("rule_tweak", "new_punishment", "system_flaw")
STATUSES = ("new", "accepted", "rejected", "archived")
MAX_CONTENT = 500
MAX_PER_DAY = 5


class SubmitOutcome(StrEnum):
    STORED = "stored"
    INVALID = "invalid"
    EXCLUDED = "excluded"
    DUPLICATE = "duplicate"
    DAILY_LIMIT = "daily_limit"


@dataclass(frozen=True)
class Idea:
    id: str
    user_id: str
    suggestion_type: str
    content: str
    status: str
    owner_note: str | None
    created_at: datetime
    reviewed_at: datetime | None

    @property
    def short_id(self) -> str:
        return self.id[:8]


def _key(text: str) -> str:
    folded = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", folded.lower()).strip()


class IdeaBox:
    def __init__(self, db_path: str | Path, *, core: CoreDatabase | None = None, max_per_day: int = MAX_PER_DAY) -> None:
        self.db_path = Path(db_path)
        self._core = core if core is not None else CoreDatabase(self.db_path)
        self._daily = max_per_day

    # ------------------------------------------------------------------ write (model side, validated)

    def submit(self, *, user_id: str, suggestion_type: str, content: str, user_message: str | None, now: datetime) -> SubmitOutcome:
        text = (content or "").strip()
        if suggestion_type not in SUGGESTION_TYPES or not text or len(text) > MAX_CONTENT or not user_id:
            return SubmitOutcome.INVALID
        if excluded_text(text) or excluded_text(user_message):
            return SubmitOutcome.EXCLUDED
        key = _key(text)

        def write(tx: Transaction, _s: object) -> SubmitOutcome:
            if tx.fetch_one("SELECT 1 FROM ai_suggestions WHERE user_id = ? AND content_key = ?", (user_id, key)):
                return SubmitOutcome.DUPLICATE
            if tx.fetch_one("SELECT COUNT(*) AS n FROM ai_suggestions WHERE user_id = ? AND created_at >= ?",
                            (user_id, _iso(now - timedelta(days=1))))["n"] >= self._daily:
                return SubmitOutcome.DAILY_LIMIT
            tx.execute("INSERT INTO ai_suggestions (id, user_id, suggestion_type, content, content_key, status, created_at) "
                       "VALUES (?, ?, ?, ?, ?, 'new', ?)", (str(uuid.uuid4()), user_id, suggestion_type, text, key, _iso(now)))
            return SubmitOutcome.STORED

        return apply_transition(self._core, write=write)

    def sink(self, subject_key: str, parsed, now: datetime, user_message: str | None = None) -> None:
        suggestion = getattr(parsed, "suggestion", None)
        if suggestion is None:
            return
        outcome = self.submit(user_id=subject_key, suggestion_type=suggestion.type, content=suggestion.note,
                              user_message=user_message, now=now)
        if outcome is not SubmitOutcome.STORED:
            logger.info("Suggestion not stored", extra={"outcome": outcome.value})

    # ------------------------------------------------------------------ read and review (OWNER only, via the CLI)

    def list(self, *, status: str | None = "new") -> list[Idea]:
        if status is not None and status not in STATUSES:
            raise ValueError("unknown status")
        with self._core.transaction() as tx:
            rows = tx.fetch_all("SELECT * FROM ai_suggestions" + ("" if status is None else " WHERE status = ?") + " ORDER BY created_at, rowid",
                                () if status is None else (status,))
        return [_idea(r) for r in rows]

    def review(self, short_id: str, *, status: str, note: str | None, now: datetime) -> bool:
        """Sets the owner's verdict on ONE suggestion (unambiguous id prefix, at least 4 characters). Changes nothing else."""
        if status not in ("accepted", "rejected", "archived"):
            raise ValueError("status must be accepted, rejected or archived")
        prefix = (short_id or "").strip().lower()
        if len(prefix) < 4 or not re.fullmatch(r"[0-9a-f-]+", prefix):
            return False

        def write(tx: Transaction, _s: object) -> bool:
            rows = tx.fetch_all("SELECT id FROM ai_suggestions WHERE id LIKE ?", (prefix + "%",))
            if len(rows) != 1:
                return False
            tx.execute("UPDATE ai_suggestions SET status = ?, owner_note = ?, reviewed_at = ? WHERE id = ?",
                       (status, (note or None) and note.strip()[:500], _iso(now), rows[0]["id"]))
            return True

        return bool(apply_transition(self._core, write=write))


def _idea(row) -> Idea:
    return Idea(id=row["id"], user_id=row["user_id"], suggestion_type=row["suggestion_type"], content=row["content"], status=row["status"],
                owner_note=row["owner_note"], created_at=_parse_iso(row["created_at"]),
                reviewed_at=_parse_iso(row["reviewed_at"]) if row["reviewed_at"] else None)
