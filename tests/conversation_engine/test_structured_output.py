"""v0.39 Phase F1: structured output — strict parsing, the Ollama `format` request, and the reply-only router in the engine. No network."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import requests

from conversation_engine.engine import ConversationEngine
from conversation_engine.model_types import LLMGenerationError, ModelGenerationRequest, ModelMessage, ModelMessageRole
from conversation_engine.ollama_adapter import STRUCTURED_OVERHEAD_CHARACTERS, OllamaConversationModel
from conversation_engine.structured_output import (
    FORMAT_INSTRUCTIONS, MAX_MEMORY_NOTE, MAX_SUGGESTION_NOTE, MEMORY_CATEGORIES, RESPONSE_SCHEMA, SUGGESTION_TYPES, MemoryNote, Suggestion,
    parse_structured_reply,
)
from conversation_engine.subject_queue import SubjectConversationQueue
from memory_system.working_memory import InMemoryWorkingMemory

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).parent.parent.parent


def dumps(reply="Good work today.", memory=None, suggestion=None, **extra):
    return json.dumps({"reply_to_user": reply, "save_to_memory": memory, "submit_suggestion": suggestion, **extra})


def parse(raw, limit=1800):
    return parse_structured_reply(raw, max_reply_characters=limit)


class TestParser:
    def test_a_plain_reply(self) -> None:
        r = parse(dumps())
        assert r.reply == "Good work today." and r.memory_note is None and r.suggestion is None and not r.has_side_data and r.issues == ()

    def test_valid_side_data(self) -> None:
        r = parse(dumps(memory={"category": "stated_goal", "note": " Read four books this month. "},
                        suggestion={"type": "rule_tweak", "note": "Move the evening check-in earlier."}))
        assert r.memory_note == MemoryNote("stated_goal", "Read four books this month.")
        assert r.suggestion == Suggestion("rule_tweak", "Move the evening check-in earlier.") and r.has_side_data

    @pytest.mark.parametrize("raw", [
        "not json", "", "[]", "42", '"text"', "{}", json.dumps({"reply_to_user": ""}), json.dumps({"reply_to_user": "   "}),
        json.dumps({"reply_to_user": 5}), json.dumps({"save_to_memory": None}), dumps(extra_field="x"),
        json.dumps({"reply_to_user": "hi", "set_trust": 1.0}), json.dumps({"reply_to_user": "hi", "unlock": True}),
        "Sure! " + dumps(), dumps() + " trailing",
    ])
    def test_no_usable_reply_means_nothing_is_used(self, raw) -> None:
        assert parse(raw) is None

    def test_a_too_long_reply_is_unusable(self) -> None:
        assert parse(dumps(reply="x" * 101), limit=100) is None and parse(dumps(reply="x" * 100), limit=100) is not None

    def test_a_code_fence_is_tolerated(self) -> None:
        assert parse("```json\n" + dumps() + "\n```").reply == "Good work today."
        assert parse("```\n" + dumps() + "\n```").reply == "Good work today."

    @pytest.mark.parametrize("memory,issue", [
        ({"category": "weakness", "note": "x"}, "memory_kind"), ({"category": "health", "note": "x"}, "memory_kind"),
        ({"category": "quote"}, "memory_shape"), ({"category": "quote", "note": "x", "extra": 1}, "memory_shape"),
        ({"category": "quote", "note": "  "}, "memory_empty"), ({"category": "quote", "note": 7}, "memory_empty"),
        ({"category": "quote", "note": "x" * (MAX_MEMORY_NOTE + 1)}, "memory_too_long"), ("a string", "memory_shape"),
    ])
    def test_bad_memory_data_is_dropped_and_the_reply_kept(self, memory, issue) -> None:
        r = parse(dumps(memory=memory))
        assert r.reply == "Good work today." and r.memory_note is None and r.issues == (issue,)

    @pytest.mark.parametrize("suggestion,issue", [
        ({"type": "unlock_now", "note": "x"}, "suggestion_kind"), ({"note": "x"}, "suggestion_shape"),
        ({"type": "rule_tweak", "note": "x" * (MAX_SUGGESTION_NOTE + 1)}, "suggestion_too_long"),
    ])
    def test_bad_suggestions_are_dropped(self, suggestion, issue) -> None:
        r = parse(dumps(suggestion=suggestion))
        assert r.suggestion is None and r.issues == (issue,)

    def test_the_schema_matches_the_parser(self) -> None:
        props = RESPONSE_SCHEMA["properties"]
        assert set(props) == {"reply_to_user", "save_to_memory", "submit_suggestion"} and RESPONSE_SCHEMA["additionalProperties"] is False
        assert props["save_to_memory"]["anyOf"][1]["properties"]["category"]["enum"] == list(MEMORY_CATEGORIES)
        assert props["submit_suggestion"]["anyOf"][1]["properties"]["type"]["enum"] == list(SUGGESTION_TYPES)
        assert MEMORY_CATEGORIES == ("behavior_pattern", "stated_goal", "quote")

    def test_the_instructions_carry_the_exclusions_and_secrecy(self) -> None:
        for word in ("health", "trauma", "self-harm", "distress", "never shown to the user", "must not mention"):
            assert word in FORMAT_INSTRUCTIONS
        assert not re.search(r"\b(unlock|release (time|date))\b", FORMAT_INSTRUCTIONS)


def _response(body: dict) -> Mock:
    raw = json.dumps(body).encode()
    response = Mock(spec=requests.Response)
    response.status_code = 200
    response.headers = {"Content-Type": "application/json"}
    response.iter_content = lambda chunk_size: iter([raw])
    response.close = Mock()
    return response


def _req(limit=500):
    return ModelGenerationRequest(messages=(ModelMessage(role=ModelMessageRole.USER, content="hi"),), max_output_characters=limit)


class TestOllamaAdapter:
    def test_the_schema_is_sent_as_format_and_the_raw_text_returned(self) -> None:
        with patch("requests.post", return_value=_response({"message": {"content": dumps()}})) as post:
            raw = OllamaConversationModel(host="http://localhost:11434", model_name="qwen2.5:32b").generate_structured(
                request=_req(), schema=RESPONSE_SCHEMA)
        body = post.call_args.kwargs["json"]
        assert body["format"] == RESPONSE_SCHEMA and body["stream"] is False and "keep_alive" not in body
        assert json.loads(raw)["reply_to_user"] == "Good work today."

    def test_keep_alive_is_sent_when_configured_and_plain_calls_have_no_format(self) -> None:
        with patch("requests.post", return_value=_response({"message": {"content": "plain"}})) as post:
            OllamaConversationModel(host="http://h", model_name="m", keep_alive="30m").generate(request=_req())
        body = post.call_args.kwargs["json"]
        assert body["keep_alive"] == "30m" and "format" not in body

    def test_the_size_bound_allows_the_side_fields(self) -> None:
        long = dumps(reply="x" * 500, memory={"category": "quote", "note": "y" * 300})
        assert len(long) > 500
        with patch("requests.post", return_value=_response({"message": {"content": long}})):
            assert OllamaConversationModel(host="http://h", model_name="m").generate_structured(request=_req(500), schema=RESPONSE_SCHEMA)
        with patch("requests.post", return_value=_response({"message": {"content": "z" * (500 + STRUCTURED_OVERHEAD_CHARACTERS + 1)}})):
            with pytest.raises(LLMGenerationError):
                OllamaConversationModel(host="http://h", model_name="m").generate_structured(request=_req(500), schema=RESPONSE_SCHEMA)

    def test_the_read_timeout_is_configurable(self) -> None:
        with patch("requests.post", return_value=_response({"message": {"content": "ok"}})) as post:
            OllamaConversationModel(host="http://h", model_name="m", read_timeout_seconds=120.0).generate(request=_req())
        assert post.call_args.kwargs["timeout"][1] == 120.0


class Model:
    """A fake model: `structured` is what generate_structured returns; `plain` what generate returns."""

    def __init__(self, structured=None, plain="plain reply", structured_error=None) -> None:
        self.structured, self.plain, self.structured_error = structured, plain, structured_error
        self.structured_calls: list[ModelGenerationRequest] = []
        self.plain_calls: list[ModelGenerationRequest] = []

    def generate(self, *, request):
        self.plain_calls.append(request)
        return self.plain

    def generate_structured(self, *, request, schema):
        assert schema is RESPONSE_SCHEMA
        self.structured_calls.append(request)
        if self.structured_error:
            raise self.structured_error
        return self.structured


class PlainOnlyModel:
    def __init__(self) -> None:
        self.calls = 0

    def generate(self, *, request):
        self.calls += 1
        return "plain"


def engine(model, *, structured=True, sink=None, memory=None):
    wm = memory or InMemoryWorkingMemory(max_exchanges_per_subject=5, max_characters_per_subject=5000)
    return ConversationEngine(model=model, working_memory_reader=wm, working_memory_writer=wm, queue=SubjectConversationQueue(),
                              structured_output=structured, structured_sink=sink), wm


def ask(eng, text="hi", **kw):
    return eng.generate_response(subject_key="s1", current_user_message=text, language="en", identity_id="alex", now=NOW, **kw)


class TestEngineRouter:
    def test_only_the_reply_reaches_the_user(self) -> None:
        model = Model(structured=dumps(reply="Report accepted.", memory={"category": "quote", "note": "I will do better."}))
        eng, _ = engine(model)
        result = ask(eng)
        assert result.text == "Report accepted." and "{" not in result.text and model.plain_calls == []
        last_two = model.structured_calls[0].messages[-2:]
        assert last_two[0].content == FORMAT_INSTRUCTIONS and last_two[1].role is ModelMessageRole.USER

    def test_in_f1_side_data_has_no_effect_without_a_sink(self) -> None:
        eng, wm = engine(Model(structured=dumps(memory={"category": "quote", "note": "secret note"})))
        assert ask(eng).text == "Good work today."
        stored = wm.read(subject_key="s1").turns
        assert all("secret note" not in t.content for t in stored)                       # never in working memory either

    def test_a_sink_receives_validated_side_data_when_installed(self) -> None:
        received = []
        eng, _ = engine(Model(structured=dumps(memory={"category": "stated_goal", "note": "Run 5k"})),
                        sink=lambda subject, parsed, now: received.append((subject, parsed.memory_note, now)))
        ask(eng)
        assert received == [("s1", MemoryNote("stated_goal", "Run 5k"), NOW)]

    def test_untrusted_turns_never_hand_side_data_to_the_sink(self) -> None:
        received = []
        eng, _ = engine(Model(structured=dumps(memory={"category": "quote", "note": "x"})), sink=lambda *a: received.append(a))
        assert ask(eng, allow_side_data=False).text == "Good work today." and received == []

    def test_a_failing_sink_never_affects_the_reply(self) -> None:
        def boom(*a):
            raise RuntimeError("db down")
        eng, _ = engine(Model(structured=dumps(memory={"category": "quote", "note": "x"})), sink=boom)
        assert ask(eng).text == "Good work today."

    @pytest.mark.parametrize("bad", ["not json", dumps(reply=""), dumps(extra="x"), "Sure! " + dumps()])
    def test_unusable_structured_output_falls_back_to_one_plain_reply(self, bad) -> None:
        model = Model(structured=bad, plain="A plain answer.")
        eng, _ = engine(model)
        assert ask(eng).text == "A plain answer." and len(model.plain_calls) == 1
        assert all(m.content != FORMAT_INSTRUCTIONS for m in model.plain_calls[0].messages)   # the plain retry has no format instructions

    def test_a_model_error_gives_the_usual_fallback(self) -> None:
        eng, _ = engine(Model(structured_error=LLMGenerationError("ollama_timeout")))
        result = ask(eng)
        assert result.text and "{" not in result.text

    def test_the_flag_is_off_by_default_and_plain_models_are_unaffected(self) -> None:
        model = Model(structured=dumps())
        eng, _ = engine(model, structured=False)
        assert ask(eng).text == "plain reply" and model.structured_calls == []
        plain = PlainOnlyModel()
        eng, _ = engine(plain, structured=True)                                      # no generate_structured: plain path
        assert ask(eng).text == "plain" and plain.calls == 1

    def test_extra_system_context_keeps_the_plain_path(self) -> None:
        model = Model(structured=dumps())
        eng, _ = engine(model)
        ask(eng, extra_system_context="Lock: locked.")
        assert model.structured_calls == [] and len(model.plain_calls) == 1

    def test_the_reply_is_still_validated_and_committed(self) -> None:
        eng, wm = engine(Model(structured=dumps(reply="Noted.")))
        ask(eng, "I trained today")
        turns = wm.read(subject_key="s1").turns
        assert [t.content for t in turns] == ["I trained today", "Noted."]


class TestInvariants:
    def test_system_boundaries_are_untouched(self) -> None:
        text = (ROOT / "conversation_engine" / "prompt_builder.py").read_text(encoding="utf-8")
        m = re.search(r'_SYSTEM_BOUNDARIES\s*=\s*(?:\(|""")(.*?)(?:\n\)|""")\s*\n', text, re.S)
        assert hashlib.md5(m.group(0).encode()).hexdigest() == "846faf9aa5b8c4288c2c49656d5aa34a"

    def test_the_conversation_only_entry_disables_side_data(self) -> None:
        source = (ROOT / "application" / "service.py").read_text(encoding="utf-8")
        start = source.index("def handle_conversation_only")
        assert "allow_side_data=False" in source[start:start + 2000]

    def test_config_defaults(self) -> None:
        from core.config import Config
        f = Config.__dataclass_fields__
        assert f["conversation_structured_output"].default is False and f["ollama_keep_alive"].default is None
        assert f["ollama_read_timeout_seconds"].default == 30.0
