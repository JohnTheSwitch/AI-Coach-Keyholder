"""
conversation_engine/engine.py

ConversationEngine -- Slice 3's own orchestration entry point, now
reading/writing through memory_system's WorkingMemoryReader/
WorkingMemoryWriter instead of the retired TransitionalRecentMessageBuffer.
The ONLY place in this package that logs an expected model/validation/
memory failure (ConversationModel implementations only ever raise a
typed LLMGenerationError; memory_system itself never logs anything).

Read failure policy: an expected WorkingMemoryError (or an unexpected
exception) during read() is logged once, then the engine proceeds with
an EMPTY history -- Working Memory turns are not authoritative domain
facts, so an empty history is a safe degradation, not a fabricated one
(the same state a brand-new subject's first message already has).

Write failure policy: if generation and validation both succeeded but
commit_exchange() fails (expected WorkingMemoryError,
WorkingMemoryCapacityError, or an unexpected exception), the already-
validated response is still returned to the user -- discarding a safe,
correct response over an internal memory-continuity failure would be
disproportionate. Logged once. No retry, no partial commit. The next
turn simply won't see this exchange in its history -- a real but
bounded consequence.

Expected vs. unexpected failures get DIFFERENT log codes -- an
unexpected exception must never be logged as if it were an ordinary,
anticipated capacity/read/commit condition.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Sequence

from conversation_engine.context import build_response_context
from conversation_engine.guardrails import build_guardrails
from conversation_engine.fallback import FallbackReason, render_fallback
from conversation_engine.model_types import (
    ConversationModel,
    LLMGenerationError,
    ModelGenerationRequest,
    ModelMessage,
    ModelMessageRole,
    ToolCapableConversationModel,
)
from conversation_engine.models import (
    ConversationResponse,
    ResponseCategory,
    SituationalConstraints,
    ToolCallRequest,
    ToolExecutionResult,
)
from conversation_engine.planning import build_response_plan
from conversation_engine.prompt_builder import build_generation_request
from conversation_engine.providers import ConversationContextProvider
from conversation_engine.subject_queue import SubjectConversationQueue
from conversation_engine.tools import ToolExecutionContext, ToolExecutor, build_tool_guidance
from conversation_engine.validation import validate_response
from memory_system.models import WorkingMemoryCapacityError, WorkingMemoryError, WorkingMemorySnapshot
from memory_system.working_memory import WorkingMemoryReader, WorkingMemoryWriter

__all__ = ["ConversationEngine", "DEFAULT_MAX_OUTPUT_CHARACTERS", "MAX_TOOL_CALLS_PER_MESSAGE"]

logger = logging.getLogger("ai_coach_keyholder.conversation_engine")

DEFAULT_MAX_OUTPUT_CHARACTERS = 1800

# Hard bound on model-initiated tool use: ONE tool call, in ONE round,
# per user message. Any further call the model asks for in the same
# reply is dropped; a second round is never offered (the follow-up
# request carries no tools at all).
MAX_TOOL_CALLS_PER_MESSAGE = 1


class ConversationEngine:
    """
    Composition-root constructed only -- never builds its own
    ConversationModel/WorkingMemoryReader/WorkingMemoryWriter/queue
    (dependency injection, per explicit review decision).
    `generate_response()` is the single call ApplicationService makes
    for the unmatched-text path.
    """

    def __init__(
        self, *, model: ConversationModel,
        working_memory_reader: WorkingMemoryReader, working_memory_writer: WorkingMemoryWriter,
        queue: SubjectConversationQueue, providers: Sequence[ConversationContextProvider] = (),
        max_output_characters: int = DEFAULT_MAX_OUTPUT_CHARACTERS,
        tool_executor: ToolExecutor | None = None,
    ) -> None:
        self._model = model
        self._working_memory_reader = working_memory_reader
        self._working_memory_writer = working_memory_writer
        self._queue = queue
        self._providers = providers
        self._max_output_characters = max_output_characters
        # Read-only tool calling is active only when BOTH an executor was
        # injected AND the model implements generate_with_tools().
        self._tool_executor = tool_executor
        if tool_executor is not None and not isinstance(model, ToolCapableConversationModel):
            logger.warning(
                "Tool executor configured but the model adapter does not support tools -- tools disabled",
                extra={"error_code": "tools_model_unsupported", "adapter": type(model).__name__},
            )
            self._tool_executor = None
        if self._tool_executor is not None:
            logger.info(
                "Conversation Engine tool calling ACTIVE",
                extra={"tools": [d.name for d in self._tool_executor.definitions]},
            )

    def generate_response(
        self, *, subject_key: str, current_user_message: str, language: str, identity_id: str, now: datetime,
        extra_system_context: str | None = None, remember_exchange: bool = False,
    ) -> ConversationResponse:
        """`extra_system_context` is verified, application-supplied DATA
        for this one reply (e.g. freshly read Chaster lock state). When
        given: it is inserted as a SYSTEM message right before the user
        message, tools are NEVER offered (the plain `generate` path is
        used, so the model cannot invent or call anything), and the
        exchange is NOT committed to Working Memory (provider data must
        not be read back later as if it were current).

        `remember_exchange=True` (ordinary chat turns that merely received
        context) commits the user/assistant TEXT to Working Memory anyway;
        the context block itself is never stored, and the context tells
        the model that figures in earlier turns may be outdated."""
        with self._queue.turn(subject_key):
            working_memory_snapshot = self._read_working_memory(subject_key)

            outcome = build_response_context(
                response_category=ResponseCategory.COACHING_DIALOGUE,
                current_user_message=current_user_message, language=language, identity_id=identity_id,
                situational_constraints=SituationalConstraints(), providers=self._providers,
                required_provider_namespaces=frozenset(), subject_key=subject_key, now=now,
            )
            if outcome.fallback_response is not None:
                return outcome.fallback_response  # Working Memory is NOT touched

            plan = build_response_plan(outcome.snapshot)
            request = build_generation_request(
                snapshot=outcome.snapshot, plan=plan, working_memory_turns=working_memory_snapshot.turns,
                max_output_characters=self._max_output_characters,
            )
            # Guardrails (prompt confidentiality, pleas for early unlock) apply to
            # every reply, as the last system message before the user turn.
            request = ModelGenerationRequest(
                messages=self._with_tool_guidance(request.messages, build_guardrails()),
                max_output_characters=request.max_output_characters,
            )

            tool_outputs: tuple[ToolExecutionResult, ...] = ()
            try:
                if extra_system_context is not None:
                    if not extra_system_context.strip():
                        raise ValueError("extra_system_context must not be blank")
                    context_request = ModelGenerationRequest(
                        messages=self._with_tool_guidance(request.messages, extra_system_context),
                        max_output_characters=request.max_output_characters,
                    )
                    raw_text = self._model.generate(request=context_request)
                elif self._tool_executor is not None:
                    raw_text, tool_outputs = self._generate_with_tools(
                        request, subject_key=subject_key, now=now, user_message=current_user_message,
                    )
                else:
                    raw_text = self._model.generate(request=request)
            except LLMGenerationError as exc:
                logger.warning(
                    "Conversation Engine generation failed",
                    extra={
                        "error_code": exc.code, "adapter": type(self._model).__name__,
                        "response_category": plan.response_category.value,
                    },
                )
                return render_fallback(FallbackReason.GENERATION_UNAVAILABLE, language=language)

            response = ConversationResponse(text=raw_text, response_category=plan.response_category, tool_outputs=tool_outputs)
            validation = validate_response(response, plan, outcome.snapshot)
            if not validation.is_valid:
                logger.warning(
                    "Conversation Engine response validation failed",
                    extra={
                        "error_code": "validation_failed", "adapter": type(self._model).__name__,
                        "response_category": plan.response_category.value,
                    },
                )
                return render_fallback(FallbackReason.GENERATION_UNAVAILABLE, language=language)

            # Only a fully successful, validated exchange is committed --
            # never on a fallback path. Write failure policy: the
            # already-validated response is still returned regardless
            # (see this module's own docstring).
            #
            # Exception: an exchange that used a tool is NOT committed.
            # Provider-reported data (e.g. Chaster lock state) must not
            # enter Working Memory, where a later turn could read a stale
            # figure back as if it were current. The trade-off is that
            # the next turn does not see this one exchange.
            if not tool_outputs and (extra_system_context is None or remember_exchange):
                self._commit_exchange(subject_key, current_user_message, response.text)
            return response

    def _generate_with_tools(
        self, request: ModelGenerationRequest, *, subject_key: str, now: datetime, user_message: str,
    ) -> tuple[str, tuple[ToolExecutionResult, ...]]:
        """One bounded, read-only tool round. Raises only
        LLMGenerationError (caught by the caller). If the model rejects
        tools outright, transparently uses the plain path instead so an
        unsupported model never takes conversation down.

        Two ways a round starts, both producing the SAME bounded shape
        (one call, one round, no tools on the follow-up):
        - FORCED: the executor's deterministic `match_forced_call()`
          recognised the message, so the engine runs the tool itself
          without asking the model to choose it;
        - MODEL-CHOSEN: the model asks for a tool during its first turn."""
        executor = self._tool_executor
        assert executor is not None
        definitions = executor.definitions
        tool_request = ModelGenerationRequest(
            messages=self._with_tool_guidance(request.messages, build_tool_guidance(executor)),
            max_output_characters=request.max_output_characters,
        )

        forced = self._match_forced_call(executor, user_message)
        if forced is not None:
            calls: tuple[ToolCallRequest, ...] = (forced,)
        else:
            logger.info("No forced tool match -- the model decides whether to call a tool")
            try:
                turn = self._model.generate_with_tools(request=tool_request, tools=definitions)  # type: ignore[attr-defined]
            except LLMGenerationError as exc:
                if not exc.code.endswith("_tools_unsupported"):
                    raise
                logger.warning(
                    "Model rejected tools -- using the plain generation path",
                    extra={"error_code": exc.code, "adapter": type(self._model).__name__},
                )
                return self._model.generate(request=request), ()

            if not turn.tool_calls:
                return turn.text or "", ()

            calls = turn.tool_calls[:MAX_TOOL_CALLS_PER_MESSAGE]
            if len(turn.tool_calls) > len(calls):
                logger.warning(
                    "Model requested more tool calls than allowed -- extras dropped",
                    extra={"error_code": "tool_calls_truncated", "requested": len(turn.tool_calls)},
                )
        context = ToolExecutionContext(subject_key=subject_key, now=now)
        results = tuple(self._execute_tool(call, context=context, known={d.name for d in definitions}) for call in calls)

        follow_up_messages = list(tool_request.messages)
        follow_up_messages.append(ModelMessage(role=ModelMessageRole.ASSISTANT, content="", tool_calls=calls))
        for result in results:
            follow_up_messages.append(
                ModelMessage(role=ModelMessageRole.TOOL, content=result.content, tool_name=result.tool_name)
            )
        follow_up = ModelGenerationRequest(
            messages=tuple(follow_up_messages), max_output_characters=request.max_output_characters,
        )
        # No tools are offered on the follow-up: a second round is
        # structurally impossible, not merely discouraged.
        final = self._model.generate_with_tools(request=follow_up, tools=())  # type: ignore[attr-defined]
        if final.tool_calls or not final.text:
            raise LLMGenerationError("tool_round_limit_exceeded")
        return final.text, results

    @staticmethod
    def _match_forced_call(executor: ToolExecutor, user_message: str) -> ToolCallRequest | None:
        """Runs the executor's optional deterministic matcher. Any
        failure or malformed answer means "no match" -- the normal
        model-chosen path then applies, so a matcher bug can never take
        conversation down or invent a call."""
        matcher = getattr(executor, "match_forced_call", None)
        if matcher is None:
            return None
        try:
            call = matcher(user_message)
        except Exception:  # noqa: BLE001 -- a matcher bug must degrade to "no match"
            logger.warning("Forced tool matcher raised -- ignoring", extra={"error_code": "forced_tool_matcher_failed"})
            return None
        if call is None:
            return None
        if not isinstance(call, ToolCallRequest) or call.tool_name not in {d.name for d in executor.definitions}:
            logger.warning("Forced tool matcher returned an invalid call -- ignoring", extra={"error_code": "forced_tool_invalid"})
            return None
        logger.info("Tool call forced by deterministic match", extra={"tool_name": call.tool_name})
        return call

    @staticmethod
    def _with_tool_guidance(messages: tuple[ModelMessage, ...], guidance_text: str) -> tuple[ModelMessage, ...]:
        """Inserts the tool-use guidance as its own SYSTEM message
        immediately BEFORE the final user message. It is placed last
        (not next to the original system message) on purpose: the
        original system text contains older, general advice -- e.g. to
        point the user at `lock status` / `lock report ...` -- and a
        small local model follows the instruction nearest the question.
        The original system text (including _SYSTEM_BOUNDARIES) is never
        modified."""
        guidance = ModelMessage(role=ModelMessageRole.SYSTEM, content=guidance_text)
        if messages and messages[-1].role == ModelMessageRole.USER:
            return messages[:-1] + (guidance, messages[-1])
        return messages + (guidance,)

    def _execute_tool(
        self, call: ToolCallRequest, *, context: ToolExecutionContext, known: set[str],
    ) -> ToolExecutionResult:
        """Never raises. An unknown tool name or an unexpected executor
        failure becomes an honest `ok=False` result the model can
        explain to the user."""
        if call.tool_name not in known:
            logger.warning("Model requested an unknown tool", extra={"error_code": "unknown_tool"})
            return ToolExecutionResult(
                tool_name=call.tool_name[:64] or "unknown", ok=False,
                content='{"status": "unknown_tool", "detail": "That tool does not exist. Answer without it."}',
            )
        try:
            result = self._tool_executor.execute(call, context=context)  # type: ignore[union-attr]
            if not isinstance(result, ToolExecutionResult) or result.tool_name != call.tool_name:
                raise TypeError("executor returned a result that does not match the call")
            return result
        except Exception:  # noqa: BLE001 -- safety net; expected failures are returned, not raised
            logger.warning(
                "Tool execution raised an unexpected error",
                extra={"error_code": "tool_execution_failed", "tool_name": call.tool_name},
            )
            return ToolExecutionResult(
                tool_name=call.tool_name, ok=False,
                content='{"status": "tool_error", "detail": "The tool failed unexpectedly. '
                        'Tell the user this information could not be retrieved right now."}',
            )

    def _read_working_memory(self, subject_key: str) -> WorkingMemorySnapshot:
        try:
            return self._working_memory_reader.read(subject_key=subject_key)
        except WorkingMemoryError:
            logger.warning(
                "Conversation Engine working memory read failed",
                extra={
                    "error_code": "working_memory_read_failed", "operation": "read",
                    "component": type(self._working_memory_reader).__name__,
                },
            )
        except Exception:
            logger.warning(
                "Conversation Engine working memory read raised an unexpected error",
                extra={
                    "error_code": "working_memory_unexpected_read_error", "operation": "read",
                    "component": type(self._working_memory_reader).__name__,
                },
            )
        return WorkingMemorySnapshot(turns=())  # safe degradation -- an empty history is already a normal state

    def _commit_exchange(self, subject_key: str, user_content: str, assistant_content: str) -> None:
        try:
            self._working_memory_writer.commit_exchange(
                subject_key=subject_key, user_content=user_content, assistant_content=assistant_content,
            )
        except WorkingMemoryCapacityError:
            logger.warning(
                "Conversation Engine working memory commit exceeded capacity",
                extra={
                    "error_code": "working_memory_capacity_exceeded", "operation": "commit",
                    "component": type(self._working_memory_writer).__name__,
                },
            )
        except WorkingMemoryError:
            logger.warning(
                "Conversation Engine working memory commit failed",
                extra={
                    "error_code": "working_memory_commit_failed", "operation": "commit",
                    "component": type(self._working_memory_writer).__name__,
                },
            )
        except Exception:
            logger.warning(
                "Conversation Engine working memory commit raised an unexpected error",
                extra={
                    "error_code": "working_memory_unexpected_commit_error", "operation": "commit",
                    "component": type(self._working_memory_writer).__name__,
                },
            )
        # In every case above, the already-validated response returned to
        # the caller is unaffected -- the write failure policy this
        # module's own docstring documents.
