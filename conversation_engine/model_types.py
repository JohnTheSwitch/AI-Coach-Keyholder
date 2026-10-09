"""
conversation_engine/model_types.py

The boundary between Conversation Engine's own types (ResponseContextSnapshot,
ResponsePlan, ConversationResponse, ...) and the model adapter.
`ConversationModel` implementations (e.g. OllamaConversationModel) know
NOTHING about snapshots, plans, the recent-history buffer, or
ConversationResponse -- only these three small, self-contained types.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from conversation_engine.models import ToolCallRequest
from conversation_engine.tools import ToolDefinition

__all__ = [
    "ModelMessageRole", "ModelMessage", "ModelGenerationRequest", "ConversationModel", "LLMGenerationError",
    "ModelTurn", "ToolCapableConversationModel",
]


class LLMGenerationError(RuntimeError):
    """The ONLY exception type a ConversationModel implementation may
    let escape its own `generate()`. `code` is a short, sanitized,
    machine-readable identifier -- never a prompt, a response body, or
    any user/context data (see ConversationEngine's own single logging
    point, engine.py)."""
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ModelMessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(frozen=True, kw_only=True)
class ModelMessage:
    """`tool_calls` is set only on the ASSISTANT message that requested
    tools; `tool_name` only on a TOOL message carrying that tool's
    result. Both are empty/None on every ordinary message."""
    role: ModelMessageRole
    content: str
    tool_calls: tuple[ToolCallRequest, ...] = ()
    tool_name: str | None = None


@dataclass(frozen=True, kw_only=True)
class ModelGenerationRequest:
    messages: tuple[ModelMessage, ...]
    max_output_characters: int


class ConversationModel(Protocol):
    """A structural Protocol, matching this project's own established
    precedent (infrastructure/clock.py's Clock, conversation_engine/
    providers.py's ConversationContextProvider). Returns a validated,
    already-bounded raw string -- never raises anything other than
    LLMGenerationError (or a subclass) at its own boundary."""

    def generate(self, *, request: ModelGenerationRequest) -> str: ...


@dataclass(frozen=True, kw_only=True)
class ModelTurn:
    """One model reply on the tool-enabled path: EITHER final text OR
    tool calls, never both (an adapter that receives both discards the
    text and keeps the calls)."""
    text: str | None = None
    tool_calls: tuple[ToolCallRequest, ...] = ()

    def __post_init__(self) -> None:
        has_text = self.text is not None and self.text.strip() != ""
        if has_text == bool(self.tool_calls):
            raise ValueError("a ModelTurn must contain exactly one of: non-empty text, or tool_calls.")


@runtime_checkable
class ToolCapableConversationModel(Protocol):
    """Additive to ConversationModel (which is unchanged). Same error
    contract: only LLMGenerationError may escape. A model that cannot
    accept tools raises LLMGenerationError("<adapter>_tools_unsupported"),
    which the engine treats as "use the plain path"."""

    def generate_with_tools(
        self, *, request: ModelGenerationRequest, tools: tuple[ToolDefinition, ...],
    ) -> ModelTurn: ...
