"""tests/conversation_engine/test_ollama_tool_calling.py -- Ollama adapter's tools path (all HTTP mocked)."""

from __future__ import annotations

import json
from unittest.mock import Mock, patch

import pytest
import requests

from conversation_engine.model_types import (
    LLMGenerationError, ModelGenerationRequest, ModelMessage, ModelMessageRole, ToolCapableConversationModel,
)
from conversation_engine.models import ToolCallRequest
from conversation_engine.ollama_adapter import OllamaConversationModel
from conversation_engine.tools import ToolDefinition

TOOL = ToolDefinition(
    name="get_chaster_status", description="Reads status.",
    parameters_schema={"type": "object", "properties": {}, "additionalProperties": False},
)


def _response(body: dict | bytes, *, status_code: int = 200) -> Mock:
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    response = Mock(spec=requests.Response)
    response.status_code = status_code
    response.headers = {"Content-Type": "application/json"}
    response.iter_content = lambda chunk_size: iter([raw])
    response.close = Mock()
    return response


def _model() -> OllamaConversationModel:
    return OllamaConversationModel(host="http://localhost:11434", model_name="llama3.1")


def _request(*messages: ModelMessage) -> ModelGenerationRequest:
    return ModelGenerationRequest(
        messages=messages or (ModelMessage(role=ModelMessageRole.USER, content="hi"),), max_output_characters=500,
    )


def _sent_json(mock_post: Mock) -> dict:
    return mock_post.call_args.kwargs["json"]


class TestProtocol:
    def test_adapter_is_tool_capable(self) -> None:
        assert isinstance(_model(), ToolCapableConversationModel)


class TestRequestPayload:
    def test_plain_generate_sends_no_tools_key_and_unchanged_message_shape(self) -> None:
        with patch("requests.post", return_value=_response({"message": {"content": "ok"}})) as post:
            _model().generate(request=_request())
        body = _sent_json(post)
        assert "tools" not in body
        assert body["messages"] == [{"role": "user", "content": "hi"}]

    def test_tools_are_sent_in_ollamas_native_function_format(self) -> None:
        with patch("requests.post", return_value=_response({"message": {"content": "ok"}})) as post:
            _model().generate_with_tools(request=_request(), tools=(TOOL,))
        sent = _sent_json(post)["tools"]
        assert sent == [{"type": "function", "function": {
            "name": "get_chaster_status", "description": "Reads status.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        }}]

    def test_empty_tools_sends_no_tools_key(self) -> None:
        with patch("requests.post", return_value=_response({"message": {"content": "ok"}})) as post:
            _model().generate_with_tools(request=_request(), tools=())
        assert "tools" not in _sent_json(post)

    def test_assistant_tool_calls_and_tool_results_are_serialized(self) -> None:
        call = ToolCallRequest(tool_name="get_chaster_status", parameters={})
        request = _request(
            ModelMessage(role=ModelMessageRole.USER, content="status?"),
            ModelMessage(role=ModelMessageRole.ASSISTANT, content="", tool_calls=(call,)),
            ModelMessage(role=ModelMessageRole.TOOL, content='{"status": "ok"}', tool_name="get_chaster_status"),
        )
        with patch("requests.post", return_value=_response({"message": {"content": "done"}})) as post:
            _model().generate_with_tools(request=request, tools=())
        messages = _sent_json(post)["messages"]
        assert messages[1]["tool_calls"] == [{"function": {"name": "get_chaster_status", "arguments": {}}}]
        assert messages[2] == {"role": "tool", "content": '{"status": "ok"}', "tool_name": "get_chaster_status"}


class TestResponseParsing:
    def test_plain_text_reply_becomes_a_text_turn(self) -> None:
        with patch("requests.post", return_value=_response({"message": {"content": " hello "}})):
            turn = _model().generate_with_tools(request=_request(), tools=(TOOL,))
        assert turn.text == "hello" and turn.tool_calls == ()

    def test_tool_call_reply_becomes_a_tool_turn(self) -> None:
        body = {"message": {"content": "", "tool_calls": [{"function": {"name": "get_chaster_status", "arguments": {}}}]}}
        with patch("requests.post", return_value=_response(body)):
            turn = _model().generate_with_tools(request=_request(), tools=(TOOL,))
        assert turn.text is None
        assert turn.tool_calls == (ToolCallRequest(tool_name="get_chaster_status", parameters={}),)

    def test_text_alongside_tool_calls_is_discarded(self) -> None:
        body = {"message": {"content": "let me check", "tool_calls": [{"function": {"name": "t", "arguments": {}}}]}}
        with patch("requests.post", return_value=_response(body)):
            turn = _model().generate_with_tools(request=_request(), tools=(TOOL,))
        assert turn.text is None and len(turn.tool_calls) == 1

    def test_string_arguments_are_json_decoded(self) -> None:
        body = {"message": {"content": "", "tool_calls": [{"function": {"name": "t", "arguments": '{"a": 1}'}}]}}
        with patch("requests.post", return_value=_response(body)):
            turn = _model().generate_with_tools(request=_request(), tools=(TOOL,))
        assert dict(turn.tool_calls[0].parameters) == {"a": 1}

    @pytest.mark.parametrize("bad_call", [
        {}, {"function": {}}, {"function": {"name": ""}}, {"function": {"name": 5}},
        {"function": {"name": "t", "arguments": "not json"}}, {"function": {"name": "t", "arguments": [1]}}, "str",
    ])
    def test_malformed_tool_calls_raise_shape_error(self, bad_call) -> None:
        body = {"message": {"content": "", "tool_calls": [bad_call]}}
        with patch("requests.post", return_value=_response(body)):
            with pytest.raises(LLMGenerationError) as excinfo:
                _model().generate_with_tools(request=_request(), tools=(TOOL,))
        assert excinfo.value.code == "ollama_invalid_response_shape"

    def test_tool_calls_not_a_list_raises_shape_error(self) -> None:
        with patch("requests.post", return_value=_response({"message": {"tool_calls": "oops"}})):
            with pytest.raises(LLMGenerationError) as excinfo:
                _model().generate_with_tools(request=_request(), tools=(TOOL,))
        assert excinfo.value.code == "ollama_invalid_response_shape"

    def test_empty_text_without_calls_raises_empty_response(self) -> None:
        with patch("requests.post", return_value=_response({"message": {"content": "  "}})):
            with pytest.raises(LLMGenerationError) as excinfo:
                _model().generate_with_tools(request=_request(), tools=(TOOL,))
        assert excinfo.value.code == "ollama_empty_response"

    def test_overlong_text_is_rejected(self) -> None:
        with patch("requests.post", return_value=_response({"message": {"content": "x" * 600}})):
            with pytest.raises(LLMGenerationError) as excinfo:
                _model().generate_with_tools(request=_request(), tools=(TOOL,))
        assert excinfo.value.code == "ollama_response_too_long"


class TestFailureCodes:
    def test_http_400_saying_tools_unsupported_is_reported_distinctly(self) -> None:
        response = _response(b'{"error": "registry.ollama.ai/library/gemma does not support tools"}', status_code=400)
        with patch("requests.post", return_value=response):
            with pytest.raises(LLMGenerationError) as excinfo:
                _model().generate_with_tools(request=_request(), tools=(TOOL,))
        assert excinfo.value.code == "ollama_tools_unsupported"

    def test_other_http_400_stays_an_ordinary_http_error(self) -> None:
        with patch("requests.post", return_value=_response(b'{"error": "bad"}', status_code=400)):
            with pytest.raises(LLMGenerationError) as excinfo:
                _model().generate_with_tools(request=_request(), tools=(TOOL,))
        assert excinfo.value.code == "ollama_http_error"

    def test_unsupported_detection_is_not_applied_without_tools(self) -> None:
        response = _response(b'{"error": "does not support tools"}', status_code=400)
        with patch("requests.post", return_value=response):
            with pytest.raises(LLMGenerationError) as excinfo:
                _model().generate_with_tools(request=_request(), tools=())
        assert excinfo.value.code == "ollama_http_error"

    def test_timeout_and_connection_errors_keep_their_codes(self) -> None:
        with patch("requests.post", side_effect=requests.Timeout()):
            with pytest.raises(LLMGenerationError) as excinfo:
                _model().generate_with_tools(request=_request(), tools=(TOOL,))
        assert excinfo.value.code == "ollama_timeout"
        with patch("requests.post", side_effect=requests.ConnectionError()):
            with pytest.raises(LLMGenerationError) as excinfo:
                _model().generate_with_tools(request=_request(), tools=(TOOL,))
        assert excinfo.value.code == "ollama_connection_error"

    def test_response_is_always_closed(self) -> None:
        response = _response({"message": {"content": "ok"}})
        with patch("requests.post", return_value=response):
            _model().generate_with_tools(request=_request(), tools=(TOOL,))
        response.close.assert_called_once()
