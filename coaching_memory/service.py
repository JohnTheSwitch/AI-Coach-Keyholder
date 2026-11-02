"""
coaching_memory/service.py

Coaching Memory (v0.39 Phase F2; table `wearer_dossier`, migration 036). Short notes about the user's OWN behaviour patterns, stated goals and quotes that the
model may save through the validated structured-output path, so it can coach with continuity.

Every write passes, IN CODE (the prompt rules are only a first line):
- the category and length checks of the structured parser (behavior_pattern | stated_goal | quote, at most 300 characters);
- the EXCLUSION filter: nothing about health, medical matters, injury, trauma, abuse, self-harm, suicide or distress is stored, and nothing at all is stored
  from a turn whose user message touched those topics (`excluded_text`);
- an injection filter: notes that look like instructions, rules or permissions, or that contain markup, are refused, because entries are later shown to the
  model (inside a fenced data block, but defence in depth);
- a daily write limit, a total cap, and duplicate suppression.

The user has full control: `entries()` lists them, `forget()` / `forget_all()` DELETE them. Entries never leave this tenant's database.
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

from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.time_format import iso as _iso
from infrastructure.time_format import parse_iso as _parse_iso

logger = logging.getLogger("ai_coach_keyholder.coaching_memory.service")

__all__ = ["CoachingMemoryService", "MemoryEntry", "WriteOutcome", "excluded_text", "looks_like_instruction",
           "CATEGORIES", "MAX_CONTENT", "MAX_WRITES_PER_DAY", "MAX_ENTRIES", "RETRIEVE_LIMIT"]

CATEGORIES = ("behavior_pattern", "stated_goal", "quote")
MAX_CONTENT = 300
MAX_WRITES_PER_DAY = 5
MAX_ENTRIES = 200
RETRIEVE_LIMIT = 5

# English and Czech (diacritics folded). Deliberately broad: a false positive only means a note is not saved.
_EXCLUDED = re.compile(
    r"\b(health\w*|medical\w*|medic\w*|doctor\w*|hospital\w*|clinic\w*|nurse\w*|therap\w*|psychiatr\w*|diagnos\w*|symptom\w*|"
    r"medication\w*|medicine\w*|pills?|drugs?|prescri\w*|ill|illness\w*|sick\w*|disease\w*|infection\w*|fever\w*|"
    r"pain\w*|hurts?|hurting|injur\w*|wound\w*|bleed\w*|blood\w*|swollen|swelling|numb|numbness|dizz\w*|faint\w*|chest|breath\w*|"
    r"trauma\w*|abus\w*|assault\w*|rape\w*|ptsd|flashback\w*|"
    r"suicid\w*|self[- ]?harm\w*|kill (myself|me)|cut(ting)? myself|hurt(ing)? myself|end (it|my life)|want to die|"
    r"distress\w*|panic\w*|anxiety|anxious|depress\w*|hopeless\w*|crying|cried|terrified|scared|afraid|breakdown|"
    r"emergency|ambulance|"
    r"bolest\w*|boli|nemoc\w*|nemocnice|doktor\w*|lekar\w*|lek|leky|zraneni\w*|krev|krvac\w*|uraz\w*|deprese|depresivni|"
    r"uzkost\w*|panik\w*|sebevrazd\w*|sebeposkoz\w*|zachranka|nouz\w*)\b",
    re.IGNORECASE,
)
_INSTRUCTION = re.compile(
    r"(<|>|\{|\}|\bignore\b|\binstruction\w*|\bsystem prompt\b|\bprompt\b|\byou (must|should|are allowed|may)\b|"
    r"\b(allow\w*|permit\w*|grant\w*) (release|unlock|early)\b|\brule\w*\b|\bkeyholder (says|said|allows|agreed)\b|"
    r"\bfrom now on\b|\balways (release|unlock)\b|\bnever (punish|lock)\b)",
    re.IGNORECASE,
)


def _fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def excluded_text(text: str | None) -> bool:
    """True when the text touches health, medical matters, trauma, self-harm or distress (nothing from it may be stored)."""
    return bool(text) and _EXCLUDED.search(_fold(text)) is not None


def looks_like_instruction(text: str) -> bool:
    return _INSTRUCTION.search(_fold(text)) is not None


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", _fold(text).lower()).strip()


class WriteOutcome(StrEnum):
    STORED = "stored"
    INVALID = "invalid"
    EXCLUDED = "excluded"                 # health / trauma / self-harm / distress in the note or in the user's turn
    INSTRUCTION_LIKE = "instruction_like"
    DUPLICATE = "duplicate"
    DAILY_LIMIT = "daily_limit"
    FULL = "full"


@dataclass(frozen=True)
class MemoryEntry:
    id: str
    category: str
    content: str
    created_at: datetime

    @property
    def short_id(self) -> str:
        return self.id[:8]


class CoachingMemoryService:
    def __init__(self, db_path: str | Path, *, core: CoreDatabase | None = None, max_writes_per_day: int = MAX_WRITES_PER_DAY,
                 max_entries: int = MAX_ENTRIES) -> None:
        self.db_path = Path(db_path)
        self._core = core if core is not None else CoreDatabase(self.db_path)
        self._daily = max_writes_per_day
        self._cap = max_entries

    # ------------------------------------------------------------------ writes (model side, validated)

    def record(self, *, user_id: str, category: str, content: str, user_message: str | None, now: datetime) -> WriteOutcome:
        text = (content or "").strip()
        if category not in CATEGORIES or not text or len(text) > MAX_CONTENT or not user_id:
            return WriteOutcome.INVALID
        if excluded_text(text) or excluded_text(user_message):
            return WriteOutcome.EXCLUDED
        if looks_like_instruction(text):
            return WriteOutcome.INSTRUCTION_LIKE
        key = _key(text)

        def write(tx: Transaction, _s: object) -> WriteOutcome:
            if tx.fetch_one("SELECT 1 FROM wearer_dossier WHERE user_id = ? AND content_key = ?", (user_id, key)):
                return WriteOutcome.DUPLICATE
            if tx.fetch_one("SELECT COUNT(*) AS n FROM wearer_dossier WHERE user_id = ? AND created_at >= ?",
                            (user_id, _iso(now - timedelta(days=1))))["n"] >= self._daily:
                return WriteOutcome.DAILY_LIMIT
            if tx.fetch_one("SELECT COUNT(*) AS n FROM wearer_dossier WHERE user_id = ?", (user_id,))["n"] >= self._cap:
                return WriteOutcome.FULL
            tx.execute("INSERT INTO wearer_dossier (id, user_id, category, content, content_key, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                       (str(uuid.uuid4()), user_id, category, text, key, _iso(now)))
            return WriteOutcome.STORED

        return apply_transition(self._core, write=write)

    def sink(self, subject_key: str, parsed, now: datetime, user_message: str | None = None) -> None:
        """The structured-output sink of the conversation engine: stores a valid memory note, ignores everything else.
        Logs outcome codes only, never content."""
        note = getattr(parsed, "memory_note", None)
        if note is None:
            return
        outcome = self.record(user_id=subject_key, category=note.category, content=note.note, user_message=user_message, now=now)
        if outcome is not WriteOutcome.STORED:
            logger.info("Coaching memory note not stored", extra={"outcome": outcome.value})

    # ------------------------------------------------------------------ reads and the user's control

    def recent(self, user_id: str, *, limit: int = RETRIEVE_LIMIT) -> list[MemoryEntry]:
        with self._core.transaction() as tx:
            rows = tx.fetch_all("SELECT * FROM wearer_dossier WHERE user_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
                                (user_id, limit))
        return [_entry(r) for r in rows]

    def entries(self, user_id: str) -> list[MemoryEntry]:
        with self._core.transaction() as tx:
            rows = tx.fetch_all("SELECT * FROM wearer_dossier WHERE user_id = ? ORDER BY created_at, rowid", (user_id,))
        return [_entry(r) for r in rows]

    def forget(self, user_id: str, short_id: str) -> int:
        """Deletes the user's entry whose id starts with `short_id` (at least 4 characters, must be unambiguous). Returns how many were deleted (0 or 1)."""
        prefix = (short_id or "").strip().lower()
        if len(prefix) < 4 or not re.fullmatch(r"[0-9a-f-]+", prefix):
            return 0

        def write(tx: Transaction, _s: object) -> int:
            rows = tx.fetch_all("SELECT id FROM wearer_dossier WHERE user_id = ? AND id LIKE ?", (user_id, prefix + "%"))
            if len(rows) != 1:
                return 0
            tx.execute("DELETE FROM wearer_dossier WHERE id = ? AND user_id = ?", (rows[0]["id"], user_id))
            return 1

        return apply_transition(self._core, write=write)

    def forget_all(self, user_id: str) -> int:
        def write(tx: Transaction, _s: object) -> int:
            return tx.execute("DELETE FROM wearer_dossier WHERE user_id = ?", (user_id,)).rowcount

        return apply_transition(self._core, write=write)


def _entry(row) -> MemoryEntry:
    return MemoryEntry(id=row["id"], category=row["category"], content=row["content"], created_at=_parse_iso(row["created_at"]))
