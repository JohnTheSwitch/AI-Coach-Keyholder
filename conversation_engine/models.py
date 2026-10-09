"""
conversation_engine/models.py

docs/architecture/conversation_engine_technical_design.md (draft, not
approved for implementation as a whole). This module implements ONLY
Slice 1's own runtime types -- see conversation_engine/README.md for
the exact boundary between what is implemented here and what remains
draft/undecided.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from numbers import Real
from types import MappingProxyType
from typing import Any, Mapping

from ai.identity_catalog import CommunicationProfile

__all__ = [
    "ResponseCategory",
    "GenerationPath",
    "ConversationContextFragment",
    "SituationalConstraints",
    "ResponseContextSnapshot",
    "ToolCallRequest",
    "ToolResult",
    "ToolExecutionResult",
    "MAX_TOOL_RESULT_CHARACTERS",
    "ResponsePlan",
    "ConversationResponse",
    "UnknownIdentityError",
    "ProviderNamespaceCollisionError",
    "ProviderNamespaceMismatchError",
    "RequiredProviderFailedError",
    "UnsupportedFragmentDataError",
]


class ResponseCategory(StrEnum):
    """conversation_engine_technical_design.md Section 5."""
    INFORMATIONAL_STATUS = "informational_status"
    OPERATION_CONFIRMATION = "operation_confirmation"
    GOVERNANCE_EXPLANATION = "governance_explanation"
    ONBOARDING = "onboarding"
    COACHING_DIALOGUE = "coaching_dialogue"
    MOTIVATIONAL = "motivational"
    REFLECTIVE = "reflective"
    CRISIS = "crisis"
    ERROR_FALLBACK = "error_fallback"


class GenerationPath(StrEnum):
    """
    Slice 2 adds MODEL_GENERATION -- the INTENDED path for every
    actively-processed unmatched conversational message, decided by
    build_response_plan() regardless of whether the model actually
    succeeds. DETERMINISTIC_FALLBACK is not an alternative plan for
    the same request within an active engine -- it is the real
    RESPONSE returned after a model failure/validation failure, or
    when no engine is configured at all (in which case
    ApplicationService never even reaches a model plan). See
    conversation_engine/engine.py's own docstring.
    """
    DETERMINISTIC_FALLBACK = "deterministic_fallback"
    MODEL_GENERATION = "model_generation"


class UnknownIdentityError(ValueError):
    """identity_adapter.build_identity_profile() found no catalog
    entry for the given identity_id -- a deterministic failure, never
    a silent default."""


class ProviderNamespaceCollisionError(ValueError):
    """Two providers' fragments claimed the same namespace during
    context assembly."""


class ProviderNamespaceMismatchError(ValueError):
    """A provider's own .namespace property does not match the
    namespace on the ConversationContextFragment it returned."""


class RequiredProviderFailedError(RuntimeError):
    """A namespace listed in ResponsePlan.required_provider_namespaces
    has no successful fragment -- context assembly must not proceed
    with a fabricated or partial stand-in; the caller (build_response_context)
    is the one place that turns this into a deterministic fallback."""


class UnsupportedFragmentDataError(TypeError):
    """ConversationContextFragment.data contained a value _freeze()
    cannot recursively normalize into a genuinely immutable form."""


def _freeze(value: Any) -> Any:
    """
    Recursive normalization into genuinely immutable data -- not just
    a top-level MappingProxyType, which leaves nested lists/dicts
    mutable. Mapping -> immutable mapping (recursively frozen values);
    list/tuple -> tuple (recursively frozen elements); set/frozenset ->
    frozenset (recursively frozen elements); str/int/float/bool/bytes/
    None -> unchanged. Anything else (an arbitrary domain object, a
    custom class instance, ...) is rejected outright -- this function
    deliberately never attempts a deepcopy or tries to "freeze" a
    foreign domain instance; a provider that wants to expose such a
    thing must serialize it to a supported shape itself.
    """
    if value is None or isinstance(value, (str, bool, int, float, bytes)):
        return value
    if isinstance(value, Mapping):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(v) for v in value)
    raise UnsupportedFragmentDataError(
        f"ConversationContextFragment.data contains an unsupported, potentially "
        f"mutable value of type {type(value).__name__!r} -- only mappings, "
        f"lists/tuples, sets, and JSON-like scalars are accepted. Serialize it "
        f"to a supported shape before returning it from a provider."
    )


@dataclass(frozen=True, kw_only=True)
class ConversationContextFragment:
    """CE-7/CE-9: immutable (recursively -- see `_freeze`), namespaced,
    read-only once assembled. `namespace` must be non-empty."""
    namespace: str
    data: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not self.namespace or not self.namespace.strip():
            raise ValueError("ConversationContextFragment.namespace must be non-empty.")
        object.__setattr__(self, "data", _freeze(dict(self.data)))


def _validate_constraint_value(name: str, value: float | None) -> None:
    if value is None:
        return
    if not isinstance(value, Real) or isinstance(value, bool):
        raise ValueError(f"SituationalConstraints.{name} must be None or a number, got {type(value).__name__!r}.")
    if not (0.0 <= float(value) <= 1.0):
        raise ValueError(f"SituationalConstraints.{name} must be within 0.0-1.0, got {value!r}.")


@dataclass(frozen=True, kw_only=True)
class SituationalConstraints:
    """
    A clamp already decided elsewhere and handed in -- this type (and
    apply_situational_constraints() in context.py) has no detection
    logic of its own (ai_identity_technical_design.md Section 6's own
    ID-4/ID-5: the constraint is a property of the situation, decided
    upstream of this engine, never negotiable by identity or by this
    engine's own judgment).
    """
    max_humor: float | None = None
    max_teasing: float | None = None
    max_assertiveness: float | None = None
    max_verbosity: float | None = None

    def __post_init__(self) -> None:
        _validate_constraint_value("max_humor", self.max_humor)
        _validate_constraint_value("max_teasing", self.max_teasing)
        _validate_constraint_value("max_assertiveness", self.max_assertiveness)
        _validate_constraint_value("max_verbosity", self.max_verbosity)


@dataclass(frozen=True, kw_only=True)
class ResponseContextSnapshot:
    """CE-7: assembled once per response, immutable, never persisted
    by this engine. Six core fields (always present, not
    provider-sourced) plus context_fragments (Section 6/7 of the
    design document -- everything domain- or memory-specific).

    `identity_id` (Scarlett/Hybrid Personality Presentation slice):
    the raw catalog id (e.g. "scarlett") the caller already resolved
    `identity_profile` from -- threaded through as plain data so
    `prompt_builder.py` can label the tone-guidance block without
    prompt_builder itself gaining a second, independent dependency on
    `ai.identity_catalog` (the catalog is read exactly once, in
    `identity_adapter.build_identity_profile()`/`context.py`, same as
    before this slice)."""
    response_category: ResponseCategory
    current_user_message: str
    language: str
    identity_profile: CommunicationProfile
    identity_id: str
    situational_constraints: SituationalConstraints
    context_fragments: Mapping[str, ConversationContextFragment]

    def __post_init__(self) -> None:
        if not isinstance(self.context_fragments, MappingProxyType):
            object.__setattr__(self, "context_fragments", MappingProxyType(dict(self.context_fragments)))
        for key, fragment in self.context_fragments.items():
            if key != fragment.namespace:
                raise ValueError(
                    f"context_fragments key {key!r} does not match its own "
                    f"fragment.namespace {fragment.namespace!r}."
                )


@dataclass(frozen=True, kw_only=True)
class ToolCallRequest:
    """A model-initiated request to run one read-only tool
    (conversation_engine_technical_design.md Section 11). Constructed
    only by a tool-capable ConversationModel adapter from the model's
    own reply; executed by ConversationEngine through an injected
    ToolExecutor, never planned in advance (ResponsePlan.tool_calls
    stays empty). Tool calling was approved for READ-ONLY tools only."""
    tool_name: str
    parameters: Mapping[str, Any]


@dataclass(frozen=True, kw_only=True)
class ToolResult:
    """Design sketch only (superseded by ToolExecutionResult below,
    which is what the engine actually uses)."""
    tool_name: str
    outcome: Any
    error: str | None = None


MAX_TOOL_RESULT_CHARACTERS = 4000


@dataclass(frozen=True, kw_only=True)
class ToolExecutionResult:
    """What a ToolExecutor returns for one call.

    `content` is the ONLY field the model ever sees (bounded text,
    treated as untrusted DATA by the prompt guidance). `data` is an
    opaque, application-layer payload the engine passes through
    untouched and never inspects -- it lets ApplicationService attach a
    deterministic rich presentation (e.g. an embed) built from the real
    tool output rather than from anything the model wrote. `ok=False`
    means the tool could not produce its normal result; the model is
    still given `content` so it can explain the situation honestly."""
    tool_name: str
    ok: bool
    content: str
    data: object | None = None

    def __post_init__(self) -> None:
        if not self.tool_name or not self.tool_name.strip():
            raise ValueError("tool_name must be a non-empty string.")
        if not self.content or not self.content.strip():
            raise ValueError("content must be a non-empty string.")
        if len(self.content) > MAX_TOOL_RESULT_CHARACTERS:
            raise ValueError(f"content must be at most {MAX_TOOL_RESULT_CHARACTERS} characters.")


@dataclass(frozen=True, kw_only=True)
class ResponsePlan:
    """Response Planning's own output (pipeline stage 2). `tool_calls`
    is always empty through this slice -- CE-21."""
    response_category: ResponseCategory
    required_provider_namespaces: frozenset[str] = frozenset()
    optional_provider_namespaces: frozenset[str] = frozenset()
    generation_path: GenerationPath = GenerationPath.DETERMINISTIC_FALLBACK
    tool_calls: tuple[ToolCallRequest, ...] = ()

    def __post_init__(self) -> None:
        overlap = self.required_provider_namespaces & self.optional_provider_namespaces
        if overlap:
            raise ValueError(
                f"required_provider_namespaces and optional_provider_namespaces must "
                f"not overlap -- a namespace cannot be both: {sorted(overlap)!r}"
            )


@dataclass(frozen=True, kw_only=True)
class ConversationResponse:
    """Pipeline stage 7's own output. No field here can express a
    domain write -- validation.py checks this shape structurally, not
    merely by convention. `tool_outputs` records the read-only tool
    results (if any) the reply was generated from, so the application
    layer can attach a deterministic presentation; it is data about what
    was READ, never an instruction to change anything."""
    text: str
    response_category: ResponseCategory
    tool_outputs: tuple[ToolExecutionResult, ...] = ()
