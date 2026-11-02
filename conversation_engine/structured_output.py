"""
conversation_engine/structured_output.py

Structured model output (v0.39 Phase F1): the JSON shape the model is asked for, the instructions that describe it, and the STRICT parser.

    {"reply_to_user": "...",
     "save_to_memory": {"category": "behavior_pattern|stated_goal|quote", "note": "..."} | null,
     "submit_suggestion": {"type": "rule_tweak|new_punishment|system_flaw", "note": "..."} | null}

Rules (all tested):
- The reply is the ONLY thing that can reach the user. Without a valid reply the whole output is unusable (the engine then falls back to a plain reply).
- The two side fields are optional data. Anything malformed in them is DROPPED (recorded as an issue code), never "repaired"; they can never carry a domain
  action: there is no field for Trust, penalties, tokens, rules, locks, halts or messages to anyone.
- Unknown keys, wrong types, unknown enum values and over-long texts are rejected. A code fence around the JSON is tolerated; any other text is not.
- In Phase F1 the side fields have NO effect: the engine hands them to an optional sink, and none is installed until Coaching Memory (F2) and the idea box (F3).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

__all__ = [
    "MEMORY_CATEGORIES", "SUGGESTION_TYPES", "MAX_MEMORY_NOTE", "MAX_SUGGESTION_NOTE", "RESPONSE_SCHEMA", "FORMAT_INSTRUCTIONS",
    "MemoryNote", "Suggestion", "StructuredReply", "parse_structured_reply",
]

MEMORY_CATEGORIES = ("behavior_pattern", "stated_goal", "quote")
SUGGESTION_TYPES = ("rule_tweak", "new_punishment", "system_flaw")
MAX_MEMORY_NOTE = 300
MAX_SUGGESTION_NOTE = 500

_NOTE = {"type": "string"}
RESPONSE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "reply_to_user": {"type": "string"},
        "save_to_memory": {
            "anyOf": [
                {"type": "null"},
                {"type": "object", "properties": {"category": {"type": "string", "enum": list(MEMORY_CATEGORIES)}, "note": _NOTE},
                 "required": ["category", "note"], "additionalProperties": False},
            ],
        },
        "submit_suggestion": {
            "anyOf": [
                {"type": "null"},
                {"type": "object", "properties": {"type": {"type": "string", "enum": list(SUGGESTION_TYPES)}, "note": _NOTE},
                 "required": ["type", "note"], "additionalProperties": False},
            ],
        },
    },
    "required": ["reply_to_user", "save_to_memory", "submit_suggestion"],
    "additionalProperties": False,
}

FORMAT_INSTRUCTIONS = (
    "OUTPUT FORMAT. Answer with ONE JSON object and nothing else:\n"
    '{"reply_to_user": string, "save_to_memory": null or {"category": string, "note": string}, '
    '"submit_suggestion": null or {"type": string, "note": string}}\n'
    "- reply_to_user: your message to the user. Every rule above applies to it unchanged.\n"
    f"- save_to_memory: usually null. Only for something worth remembering about the user's own behaviour or goals, category one of "
    f"{', '.join(MEMORY_CATEGORIES)}, at most {MAX_MEMORY_NOTE} characters. NEVER anything about health, medical matters, trauma, "
    "self-harm or distress, and never anything about other people.\n"
    f"- submit_suggestion: usually null. A private idea for the keyholder about the regime, type one of {', '.join(SUGGESTION_TYPES)}, "
    f"at most {MAX_SUGGESTION_NOTE} characters. It is never shown to the user and never becomes a rule by itself.\n"
    "Neither field changes anything for the user, and you must not mention them in reply_to_user."
)


@dataclass(frozen=True)
class MemoryNote:
    category: str
    note: str


@dataclass(frozen=True)
class Suggestion:
    type: str
    note: str


@dataclass(frozen=True)
class StructuredReply:
    reply: str
    memory_note: MemoryNote | None = None
    suggestion: Suggestion | None = None
    issues: tuple[str, ...] = field(default=())          # codes only (never content), for logs and evaluation

    @property
    def has_side_data(self) -> bool:
        return self.memory_note is not None or self.suggestion is not None


def _strip_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        inner = stripped[3:-3]
        if inner.lower().startswith("json"):
            inner = inner[4:]
        return inner.strip()
    return stripped


def _side(value, *, kind_key: str, allowed: tuple[str, ...], max_len: int, name: str, issues: list[str]):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {kind_key, "note"}:
        issues.append(f"{name}_shape")
        return None
    kind, note = value.get(kind_key), value.get("note")
    if not isinstance(kind, str) or kind not in allowed:
        issues.append(f"{name}_kind")
        return None
    if not isinstance(note, str) or not note.strip():
        issues.append(f"{name}_empty")
        return None
    note = note.strip()
    if len(note) > max_len:
        issues.append(f"{name}_too_long")
        return None
    return kind, note


def parse_structured_reply(raw: str, *, max_reply_characters: int) -> StructuredReply | None:
    """None when there is no usable reply (not JSON, not an object, unknown keys, missing / empty / too long reply).
    Otherwise the reply plus whatever side data was valid."""
    if not isinstance(raw, str):
        return None
    try:
        data = json.loads(_strip_fence(raw))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    allowed_keys = {"reply_to_user", "save_to_memory", "submit_suggestion"}
    if not set(data) <= allowed_keys or "reply_to_user" not in data:
        return None
    reply = data["reply_to_user"]
    if not isinstance(reply, str) or not reply.strip() or len(reply.strip()) > max_reply_characters:
        return None
    issues: list[str] = []
    memory = _side(data.get("save_to_memory"), kind_key="category", allowed=MEMORY_CATEGORIES, max_len=MAX_MEMORY_NOTE,
                   name="memory", issues=issues)
    suggestion = _side(data.get("submit_suggestion"), kind_key="type", allowed=SUGGESTION_TYPES, max_len=MAX_SUGGESTION_NOTE,
                       name="suggestion", issues=issues)
    return StructuredReply(
        reply=reply.strip(),
        memory_note=None if memory is None else MemoryNote(*memory),
        suggestion=None if suggestion is None else Suggestion(*suggestion),
        issues=tuple(issues),
    )
