"""
tests/conversation_engine/test_engine_tool_calling.py

ConversationEngine's bounded, read-only tool round. Uses scripted fake
models and a fake executor -- no network, no Chaster.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest

from conversation_engine.engine import MAX_TOOL_CALLS_PER_MESSAGE, ConversationEngine
from conversation_engine.guardrails import build_guardrails
from conversation_engine.model_types import (
    LLMGenerationError, ModelGenerationRequest, ModelMessageRole, ModelTurn,
)
from conversation_engine.models import ToolCallRequest, ToolExecutionResult
from conversation_engine.prompt_builder import _SYSTEM_BOUNDARIES
from conversation_engine.subject_queue import SubjectConversationQueue
from conversation_engine.tools import TOOL_USE_GUIDANCE, ToolDefinition, ToolExecutionContext
from memory_system.working_memory import InMemoryWorkingMemory

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
TOOL = ToolDefinition(
    name="read_thing", description="Reads a thing.",
    parameters_schema={"type": "object", "properties": {}, "additionalProperties": False},
)
CALL = ToolCallRequest(tool_name="read_thing", parameters={})


class ScriptedToolModel:
    """Plays back scripted ModelTurns / exceptions; records every call."""

    def __init__(self, *script) -> None:
        self._script = list(script)
        self.tool_calls_made: list[tuple[ModelGenerationRequest, tuple]] = []
        self.plain_calls: list[ModelGenerationRequest] = []

    def generate(self, *, request) -> str:
        self.plain_calls.append(request)
        return "plain reply"

    def generate_with_tools(self, *, request, tools):
        self.tool_calls_made.append((request, tools))
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class PlainModel:
    def __init__(self) -> None:
        self.calls = []

    def generate(self, *, request) -> str:
        self.calls.append(request)
        return "plain reply"


class FakeExecutor:
    def __init__(self, result: ToolExecutionResult | Exception | None = None) -> None:
        self._result = result or ToolExecutionResult(tool_name="read_thing", ok=True, content='{"v": 1}', data="payload")
        self.contexts: list[ToolExecutionContext] = []
        self.calls: list[ToolCallRequest] = []

    @property
    def definitions(self):
        return (TOOL,)

    def execute(self, call, *, context):
        self.calls.append(call)
        self.contexts.append(context)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _engine(model, executor=None):
    memory = InMemoryWorkingMemory(max_exchanges_per_subject=10, max_characters_per_subject=8000)
    engine = ConversationEngine(
        model=model, working_memory_reader=memory, working_memory_writer=memory,
        queue=SubjectConversationQueue(), tool_executor=executor,
    )
    return engine, memory


def _ask(engine, text="how long is left?"):
    return engine.generate_response(subject_key="user-1", current_user_message=text, language="en", identity_id="alex", now=NOW)


@pytest.fixture(autouse=True)
def _identity_available():
    # identity_id "alex" is a real catalog entry (used throughout the existing suite).
    yield


class TestNoExecutorKeepsThePlainPath:
    def test_plain_generate_is_used_when_no_executor_is_configured(self) -> None:
        model = ScriptedToolModel()
        engine, _ = _engine(model)
        assert _ask(engine).text == "plain reply"
        assert model.tool_calls_made == [] and len(model.plain_calls) == 1

    def test_no_guidance_message_is_added_on_the_plain_path(self) -> None:
        model = ScriptedToolModel()
        engine, _ = _engine(model)
        _ask(engine)
        assert all(m.content != TOOL_USE_GUIDANCE for m in model.plain_calls[0].messages)

    def test_non_tool_capable_model_with_executor_disables_tools_and_warns(self, caplog) -> None:
        model = PlainModel()
        with caplog.at_level(logging.WARNING, logger="ai_coach_keyholder.conversation_engine"):
            engine, _ = _engine(model, FakeExecutor())
        assert any(r.error_code == "tools_model_unsupported" for r in caplog.records if hasattr(r, "error_code"))
        assert _ask(engine).text == "plain reply"
        assert len(model.calls) == 1


class TestDirectAnswerWithoutAToolCall:
    def test_model_answering_directly_makes_no_tool_call(self) -> None:
        model = ScriptedToolModel(ModelTurn(text="Hello there"))
        executor = FakeExecutor()
        engine, memory = _engine(model, executor)
        response = _ask(engine, "hi")
        assert response.text == "Hello there" and response.tool_outputs == ()
        assert executor.calls == []
        assert len(model.tool_calls_made) == 1

    def test_tools_are_offered_on_the_first_request(self) -> None:
        model = ScriptedToolModel(ModelTurn(text="ok"))
        engine, _ = _engine(model, FakeExecutor())
        _ask(engine)
        assert model.tool_calls_made[0][1] == (TOOL,)

    def test_a_direct_answer_is_committed_to_working_memory(self) -> None:
        model = ScriptedToolModel(ModelTurn(text="Hello there"))
        engine, memory = _engine(model, FakeExecutor())
        _ask(engine, "hi")
        assert len(memory.read(subject_key="user-1").turns) == 2


class TestGuidancePlacement:
    def test_guidance_is_a_separate_system_message_after_the_original_one(self) -> None:
        model = ScriptedToolModel(ModelTurn(text="ok"))
        engine, _ = _engine(model, FakeExecutor())
        _ask(engine)
        messages = model.tool_calls_made[0][0].messages
        assert messages[0].role == ModelMessageRole.SYSTEM and messages[0].content != TOOL_USE_GUIDANCE
        assert messages[1].role == ModelMessageRole.SYSTEM and messages[1].content == build_guardrails()
        assert messages[2].role == ModelMessageRole.SYSTEM and messages[2].content == TOOL_USE_GUIDANCE
        assert messages[-1].role == ModelMessageRole.USER

    def test_original_system_boundaries_are_untouched(self) -> None:
        model = ScriptedToolModel(ModelTurn(text="ok"))
        engine, _ = _engine(model, FakeExecutor())
        _ask(engine)
        assert _SYSTEM_BOUNDARIES in model.tool_calls_made[0][0].messages[0].content
        assert TOOL_USE_GUIDANCE not in model.tool_calls_made[0][0].messages[0].content


class TestGuidanceIsLastBeforeTheUserTurn:
    """The older general system text advises `lock status`/`lock report ...`;
    the tool guidance must sit AFTER all history, right before the final
    user message, so the nearest instruction wins."""

    def test_guidance_comes_after_working_memory_history(self) -> None:
        model = ScriptedToolModel(ModelTurn(text="a"), ModelTurn(text="b"))
        engine, _ = _engine(model, FakeExecutor())
        _ask(engine, "first")
        _ask(engine, "second")
        messages = model.tool_calls_made[1][0].messages
        assert messages[-1].role == ModelMessageRole.USER and messages[-1].content == "second"
        assert messages[-2].role == ModelMessageRole.SYSTEM and messages[-2].content.startswith(TOOL_USE_GUIDANCE)
        assert [m.role for m in messages[1:-3]] == [ModelMessageRole.USER, ModelMessageRole.ASSISTANT]
        assert messages[-3].content == build_guardrails()

    def test_executor_guidance_is_appended_after_the_generic_guidance(self) -> None:
        class Guided(FakeExecutor):
            guidance = "ROUTING RULE X"
        model = ScriptedToolModel(ModelTurn(text="ok"))
        engine, _ = _engine(model, Guided())
        _ask(engine)
        content = model.tool_calls_made[0][0].messages[-2].content
        assert content.startswith(TOOL_USE_GUIDANCE) and content.endswith("ROUTING RULE X")

    def test_executor_without_guidance_gets_only_the_generic_text(self) -> None:
        model = ScriptedToolModel(ModelTurn(text="ok"))
        engine, _ = _engine(model, FakeExecutor())
        _ask(engine)
        assert model.tool_calls_made[0][0].messages[-2].content == TOOL_USE_GUIDANCE

    def test_follow_up_request_still_carries_the_guidance(self) -> None:
        model = ScriptedToolModel(ModelTurn(tool_calls=(CALL,)), ModelTurn(text="ok"))
        engine, _ = _engine(model, FakeExecutor())
        _ask(engine)
        assert any(m.content.startswith(TOOL_USE_GUIDANCE) for m in model.tool_calls_made[1][0].messages)


class TestToolRound:
    def test_tool_is_executed_and_the_final_text_is_returned(self) -> None:
        model = ScriptedToolModel(ModelTurn(tool_calls=(CALL,)), ModelTurn(text="3 days left"))
        executor = FakeExecutor()
        engine, _ = _engine(model, executor)
        response = _ask(engine)
        assert response.text == "3 days left"
        assert executor.calls == [CALL]

    def test_executor_receives_the_subject_key_and_injected_time(self) -> None:
        model = ScriptedToolModel(ModelTurn(tool_calls=(CALL,)), ModelTurn(text="ok"))
        executor = FakeExecutor()
        engine, _ = _engine(model, executor)
        _ask(engine)
        assert executor.contexts[0].subject_key == "user-1" and executor.contexts[0].now == NOW

    def test_follow_up_carries_the_assistant_call_and_the_tool_result(self) -> None:
        model = ScriptedToolModel(ModelTurn(tool_calls=(CALL,)), ModelTurn(text="ok"))
        engine, _ = _engine(model, FakeExecutor())
        _ask(engine)
        follow_up = model.tool_calls_made[1][0].messages
        assert follow_up[-2].role == ModelMessageRole.ASSISTANT and follow_up[-2].tool_calls == (CALL,)
        assert follow_up[-1].role == ModelMessageRole.TOOL
        assert follow_up[-1].content == '{"v": 1}' and follow_up[-1].tool_name == "read_thing"

    def test_follow_up_offers_no_tools_so_a_second_round_is_impossible(self) -> None:
        model = ScriptedToolModel(ModelTurn(tool_calls=(CALL,)), ModelTurn(text="ok"))
        engine, _ = _engine(model, FakeExecutor())
        _ask(engine)
        assert model.tool_calls_made[1][1] == ()

    def test_tool_outputs_are_returned_on_the_response(self) -> None:
        model = ScriptedToolModel(ModelTurn(tool_calls=(CALL,)), ModelTurn(text="ok"))
        engine, _ = _engine(model, FakeExecutor())
        response = _ask(engine)
        assert len(response.tool_outputs) == 1 and response.tool_outputs[0].data == "payload"

    def test_a_model_that_asks_for_a_second_round_gets_the_fallback(self) -> None:
        model = ScriptedToolModel(ModelTurn(tool_calls=(CALL,)), ModelTurn(tool_calls=(CALL,)))
        executor = FakeExecutor()
        engine, _ = _engine(model, executor)
        response = _ask(engine)
        assert response.tool_outputs == ()
        assert len(executor.calls) == 1
        assert len(model.tool_calls_made) == 2   # never a third call

    def test_extra_calls_in_one_reply_are_dropped(self) -> None:
        model = ScriptedToolModel(ModelTurn(tool_calls=(CALL, CALL, CALL)), ModelTurn(text="ok"))
        executor = FakeExecutor()
        engine, _ = _engine(model, executor)
        _ask(engine)
        assert len(executor.calls) == MAX_TOOL_CALLS_PER_MESSAGE == 1

    def test_tool_augmented_exchange_is_not_committed_to_working_memory(self) -> None:
        model = ScriptedToolModel(ModelTurn(tool_calls=(CALL,)), ModelTurn(text="3 days left"))
        engine, memory = _engine(model, FakeExecutor())
        _ask(engine)
        assert memory.read(subject_key="user-1").turns == ()

    def test_tool_result_text_never_reaches_working_memory(self) -> None:
        model = ScriptedToolModel(ModelTurn(tool_calls=(CALL,)), ModelTurn(text="ok"), ModelTurn(text="again"))
        engine, memory = _engine(model, FakeExecutor())
        _ask(engine)
        _ask(engine, "unrelated")
        assert all('{"v": 1}' not in t.content for t in memory.read(subject_key="user-1").turns)


class TestErrorRecovery:
    def test_unknown_tool_name_yields_an_error_result_and_still_answers(self) -> None:
        model = ScriptedToolModel(
            ModelTurn(tool_calls=(ToolCallRequest(tool_name="launch_missiles", parameters={}),)), ModelTurn(text="Sorry"),
        )
        executor = FakeExecutor()
        engine, _ = _engine(model, executor)
        response = _ask(engine)
        assert response.text == "Sorry"
        assert executor.calls == []          # never reached the executor
        assert response.tool_outputs[0].ok is False
        assert "unknown_tool" in model.tool_calls_made[1][0].messages[-1].content

    def test_executor_exception_is_contained_and_the_model_can_still_answer(self, caplog) -> None:
        model = ScriptedToolModel(ModelTurn(tool_calls=(CALL,)), ModelTurn(text="I couldn't check that."))
        engine, _ = _engine(model, FakeExecutor(RuntimeError("boom")))
        with caplog.at_level(logging.WARNING, logger="ai_coach_keyholder.conversation_engine"):
            response = _ask(engine)
        assert response.text == "I couldn't check that."
        assert response.tool_outputs[0].ok is False
        assert "tool_error" in model.tool_calls_made[1][0].messages[-1].content
        assert "boom" not in model.tool_calls_made[1][0].messages[-1].content
        assert any(getattr(r, "error_code", None) == "tool_execution_failed" for r in caplog.records)

    def test_executor_returning_the_wrong_type_is_treated_as_a_failure(self) -> None:
        class Bad(FakeExecutor):
            def execute(self, call, *, context):
                return "not a result"
        model = ScriptedToolModel(ModelTurn(tool_calls=(CALL,)), ModelTurn(text="ok"))
        engine, _ = _engine(model, Bad())
        assert _ask(engine).tool_outputs[0].ok is False

    def test_executor_returning_a_mismatched_tool_name_is_treated_as_a_failure(self) -> None:
        wrong = ToolExecutionResult(tool_name="other", ok=True, content="{}")
        model = ScriptedToolModel(ModelTurn(tool_calls=(CALL,)), ModelTurn(text="ok"))
        engine, _ = _engine(model, FakeExecutor(wrong))
        assert _ask(engine).tool_outputs[0].ok is False

    def test_model_failure_on_the_first_call_gives_the_fallback(self) -> None:
        model = ScriptedToolModel(LLMGenerationError("ollama_timeout"))
        engine, _ = _engine(model, FakeExecutor())
        response = _ask(engine)
        assert response.tool_outputs == () and response.text

    def test_model_failure_after_the_tool_ran_gives_the_fallback(self) -> None:
        model = ScriptedToolModel(ModelTurn(tool_calls=(CALL,)), LLMGenerationError("ollama_timeout"))
        engine, memory = _engine(model, FakeExecutor())
        response = _ask(engine)
        assert response.tool_outputs == ()
        assert memory.read(subject_key="user-1").turns == ()

    def test_tools_unsupported_falls_back_to_the_plain_path(self) -> None:
        model = ScriptedToolModel(LLMGenerationError("ollama_tools_unsupported"))
        executor = FakeExecutor()
        engine, memory = _engine(model, executor)
        response = _ask(engine)
        assert response.text == "plain reply" and response.tool_outputs == ()
        assert executor.calls == [] and len(model.plain_calls) == 1
        assert len(memory.read(subject_key="user-1").turns) == 2   # a normal exchange

    def test_plain_path_after_unsupported_uses_the_original_request_without_guidance(self) -> None:
        model = ScriptedToolModel(LLMGenerationError("ollama_tools_unsupported"))
        engine, _ = _engine(model, FakeExecutor())
        _ask(engine)
        assert all(m.content != TOOL_USE_GUIDANCE for m in model.plain_calls[0].messages)


class TestReadOnlyBoundary:
    def test_engine_module_has_no_chaster_or_domain_imports(self) -> None:
        from pathlib import Path
        for name in ("engine.py", "tools.py", "model_types.py", "ollama_adapter.py"):
            source = (Path(__file__).parent.parent.parent / "conversation_engine" / name).read_text(encoding="utf-8")
            imports = [l for l in source.splitlines() if l.startswith(("import ", "from "))]
            assert not any(("chaster" in l or "lock_state" in l or "task_runtime" in l or "application" in l) for l in imports), name

    def test_plan_tool_calls_stay_empty(self) -> None:
        from conversation_engine.planning import build_response_plan  # noqa: F401
        model = ScriptedToolModel(ModelTurn(tool_calls=(CALL,)), ModelTurn(text="ok"))
        engine, _ = _engine(model, FakeExecutor())
        assert _ask(engine).text == "ok"   # validate_response still rejects any non-empty plan.tool_calls


class ForcingExecutor(FakeExecutor):
    """Deterministic matcher: forces the tool for any message containing 'zamek'."""

    def match_forced_call(self, message):
        return CALL if "zamek" in message else None


class TestForcedToolCall:
    def test_forced_call_runs_the_tool_without_asking_the_model_to_choose_it(self) -> None:
        model = ScriptedToolModel(ModelTurn(text="Stav je v poradku"))          # ONLY the final text is scripted
        executor = ForcingExecutor()
        engine, _ = _engine(model, executor)
        response = _ask(engine, "jak je na tom muj zamek")
        assert response.text == "Stav je v poradku"
        assert executor.calls == [CALL]
        assert len(model.tool_calls_made) == 1             # the first model call IS the follow-up

    def test_the_only_model_call_already_contains_the_tool_result_and_offers_no_tools(self) -> None:
        model = ScriptedToolModel(ModelTurn(text="ok"))
        engine, _ = _engine(model, ForcingExecutor())
        _ask(engine, "zamek?")
        request, tools = model.tool_calls_made[0]
        assert tools == ()
        assert request.messages[-2].role == ModelMessageRole.ASSISTANT and request.messages[-2].tool_calls == (CALL,)
        assert request.messages[-1].role == ModelMessageRole.TOOL and request.messages[-1].content == '{"v": 1}'

    def test_tool_guidance_is_still_present_on_the_forced_path(self) -> None:
        model = ScriptedToolModel(ModelTurn(text="ok"))
        engine, _ = _engine(model, ForcingExecutor())
        _ask(engine, "zamek?")
        assert any(m.content.startswith(TOOL_USE_GUIDANCE) for m in model.tool_calls_made[0][0].messages)

    def test_forced_exchange_returns_tool_outputs_and_is_not_committed(self) -> None:
        model = ScriptedToolModel(ModelTurn(text="ok"))
        engine, memory = _engine(model, ForcingExecutor())
        response = _ask(engine, "zamek?")
        assert response.tool_outputs[0].data == "payload"
        assert memory.read(subject_key="user-1").turns == ()

    def test_non_matching_message_uses_the_normal_model_chosen_path(self) -> None:
        model = ScriptedToolModel(ModelTurn(text="Hello"))
        executor = ForcingExecutor()
        engine, _ = _engine(model, executor)
        assert _ask(engine, "good morning").text == "Hello"
        assert executor.calls == [] and model.tool_calls_made[0][1] == (TOOL,)

    def test_executor_without_a_matcher_is_unaffected(self) -> None:
        model = ScriptedToolModel(ModelTurn(text="Hello"))
        engine, _ = _engine(model, FakeExecutor())
        _ask(engine, "zamek?")
        assert model.tool_calls_made[0][1] == (TOOL,)

    def test_matcher_exception_degrades_to_the_model_chosen_path(self, caplog) -> None:
        class Broken(FakeExecutor):
            def match_forced_call(self, message):
                raise RuntimeError("bug")
        model = ScriptedToolModel(ModelTurn(text="still works"))
        engine, _ = _engine(model, Broken())
        with caplog.at_level(logging.WARNING, logger="ai_coach_keyholder.conversation_engine"):
            assert _ask(engine, "zamek?").text == "still works"
        assert any(getattr(r, "error_code", None) == "forced_tool_matcher_failed" for r in caplog.records)

    @pytest.mark.parametrize("bogus", ["not a call", ToolCallRequest(tool_name="launch_missiles", parameters={})])
    def test_invalid_matcher_result_is_ignored(self, bogus) -> None:
        class Bogus(FakeExecutor):
            def match_forced_call(self, message):
                return bogus
        model = ScriptedToolModel(ModelTurn(text="normal"))
        executor = Bogus()
        engine, _ = _engine(model, executor)
        assert _ask(engine, "zamek?").text == "normal"
        assert executor.calls == []

    def test_failing_tool_on_the_forced_path_still_lets_the_model_explain(self) -> None:
        failing = ToolExecutionResult(tool_name="read_thing", ok=False, content='{"status": "not_connected"}')
        class Forcing(ForcingExecutor):
            pass
        executor = Forcing(failing)
        model = ScriptedToolModel(ModelTurn(text="Run chaster connect"))
        engine, _ = _engine(model, executor)
        response = _ask(engine, "zamek?")
        assert response.text == "Run chaster connect" and response.tool_outputs[0].ok is False

    def test_executor_exception_on_the_forced_path_is_contained(self) -> None:
        model = ScriptedToolModel(ModelTurn(text="could not check"))
        engine, _ = _engine(model, ForcingExecutor(RuntimeError("boom")))
        response = _ask(engine, "zamek?")
        assert response.text == "could not check" and response.tool_outputs[0].ok is False

    def test_model_failure_after_a_forced_tool_gives_the_deterministic_fallback(self) -> None:
        model = ScriptedToolModel(LLMGenerationError("ollama_timeout"))
        engine, memory = _engine(model, ForcingExecutor())
        response = _ask(engine, "zamek?")
        assert response.tool_outputs == () and response.text
        assert memory.read(subject_key="user-1").turns == ()

    def test_a_forced_call_makes_exactly_one_tool_execution(self) -> None:
        model = ScriptedToolModel(ModelTurn(text="ok"))
        executor = ForcingExecutor()
        engine, _ = _engine(model, executor)
        _ask(engine, "zamek?")
        assert len(executor.calls) == 1

    def test_a_follow_up_that_asks_for_another_tool_round_gets_the_fallback(self) -> None:
        model = ScriptedToolModel(ModelTurn(tool_calls=(CALL,)))
        engine, _ = _engine(model, ForcingExecutor())
        response = _ask(engine, "zamek?")
        assert response.tool_outputs == ()


class TestToolingVisibility:
    def test_engine_logs_when_tool_calling_is_active(self, caplog) -> None:
        with caplog.at_level(logging.INFO, logger="ai_coach_keyholder.conversation_engine"):
            _engine(ScriptedToolModel(), FakeExecutor())
        assert any("tool calling ACTIVE" in r.getMessage() for r in caplog.records)


class TestExtraSystemContext:
    """Verified application data for one reply: tools never offered,
    context placed last before the user turn, nothing remembered."""
    CONTEXT = "SYSTEM CONTEXT (VERIFIED CHASTER DATA)\nLock details: []"

    def _ask_with_context(self, engine, context=CONTEXT):
        return engine.generate_response(
            subject_key="user-1", current_user_message="how long is left?", language="en",
            identity_id="alex", now=NOW, extra_system_context=context,
        )

    def test_uses_the_plain_path_even_when_a_tool_executor_is_configured(self) -> None:
        model = ScriptedToolModel()
        engine, _ = _engine(model, FakeExecutor())
        assert self._ask_with_context(engine).text == "plain reply"
        assert model.tool_calls_made == [] and len(model.plain_calls) == 1

    def test_context_is_the_system_message_right_before_the_user_turn(self) -> None:
        model = ScriptedToolModel()
        engine, _ = _engine(model)
        self._ask_with_context(engine)
        messages = model.plain_calls[0].messages
        assert messages[-1].role == ModelMessageRole.USER
        assert messages[-2].role == ModelMessageRole.SYSTEM and messages[-2].content == self.CONTEXT
        assert any(_SYSTEM_BOUNDARIES in m.content for m in messages)  # original system text untouched

    def test_exchange_is_not_committed_to_working_memory(self) -> None:
        model = ScriptedToolModel()
        engine, memory = _engine(model)
        self._ask_with_context(engine)
        assert memory.read(subject_key="user-1").turns == ()

    def test_blank_context_fails_fast(self) -> None:
        engine, _ = _engine(ScriptedToolModel())
        with pytest.raises(ValueError):
            self._ask_with_context(engine, "   ")

    def test_absent_context_keeps_the_existing_behaviour(self) -> None:
        engine, memory = _engine(ScriptedToolModel())
        _ask(engine)
        assert len(memory.read(subject_key="user-1").turns) == 2


class TestRememberExchange:
    def test_remember_exchange_commits_text_but_not_the_context(self) -> None:
        engine, memory = _engine(ScriptedToolModel())
        engine.generate_response(
            subject_key="user-1", current_user_message="hi", language="en", identity_id="alex", now=NOW,
            extra_system_context="VERIFIED CHASTER DATA", remember_exchange=True,
        )
        turns = memory.read(subject_key="user-1").turns
        assert [t.content for t in turns] == ["hi", "plain reply"]

    def test_default_is_still_not_remembered(self) -> None:
        engine, memory = _engine(ScriptedToolModel())
        engine.generate_response(
            subject_key="user-1", current_user_message="hi", language="en", identity_id="alex", now=NOW,
            extra_system_context="VERIFIED CHASTER DATA",
        )
        assert memory.read(subject_key="user-1").turns == ()
