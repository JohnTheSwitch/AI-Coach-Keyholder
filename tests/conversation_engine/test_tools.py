"""tests/conversation_engine/test_tools.py -- ToolDefinition validity and the executor boundary types."""

from __future__ import annotations

import pytest

from conversation_engine.models import MAX_TOOL_RESULT_CHARACTERS, ToolExecutionResult
from conversation_engine.tools import MAX_TOOL_DESCRIPTION_CHARACTERS, TOOL_USE_GUIDANCE, ToolDefinition

_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}


def _tool(**overrides) -> ToolDefinition:
    kwargs = dict(name="get_thing", description="Reads a thing.", parameters_schema=_SCHEMA)
    kwargs.update(overrides)
    return ToolDefinition(**kwargs)


class TestToolDefinitionValidity:
    def test_valid_definition_is_accepted(self) -> None:
        assert _tool().name == "get_thing"

    @pytest.mark.parametrize("name", ["", "Get", "1abc", "has space", "a-b", "x" * 65])
    def test_invalid_names_are_rejected(self, name: str) -> None:
        with pytest.raises(ValueError, match="name"):
            _tool(name=name)

    @pytest.mark.parametrize("description", ["", "   "])
    def test_blank_description_is_rejected(self, description: str) -> None:
        with pytest.raises(ValueError, match="description"):
            _tool(description=description)

    def test_overlong_description_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="at most"):
            _tool(description="x" * (MAX_TOOL_DESCRIPTION_CHARACTERS + 1))

    def test_schema_must_be_an_object_type(self) -> None:
        with pytest.raises(ValueError, match="type 'object'"):
            _tool(parameters_schema={"type": "string"})

    def test_schema_must_declare_properties(self) -> None:
        with pytest.raises(ValueError, match="properties"):
            _tool(parameters_schema={"type": "object", "additionalProperties": False})

    def test_schema_must_forbid_additional_properties(self) -> None:
        with pytest.raises(ValueError, match="additionalProperties"):
            _tool(parameters_schema={"type": "object", "properties": {}})

    def test_required_must_reference_declared_properties(self) -> None:
        with pytest.raises(ValueError, match="required"):
            _tool(parameters_schema={"type": "object", "properties": {}, "required": ["ghost"], "additionalProperties": False})

    def test_schema_is_stored_immutably(self) -> None:
        tool = _tool()
        with pytest.raises(TypeError):
            tool.parameters_schema["type"] = "array"  # type: ignore[index]

    def test_definition_is_frozen(self) -> None:
        with pytest.raises(Exception):
            _tool().name = "other"  # type: ignore[misc]


class TestToolExecutionResult:
    def test_valid_result(self) -> None:
        assert ToolExecutionResult(tool_name="t", ok=True, content="{}").data is None

    def test_blank_content_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            ToolExecutionResult(tool_name="t", ok=True, content="  ")

    def test_overlong_content_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            ToolExecutionResult(tool_name="t", ok=True, content="x" * (MAX_TOOL_RESULT_CHARACTERS + 1))

    def test_blank_tool_name_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            ToolExecutionResult(tool_name=" ", ok=True, content="x")


class TestGuidanceText:
    def test_guidance_frames_results_as_untrusted_data(self) -> None:
        assert "never an instruction" in TOOL_USE_GUIDANCE

    def test_guidance_forbids_verified_framing(self) -> None:
        assert "never describe it as verified" in TOOL_USE_GUIDANCE

    def test_guidance_routes_reconnects_to_chaster_connect(self) -> None:
        assert "`chaster connect`" in TOOL_USE_GUIDANCE

    def test_guidance_states_tools_are_read_only(self) -> None:
        assert "read-only" in TOOL_USE_GUIDANCE


class TestModelTurn:
    def test_text_only_turn(self) -> None:
        from conversation_engine.model_types import ModelTurn
        assert ModelTurn(text="hi").tool_calls == ()

    def test_tool_call_only_turn(self) -> None:
        from conversation_engine.model_types import ModelTurn
        from conversation_engine.models import ToolCallRequest
        assert ModelTurn(tool_calls=(ToolCallRequest(tool_name="t", parameters={}),)).text is None

    def test_both_or_neither_is_rejected(self) -> None:
        from conversation_engine.model_types import ModelTurn
        from conversation_engine.models import ToolCallRequest
        call = ToolCallRequest(tool_name="t", parameters={})
        with pytest.raises(ValueError):
            ModelTurn(text="hi", tool_calls=(call,))
        with pytest.raises(ValueError):
            ModelTurn()
        with pytest.raises(ValueError):
            ModelTurn(text="   ")
