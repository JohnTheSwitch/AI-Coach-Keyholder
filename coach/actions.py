"""
coach/actions.py

The CLOSED catalogue of what the language model may ask for (v0.39 Phase J3), and its strict validator. Pure: no database, no clock, no model.

There are exactly three actions and all of them are PROPOSALS: they put a card in front of the user, who decides with a button. There is no
action for a lock, time, a release, a token, a penalty, Trust, a rule, consent, a message to anyone, or a task assignment, and adding one means
adding it here, with a test. Anything else the model asks for is refused by name.

The request travels as a `proposal` field of the JSON reply, constrained by `ACTION_SCHEMAS` through Ollama's `format`. Native tool calling was
measured and rejected on the owner's models (2026-11-04): `llama3.1` called a tool for "hi" and proposed "I will immediately unlock you right
now" when told to, and it loses the reply text on every call. The schema is only a hint for the model; `validate_action` is the check. It
rejects, it never repairs: an unknown argument, a wrong type (a bool is not an integer, "1" is not an integer), an over-long or empty text, a
control or invisible character, a link.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping

__all__ = [
    "ActionKind", "ACTION_SCHEMAS", "ACTION_INSTRUCTIONS", "ValidatedAction", "Refusal", "validate_action", "MAX_TITLE", "MAX_TARGET",
    "MAX_COMMITMENT", "MAX_REASON", "MAX_GOAL_REF",
]

MAX_TITLE = 80
MAX_TARGET = 300
MAX_COMMITMENT = 200
MAX_REASON = 200
MAX_GOAL_REF = 9


class ActionKind(StrEnum):
    PROPOSE_GOAL = "propose_goal"
    ADAPT_GOAL = "adapt_goal"
    PROPOSE_COMMITMENT = "propose_commitment"


_TEXT = {"type": "string"}
_GOAL = {"type": "integer", "minimum": 1, "maximum": MAX_GOAL_REF}

ACTION_SCHEMAS: tuple[dict, ...] = (
    {"type": "object", "additionalProperties": False, "required": ["action", "title", "target_description"],
     "properties": {"action": {"type": "string", "enum": [ActionKind.PROPOSE_GOAL.value]}, "title": _TEXT, "target_description": _TEXT}},
    {"type": "object", "additionalProperties": False, "required": ["action", "goal", "target_description", "reason"],
     "properties": {"action": {"type": "string", "enum": [ActionKind.ADAPT_GOAL.value]}, "goal": _GOAL, "target_description": _TEXT, "reason": _TEXT}},
    {"type": "object", "additionalProperties": False, "required": ["action", "text", "when"],
     "properties": {"action": {"type": "string", "enum": [ActionKind.PROPOSE_COMMITMENT.value]}, "text": _TEXT,
                    "when": {"type": "string", "enum": ["today", "tomorrow"]},
                    "goal": {"anyOf": [{"type": "null"}, _GOAL]}, "active_task": {"type": "boolean"}}},
)

ACTION_INSTRUCTIONS = (
    "- proposal: usually null. At most one, and only when you really offer something concrete:\n"
    f'  {{"action": "propose_goal", "title": at most {MAX_TITLE} characters, "target_description": what reaching it looks like, at most {MAX_TARGET}}}\n'
    f'  {{"action": "adapt_goal", "goal": the goal\'s number from the application state, "target_description": the new target, "reason": why, at most {MAX_REASON}}}\n'
    f'  {{"action": "propose_commitment", "text": one small concrete thing, at most {MAX_COMMITMENT}, "when": "today" or "tomorrow", '
    '"goal": a goal number or null, "active_task": true when it is about the active task}\n'
    "  Put it ONLY in this field, never in reply_to_user. A proposal does nothing by itself: the application shows it on a card and only the user's own "
    "button can accept it. Never say that anything has been set, saved or agreed, and do not explain the card."
)

_SPEC: dict[ActionKind, dict[str, tuple[str, bool, int]]] = {   # argument -> (type, required, max characters)
    ActionKind.PROPOSE_GOAL: {"title": ("text", True, MAX_TITLE), "target_description": ("text", True, MAX_TARGET)},
    ActionKind.ADAPT_GOAL: {"goal": ("goal", True, 0), "target_description": ("text", True, MAX_TARGET), "reason": ("text", True, MAX_REASON)},
    ActionKind.PROPOSE_COMMITMENT: {"text": ("text", True, MAX_COMMITMENT), "when": ("when", True, 0), "goal": ("goal", False, 0),
                                    "active_task": ("bool", False, 0)},
}
_LINK = re.compile(r"(https?:|www\.|discord\.gg)", re.IGNORECASE)


@dataclass(frozen=True, kw_only=True)
class ValidatedAction:
    kind: ActionKind
    title: str | None = None
    target: str | None = None
    text: str | None = None
    when: str | None = None
    goal_ref: int | None = None
    active_task: bool = False
    reason: str | None = None

    def texts(self) -> tuple[str, ...]:
        return tuple(t for t in (self.title, self.target, self.text, self.reason) if t)


@dataclass(frozen=True, kw_only=True)
class Refusal:
    reason: str


def _clean_text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    normalised = unicodedata.normalize("NFKC", value)
    if any(unicodedata.category(c) in ("Cc", "Cf", "Co", "Cs", "Cn") and c not in "\n\t" for c in normalised):
        return None
    collapsed = " ".join(normalised.split())
    if not collapsed or len(collapsed) > limit or _LINK.search(collapsed):
        return None
    return collapsed


def validate_action(raw: object) -> ValidatedAction | Refusal:
    """`raw` is the `proposal` object exactly as the model wrote it. Never raises. A `Refusal` carries a code that is safe to log."""
    if not isinstance(raw, Mapping):
        return Refusal(reason="invalid_parameters")
    name = raw.get("action")
    try:
        kind = ActionKind(name)
    except ValueError:
        return Refusal(reason="unknown_action")
    parameters = {k: v for k, v in raw.items() if k != "action"}
    spec = _SPEC[kind]
    if not set(parameters) <= set(spec):
        return Refusal(reason="unknown_parameter")
    values: dict[str, Any] = {}
    for key, (type_name, required, limit) in spec.items():
        value = parameters.get(key)
        if value is None:
            if required:
                return Refusal(reason="missing_parameter")
            continue
        if type_name == "text":
            value = _clean_text(value, limit)
            if value is None:
                return Refusal(reason="invalid_text")
        elif type_name == "goal":
            if type(value) is not int or not 1 <= value <= MAX_GOAL_REF:
                return Refusal(reason="invalid_goal")
        elif type_name == "when":
            if value not in ("today", "tomorrow"):
                return Refusal(reason="invalid_when")
        elif type(value) is not bool:
            return Refusal(reason="invalid_parameters")
        values[key] = value
    return ValidatedAction(kind=kind, title=values.get("title"), target=values.get("target_description"), text=values.get("text"),
                           when=values.get("when"), goal_ref=values.get("goal"), active_task=bool(values.get("active_task", False)),
                           reason=values.get("reason"))
