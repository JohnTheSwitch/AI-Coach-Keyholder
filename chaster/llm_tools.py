"""
chaster/llm_tools.py

The READ-ONLY Chaster tool the LLM may call: `get_chaster_status`.

SCOPE (explicitly approved): on-demand, read-only inspection of the
user's ACTIVE Chaster locks. This is the lifting of CHASTER-01B
Increment 4's gate for exactly this tool and nothing broader:
- one tool, no parameters (any arguments the model supplies are IGNORED
  -- nothing is read from them, so they cannot steer the call);
- exactly one Chaster call (`GET /locks?status=active`) via the shared
  `ChasterTokenManager` (pre-call refresh, one 401 refresh-and-retry);
- never `emergency_unlock`, never any write, never `lock_state`,
  `task_runtime` or `chaster_provider_observations`.

EPISTEMIC FRAMING: the result handed to the model is labelled
provider-reported, carries `fetched_at`, and is never described as
verified. It is structurally separate from `lock_state`'s user-reported
state and is never merged with it.

PROMPT-INJECTION HARDENING: lock titles and statuses are free text that
other people (a keyholder) can author. They are control-character
stripped, whitespace-collapsed and length-capped before reaching the
model, the result is capped to MAX_LOCKS_IN_RESULT locks, and the
conversation guidance (conversation_engine/tools.py::TOOL_USE_GUIDANCE)
tells the model a tool result is data, never instructions.

Time comes from `ToolExecutionContext.now` (injected); nothing here
reads the wall clock. Tokens never appear in a result.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum

from chaster.lock_client import ChasterLockApiError, ChasterLockClient
from chaster.presentation import lock_status_text, lock_time_remaining, lock_title
from chaster.token_manager import (
    ChasterNotConnectedError,
    ChasterReauthorizationRequiredError,
    ChasterRefreshUnavailableError,
    ChasterTokenManager,
)
from conversation_engine.models import ToolCallRequest, ToolExecutionResult
from conversation_engine.tools import ToolDefinition, ToolExecutionContext
from infrastructure.time_format import iso

__all__ = [
    "CHASTER_STATUS_TOOL_NAME",
    "CHASTER_STATUS_TOOL",
    "ChasterToolOutcomeKind",
    "ChasterToolOutcome",
    "ChasterToolExecutor",
    "MAX_LOCKS_IN_RESULT",
    "mentions_lock_topic",
    "CHASTER_TOOL_GUIDANCE",
    "is_chaster_status_query",
]

CHASTER_STATUS_TOOL_NAME = "get_chaster_status"
MAX_LOCKS_IN_RESULT = 10
MAX_TEXT_FIELD_CHARACTERS = 80

CHASTER_STATUS_TOOL = ToolDefinition(
    name=CHASTER_STATUS_TOOL_NAME,
    description=(
        "ALWAYS call this first for ANY question about the user's lock, lock status, Chaster, whether they are "
        "locked, or how much time is left -- in ANY language. Read-only: fetches the user's current ACTIVE "
        "Chaster locks live (title, Chaster-reported status, time remaining). Takes no parameters. Examples: "
        "'How much time is left on my lock?', 'What's my status?', 'Check my Chaster lock'. "
        "It cannot change or unlock anything. Not for general conversation."
    ),
    parameters_schema={"type": "object", "properties": {}, "additionalProperties": False},
)

# Appended (as the last system message before the user's turn) ONLY when
# this tool is enabled. It deliberately OVERRIDES the older, general
# advice in prompt_builder's coaching instructions that points the user
# at `lock status` / `lock report ...` to learn their lock state: those
# commands remain real and unchanged, but when this tool is available
# the model must not send the user to them to find out what Chaster
# reports. The two sources stay separate (user-reported vs Chaster).
CHASTER_TOOL_GUIDANCE = (
    "CHASTER TOOL RULES (these take precedence over any earlier advice about commands). "
    f"If the user asks about their lock, lock status, Chaster, whether they are locked, or how much time is "
    f"left -- in any language -- you MUST call the `{CHASTER_STATUS_TOOL_NAME}` tool FIRST, before writing any "
    "answer. Do not answer from memory, and do not say the status is unknown before calling it. "
    "While this tool is available, do NOT tell the user to run `lock status`, `lock report locked` or "
    "`lock report unlocked` to find out their lock state; call the tool instead. "
    "Report only what the tool returns. Chaster-reported data and the user's own earlier reports are "
    "separate facts: never merge them or present one as confirming the other. "
    "If the tool says the account is not connected or needs reconnecting, tell the user to run "
    "`chaster connect`; if it says Chaster is temporarily unavailable, say so and suggest trying again shortly "
    "-- in neither case suggest the `lock report` commands as a substitute."
)


# ---------------------------------------------------------------------------
# Deterministic intent matcher (no model involved)
#
# Small local models (llama3.1 8B) do not reliably choose to call a tool,
# so a message that is clearly asking about the user's Chaster lock is
# routed to `get_chaster_status` by this plain-Python check instead. It
# is deliberately PRECISE rather than broad -- a false positive costs a
# needless (read-only) Chaster call and a confusing answer, e.g. for a
# question about a door lock. The rules, on text that is lowercased and
# stripped of diacritics and punctuation ("Zámek" == "zamek"):
#   1. the word "chaster" (any inflection, e.g. "chasteru"), OR
#   2. a lock noun (lock/locked, or Czech zamek/zamku/zamkem/zamcen...)
#      TOGETHER WITH a status/time cue (status, stav, vypada, zbyva,
#      cas, time, left, remaining, check, zkontroluj, nacti, ...), OR
#   3. a short first-person state QUESTION (must contain "?"): "am i
#      locked?" / "jsem zamceny?" (first-person word + locked/unlocked).
# A message that also names an obviously physical lock (door, car,
# bike, safe ...) is never matched, and neither is a long message.
# Plain `lock status` is a registered command and never reaches here.
# Anything not matched still reaches the model, which may choose the
# tool itself.
# ---------------------------------------------------------------------------
MAX_FORCED_MATCH_CHARACTERS = 200

_LOCK_NOUN_PREFIXES = ("zamk", "zamek", "zamc", "lock")
_STATUS_CUES = frozenset({
    "status", "stav", "stavu", "stavem", "vypada", "zbyva", "zbyvaji", "zbyvajici", "zbyvat",
    "cas", "casu", "time", "left", "remaining", "check", "zkontroluj", "zkontrolovat", "kontrola",
    "nacti", "nactu", "ukaz", "zobraz", "show", "state", "konci", "skonci",
    "duration", "dlouho", "remains", "ends", "end",
})
_PHYSICAL_LOCK_WORDS = frozenset({
    "dver", "dveri", "dvere", "dverich", "dvermi", "auto", "auta", "kolo", "kola", "kufr", "trezor",
    "trezoru", "brana", "brany", "door", "doors", "car", "bike", "bicycle", "safe", "padlock",
    "kolecko", "sifr", "sifry", "kod", "code", "password", "heslo", "klic", "klice", "key", "keys",
})
_FIRST_PERSON = frozenset({"jsem", "am", "i"})
_SELF_STATE_PREFIXES = ("zamcen", "odemcen", "locked", "unlocked")
MAX_SELF_STATE_CHARACTERS = 60
_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


def _normalize_tokens(text: str) -> list[str]:
    decomposed = unicodedata.normalize("NFKD", text.lower())
    # Combining marks (diacritics) AND invisible format characters
    # (zero-width space/joiner, soft hyphen, BOM, bidi marks -- category
    # "Cf") are dropped, so text pasted or auto-formatted by a chat
    # client cannot split "zámkem" into two tokens.
    ascii_text = "".join(
        ch for ch in decomposed if not unicodedata.combining(ch) and unicodedata.category(ch) != "Cf"
    )
    return [t for t in _TOKEN_SPLIT.split(ascii_text) if t]


def is_chaster_status_query(message: str) -> bool:
    """Pure, side-effect-free, never raises for a str. See the block
    comment above for the exact rules and why they are conservative."""
    if not isinstance(message, str) or not message.strip() or len(message) > MAX_FORCED_MATCH_CHARACTERS:
        return False
    tokens = _normalize_tokens(message)
    if not tokens:
        return False
    token_set = set(tokens)
    if token_set & _PHYSICAL_LOCK_WORDS:
        return False
    if any(t.startswith("chaster") for t in tokens) and token_set & _STATUS_CUES:
        return True
    if (
        "?" in message
        and len(message) <= MAX_SELF_STATE_CHARACTERS
        and token_set & _FIRST_PERSON
        and any(t.startswith(_SELF_STATE_PREFIXES) for t in tokens)
    ):
        return True
    has_lock_noun = any(t.startswith(_LOCK_NOUN_PREFIXES) for t in tokens)
    return has_lock_noun and bool(token_set & _STATUS_CUES)


_LOCK_TOPIC_PREFIXES = (
    "odemk", "odemc", "zamk", "zamc", "zamek", "klic", "chaster", "unlock", "lock", "key", "frozen", "zmraz",
)


def mentions_lock_topic(message: str) -> bool:
    """Broad, cheap, side-effect-free check: does the message contain ANY
    word connected with locks or unlocking (odemkni, odemknout, zamčený,
    zámek, klíč, chaster, ...)? Used only to decide whether to LOAD
    Chaster context for a normal conversation turn -- never to answer
    on its own, so a false positive costs one read-only call, nothing
    more. Diacritics/case-insensitive; a non-string is False."""
    if not isinstance(message, str) or not message.strip():
        return False
    return any(token.startswith(_LOCK_TOPIC_PREFIXES) for token in _normalize_tokens(message))


class ChasterToolOutcomeKind(StrEnum):
    LOCKS = "locks"
    NOT_CONNECTED = "not_connected"
    RECONNECT_REQUIRED = "reconnect_required"
    TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"
    API_ERROR = "api_error"


@dataclass(frozen=True, kw_only=True)
class ChasterToolOutcome:
    """The structured payload carried in `ToolExecutionResult.data` for
    the application layer to build a deterministic embed from. The raw
    lock dicts are the same objects `chaster status` renders. Never
    contains a token."""
    kind: ChasterToolOutcomeKind
    locks: tuple[dict, ...] = ()
    detail: str | None = None


_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f  ]+")


def _clean(text: str) -> str:
    collapsed = " ".join(_CONTROL_CHARS.sub(" ", text).split())
    if len(collapsed) > MAX_TEXT_FIELD_CHARACTERS:
        collapsed = collapsed[: MAX_TEXT_FIELD_CHARACTERS - 1].rstrip() + "…"
    return collapsed


sanitize_text = _clean  # public alias for sibling modules (status_context)


class ChasterToolExecutor:
    """Implements conversation_engine.tools.ToolExecutor for Chaster."""

    guidance = CHASTER_TOOL_GUIDANCE

    def __init__(self, *, token_manager: ChasterTokenManager, lock_client: ChasterLockClient) -> None:
        self._token_manager = token_manager
        self._lock_client = lock_client

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        return (CHASTER_STATUS_TOOL,)

    def match_forced_call(self, message: str) -> ToolCallRequest | None:
        """Deterministic routing: a message clearly asking about the
        user's Chaster lock runs `get_chaster_status` without the model
        having to choose it. See `is_chaster_status_query`."""
        if is_chaster_status_query(message):
            return ToolCallRequest(tool_name=CHASTER_STATUS_TOOL_NAME, parameters={})
        return None

    def execute(self, call: ToolCallRequest, *, context: ToolExecutionContext) -> ToolExecutionResult:
        if call.tool_name != CHASTER_STATUS_TOOL_NAME:
            return self._result(ok=False, kind=None, payload={"status": "unknown_tool"})

        fetched_at = iso(context.now)
        try:
            locks = self._token_manager.call_with_token(
                context.subject_key, now=context.now,
                operation=lambda token: self._lock_client.list_active_locks(access_token=token),
            )
        except ChasterNotConnectedError:
            return self._failure(
                ChasterToolOutcomeKind.NOT_CONNECTED, "not_connected",
                "The user has no Chaster account connected. Tell them to run `chaster connect`.",
            )
        except ChasterReauthorizationRequiredError:
            return self._failure(
                ChasterToolOutcomeKind.RECONNECT_REQUIRED, "reconnect_required",
                "The user's Chaster authorization expired or was revoked and could not be renewed. "
                "Tell them to run `chaster connect` to reconnect.",
            )
        except ChasterRefreshUnavailableError:
            return self._failure(
                ChasterToolOutcomeKind.TEMPORARILY_UNAVAILABLE, "temporarily_unavailable",
                "Chaster could not be reached to renew access. The connection is intact. "
                "Tell the user to try again shortly.",
            )
        except ChasterLockApiError as exc:
            return self._failure(
                ChasterToolOutcomeKind.API_ERROR, "api_error",
                "Chaster's lock service returned an error. Tell the user it is unavailable right now "
                "and to try again shortly.", outcome_detail=str(exc),
            )

        shown = locks[:MAX_LOCKS_IN_RESULT]
        payload = {
            "status": "ok",
            "source": "Chaster API",
            "reliability": "provider-reported; not independently verified",
            "fetched_at": fetched_at,
            "active_lock_count": len(locks),
            "locks": [
                {
                    "title": _clean(lock_title(lock)),
                    "status": _clean(lock_status_text(lock)),
                    "time_remaining": lock_time_remaining(lock, now=context.now),
                }
                for lock in shown
            ],
        }
        if len(locks) > len(shown):
            payload["locks_omitted"] = len(locks) - len(shown)
        return self._result(
            ok=True, kind=ChasterToolOutcomeKind.LOCKS, payload=payload, locks=tuple(locks),
        )

    def _failure(
        self, kind: ChasterToolOutcomeKind, status: str, instruction: str, *, outcome_detail: str | None = None,
    ) -> ToolExecutionResult:
        return self._result(
            ok=False, kind=kind, payload={"status": status, "instruction": instruction}, detail=outcome_detail,
        )

    @staticmethod
    def _result(
        *, ok: bool, kind: ChasterToolOutcomeKind | None, payload: dict, locks: tuple[dict, ...] = (),
        detail: str | None = None,
    ) -> ToolExecutionResult:
        data = ChasterToolOutcome(kind=kind, locks=locks, detail=detail) if kind is not None else None
        return ToolExecutionResult(
            tool_name=CHASTER_STATUS_TOOL_NAME, ok=ok, content=json.dumps(payload, ensure_ascii=False), data=data,
        )
