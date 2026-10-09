"""
conversation_engine/ollama_adapter.py

OllamaConversationModel -- the ONLY file in this project that imports
`requests` for LLM communication. Knows nothing about
ResponseContextSnapshot, ResponsePlan, the recent-history buffer, or
ConversationResponse -- only ModelGenerationRequest -> str
(model_types.py's own ConversationModel Protocol), plus the additive
`generate_with_tools()` (ToolCapableConversationModel) for read-only,
model-initiated tool calls.
"""

from __future__ import annotations

import json

import requests

from conversation_engine.model_types import (
    LLMGenerationError,
    ModelGenerationRequest,
    ModelMessage,
    ModelTurn,
)
from conversation_engine.models import ToolCallRequest
from conversation_engine.tools import ToolDefinition

__all__ = [
    "OllamaConversationModel",
    "DEFAULT_CONNECT_TIMEOUT_SECONDS",
    "DEFAULT_READ_TIMEOUT_SECONDS",
    "DEFAULT_MAX_RESPONSE_BODY_BYTES",
    "DEFAULT_HTTP_CHUNK_SIZE_BYTES",
]

DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
DEFAULT_READ_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_RESPONSE_BODY_BYTES = 1_000_000
DEFAULT_HTTP_CHUNK_SIZE_BYTES = 8192  # a reasonable default read-chunk size only --
                                       # the actual safety limit is max_bytes itself, not this.


def _is_accepted_content_type(content_type: str) -> bool:
    """Accepts 'application/json' and 'application/json; charset=utf-8'
    (case-insensitive on both the media type and the charset value).
    Rejects any other media type, and any explicitly different charset."""
    parts = [p.strip() for p in content_type.split(";")]
    if not parts or parts[0].lower() != "application/json":
        return False
    for param in parts[1:]:
        if "=" not in param:
            continue
        key, _, value = param.partition("=")
        if key.strip().lower() == "charset" and value.strip().lower() != "utf-8":
            return False
    return True


def _read_bounded_response_body(response: requests.Response, *, max_bytes: int) -> bytes:
    """
    Never uses response.content/.text/.json() -- all three would read
    the entire body before any size check could apply. Checks
    Content-Length first (rejecting a declared size over the limit
    before reading anything), but never trusts it -- the actual byte
    count read via iter_content() is checked on every chunk regardless
    of what the header claimed. Exactly max_bytes is accepted;
    max_bytes + 1 is rejected. Transport errors during iteration
    (a slow/partial read failing mid-stream, distinct from the initial
    connect) are mapped to LLMGenerationError here too, not left to
    propagate as a raw requests exception.
    """
    content_length_header = response.headers.get("Content-Length")
    if content_length_header is not None:
        try:
            declared_length = int(content_length_header)
        except ValueError:
            raise LLMGenerationError("ollama_invalid_content_length")
        if declared_length < 0:
            raise LLMGenerationError("ollama_invalid_content_length")
        if declared_length > max_bytes:
            raise LLMGenerationError("ollama_response_too_large")

    body = bytearray()
    try:
        for chunk in response.iter_content(chunk_size=DEFAULT_HTTP_CHUNK_SIZE_BYTES):
            if not chunk:
                continue
            remaining = max_bytes - len(body)
            if len(chunk) > remaining:
                raise LLMGenerationError("ollama_response_too_large")
            body.extend(chunk)
    except requests.RequestException:
        raise LLMGenerationError("ollama_read_error")
    return bytes(body)


class OllamaConversationModel:
    """Talks to Ollama's own `/api/chat` endpoint. `stream=True` on the
    requests.post() call and Ollama's own `"stream": False` payload
    field are two DIFFERENT, unrelated things -- the former lets THIS
    client bound how much of the HTTP body it reads; the latter tells
    OLLAMA to return one complete JSON object instead of an NDJSON
    stream of partial tokens. Both are used, deliberately, together."""

    def __init__(
        self, *, host: str, model_name: str,
        connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        read_timeout_seconds: float = DEFAULT_READ_TIMEOUT_SECONDS,
        max_response_body_bytes: int = DEFAULT_MAX_RESPONSE_BODY_BYTES,
    ) -> None:
        self._host = host.rstrip("/")
        self._model_name = model_name
        self._connect_timeout = connect_timeout_seconds
        self._read_timeout = read_timeout_seconds
        self._max_body_bytes = max_response_body_bytes

    def generate(self, *, request: ModelGenerationRequest) -> str:
        payload = self._post_chat(request=request, tools=())
        return self._extract_text(payload, max_characters=request.max_output_characters)

    def generate_with_tools(
        self, *, request: ModelGenerationRequest, tools: tuple[ToolDefinition, ...],
    ) -> ModelTurn:
        """Sends Ollama's native `tools` array (when non-empty) and maps
        the reply to a ModelTurn. If the reply contains `tool_calls`,
        any accompanying text is discarded -- the calls win. Otherwise
        the reply is validated exactly like generate()'s. A model that
        does not support tools makes Ollama answer HTTP 400; that is
        reported as `ollama_tools_unsupported` so the engine can fall
        back to the plain path instead of failing every message."""
        payload = self._post_chat(request=request, tools=tools)
        message = payload.get("message") if isinstance(payload, dict) else None
        if not isinstance(message, dict):
            raise LLMGenerationError("ollama_invalid_response_shape")

        raw_calls = message.get("tool_calls")
        if raw_calls:
            if not isinstance(raw_calls, list):
                raise LLMGenerationError("ollama_invalid_response_shape")
            return ModelTurn(tool_calls=tuple(self._parse_tool_call(raw) for raw in raw_calls))
        return ModelTurn(text=self._extract_text(payload, max_characters=request.max_output_characters))

    @staticmethod
    def _parse_tool_call(raw: object) -> ToolCallRequest:
        try:
            function = raw["function"]  # type: ignore[index]
            name = function["name"]
            arguments = function.get("arguments", {})
        except (KeyError, TypeError, AttributeError):
            raise LLMGenerationError("ollama_invalid_response_shape")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments) if arguments.strip() else {}
            except json.JSONDecodeError:
                raise LLMGenerationError("ollama_invalid_response_shape")
        if not isinstance(name, str) or not name.strip() or not isinstance(arguments, dict):
            raise LLMGenerationError("ollama_invalid_response_shape")
        return ToolCallRequest(tool_name=name, parameters=arguments)

    @staticmethod
    def _serialize_message(message: ModelMessage) -> dict:
        serialized: dict = {"role": message.role.value, "content": message.content}
        if message.tool_calls:
            serialized["tool_calls"] = [
                {"function": {"name": call.tool_name, "arguments": dict(call.parameters)}}
                for call in message.tool_calls
            ]
        if message.tool_name is not None:
            serialized["tool_name"] = message.tool_name
        return serialized

    @staticmethod
    def _serialize_tool(tool: ToolDefinition) -> dict:
        return {
            "type": "function",
            "function": {
                "name": tool.name, "description": tool.description,
                "parameters": json.loads(json.dumps(dict(tool.parameters_schema))),
            },
        }

    def _post_chat(self, *, request: ModelGenerationRequest, tools: tuple[ToolDefinition, ...]) -> dict:
        body: dict = {
            "model": self._model_name,
            "messages": [self._serialize_message(m) for m in request.messages],
            "stream": False,
        }
        if tools:
            body["tools"] = [self._serialize_tool(t) for t in tools]
        try:
            response = requests.post(
                f"{self._host}/api/chat",
                json=body,
                timeout=(self._connect_timeout, self._read_timeout),
                stream=True,
                allow_redirects=False,
            )
        except requests.Timeout:
            raise LLMGenerationError("ollama_timeout")
        except requests.RequestException:
            raise LLMGenerationError("ollama_connection_error")
        # No response object exists yet if requests.post() itself raised above --
        # nothing to close in that case.

        try:
            if response.status_code != 200:
                if tools and response.status_code == 400 and self._reports_tools_unsupported(response):
                    raise LLMGenerationError("ollama_tools_unsupported")
                raise LLMGenerationError("ollama_http_error")

            content_type = response.headers.get("Content-Type", "")
            if not _is_accepted_content_type(content_type):
                raise LLMGenerationError("ollama_invalid_content_type")

            body_bytes = _read_bounded_response_body(response, max_bytes=self._max_body_bytes)

            try:
                decoded = body_bytes.decode("utf-8")
            except UnicodeDecodeError:
                raise LLMGenerationError("ollama_invalid_utf8")

            try:
                return json.loads(decoded)
            except json.JSONDecodeError:
                raise LLMGenerationError("ollama_invalid_json")
        finally:
            response.close()

    @staticmethod
    def _reports_tools_unsupported(response: requests.Response) -> bool:
        """Best-effort, bounded (4 KiB) look at an HTTP 400 body for
        Ollama's "does not support tools" message. Any problem reading
        it means "not recognised" -- the caller then reports an
        ordinary http error."""
        try:
            raw = _read_bounded_response_body(response, max_bytes=4096)
        except Exception:  # noqa: BLE001 -- diagnostic only; any failure means "not recognised"
            return False
        return b"support tools" in raw.lower()

    @staticmethod
    def _extract_text(payload: object, *, max_characters: int) -> str:
        try:
            text = payload["message"]["content"]  # type: ignore[index]
        except (KeyError, TypeError):
            raise LLMGenerationError("ollama_invalid_response_shape")

        if not isinstance(text, str):
            raise LLMGenerationError("ollama_invalid_response_shape")

        text = text.strip()
        if not text:
            raise LLMGenerationError("ollama_empty_response")

        if len(text) > max_characters:
            raise LLMGenerationError("ollama_response_too_long")

        return text
