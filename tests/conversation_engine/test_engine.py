"""tests/conversation_engine/test_engine.py"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest

from conversation_engine.guardrails import build_guardrails
from conversation_engine.engine import ConversationEngine
from conversation_engine.model_types import LLMGenerationError, ModelGenerationRequest
from conversation_engine.models import ResponseCategory, UnknownIdentityError
from conversation_engine.subject_queue import SubjectConversationQueue
from memory_system.models import WorkingMemoryCapacityError, WorkingMemoryError, WorkingMemorySnapshot
from memory_system.working_memory import InMemoryWorkingMemory

FIXED_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


class _FakeModel:
    def __init__(self, *, response: str | None = None, error: LLMGenerationError | None = None) -> None:
        self._response = response
        self._error = error
        self.calls: list[ModelGenerationRequest] = []

    def generate(self, *, request: ModelGenerationRequest) -> str:
        self.calls.append(request)
        if self._error is not None:
            raise self._error
        return self._response


class _FailingReader:
    """Raises a chosen exception on every read() call -- used to
    simulate expected/unexpected memory read failures independently
    of the writer."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def read(self, *, subject_key: str) -> WorkingMemorySnapshot:
        raise self._exc


class _FailingWriter:
    """Raises a chosen exception on every commit_exchange() call --
    used to simulate expected/unexpected memory write failures
    independently of the reader."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.calls = 0

    def commit_exchange(self, *, subject_key: str, user_content: str, assistant_content: str) -> None:
        self.calls += 1
        raise self._exc


def _working_memory(*, max_exchanges: int = 5, max_characters: int = 5000) -> InMemoryWorkingMemory:
    return InMemoryWorkingMemory(max_exchanges_per_subject=max_exchanges, max_characters_per_subject=max_characters)


def _engine(model, *, reader=None, writer=None) -> ConversationEngine:
    wm = _working_memory()
    return ConversationEngine(
        model=model, working_memory_reader=reader or wm, working_memory_writer=writer or wm,
        queue=SubjectConversationQueue(),
    )


class TestSuccessfulGeneration:
    def test_returns_the_models_own_text(self) -> None:
        engine = _engine(_FakeModel(response="hello there"))
        result = engine.generate_response(
            subject_key="s1", current_user_message="hi", language="en", identity_id="alex", now=FIXED_TIME,
        )
        assert result.text == "hello there"
        assert result.response_category == ResponseCategory.COACHING_DIALOGUE

    def test_successful_exchange_is_committed_atomically(self) -> None:
        wm = _working_memory()
        engine = ConversationEngine(
            model=_FakeModel(response="reply"), working_memory_reader=wm, working_memory_writer=wm,
            queue=SubjectConversationQueue(),
        )
        engine.generate_response(subject_key="s1", current_user_message="hi", language="en", identity_id="alex", now=FIXED_TIME)
        turns = wm.read(subject_key="s1").turns
        assert len(turns) == 2
        assert turns[0].content == "hi"
        assert turns[1].content == "reply"

    def test_working_memory_read_happens_before_generation(self) -> None:
        wm = _working_memory()
        wm.commit_exchange(subject_key="s1", user_content="earlier", assistant_content="earlier reply")
        model = _FakeModel(response="new reply")
        engine = ConversationEngine(model=model, working_memory_reader=wm, working_memory_writer=wm, queue=SubjectConversationQueue())
        engine.generate_response(subject_key="s1", current_user_message="hi", language="en", identity_id="alex", now=FIXED_TIME)
        request = model.calls[0]
        contents = [m.content for m in request.messages]
        assert "earlier" in contents
        assert "earlier reply" in contents
        assert contents[-1] == "hi"  # current user message always last


class TestModelFailureFallsBack:
    def test_llm_generation_error_returns_deterministic_fallback(self) -> None:
        engine = _engine(_FakeModel(error=LLMGenerationError("ollama_timeout")))
        result = engine.generate_response(
            subject_key="s1", current_user_message="hi", language="en", identity_id="alex", now=FIXED_TIME,
        )
        assert result.response_category == ResponseCategory.ERROR_FALLBACK

    def test_fallback_does_not_commit_to_working_memory(self) -> None:
        wm = _working_memory()
        engine = ConversationEngine(
            model=_FakeModel(error=LLMGenerationError("ollama_timeout")), working_memory_reader=wm,
            working_memory_writer=wm, queue=SubjectConversationQueue(),
        )
        engine.generate_response(subject_key="s1", current_user_message="hi", language="en", identity_id="alex", now=FIXED_TIME)
        assert wm.read(subject_key="s1").turns == ()


class TestValidationFailureFallsBack:
    def test_empty_model_output_falls_back(self) -> None:
        engine = _engine(_FakeModel(response=""))
        result = engine.generate_response(
            subject_key="s1", current_user_message="hi", language="en", identity_id="alex", now=FIXED_TIME,
        )
        assert result.response_category == ResponseCategory.ERROR_FALLBACK

    def test_validation_failure_does_not_commit(self) -> None:
        wm = _working_memory()
        engine = ConversationEngine(model=_FakeModel(response=""), working_memory_reader=wm, working_memory_writer=wm, queue=SubjectConversationQueue())
        engine.generate_response(subject_key="s1", current_user_message="hi", language="en", identity_id="alex", now=FIXED_TIME)
        assert wm.read(subject_key="s1").turns == ()


class TestIdentityHandling:
    def test_unknown_identity_id_raises_not_caught_here(self) -> None:
        engine = _engine(_FakeModel(response="should not be reached"))
        with pytest.raises(UnknownIdentityError):
            engine.generate_response(
                subject_key="s1", current_user_message="hi", language="en",
                identity_id="not-a-real-identity", now=FIXED_TIME,
            )


class TestGenerationPathIsAlwaysModelGeneration:
    def test_the_model_is_actually_invoked_for_the_unmatched_path(self) -> None:
        model = _FakeModel(response="ok")
        engine = _engine(model)
        engine.generate_response(subject_key="s1", current_user_message="hi", language="en", identity_id="alex", now=FIXED_TIME)
        assert len(model.calls) == 1


class TestNoDomainWriteAPICalled:
    def test_a_model_response_claiming_an_action_never_calls_any_write_api(self) -> None:
        engine = _engine(_FakeModel(response="I switched you to Advanced Mode."))
        result = engine.generate_response(
            subject_key="s1", current_user_message="switch me to advanced", language="en",
            identity_id="alex", now=FIXED_TIME,
        )
        assert result.text == "I switched you to Advanced Mode."
        assert not hasattr(engine, "advanced_mode_admin")
        assert not hasattr(engine, "penalty_engine")


class TestReadFailurePolicy:
    def test_expected_read_failure_continues_with_empty_history_and_model_is_still_called(self) -> None:
        model = _FakeModel(response="ok despite empty history")
        engine = _engine(model, reader=_FailingReader(WorkingMemoryError("boom")))
        result = engine.generate_response(
            subject_key="s1", current_user_message="hi", language="en", identity_id="alex", now=FIXED_TIME,
        )
        assert result.text == "ok despite empty history"
        assert len(model.calls) == 1
        contents = [m.content for m in model.calls[0].messages]
        assert contents == [contents[0], build_guardrails(), "hi"]  # only system message + current user message -- empty history

    def test_expected_read_failure_logs_working_memory_read_failed(self, caplog: pytest.LogCaptureFixture) -> None:
        engine = _engine(_FakeModel(response="ok"), reader=_FailingReader(WorkingMemoryError("boom")))
        with caplog.at_level(logging.WARNING, logger="ai_coach_keyholder.conversation_engine"):
            engine.generate_response(subject_key="s1", current_user_message="hi", language="en", identity_id="alex", now=FIXED_TIME)
        codes = [r.error_code for r in caplog.records if hasattr(r, "error_code")]
        assert "working_memory_read_failed" in codes

    def test_unexpected_read_failure_logs_a_different_code(self, caplog: pytest.LogCaptureFixture) -> None:
        engine = _engine(_FakeModel(response="ok"), reader=_FailingReader(RuntimeError("totally unexpected")))
        with caplog.at_level(logging.WARNING, logger="ai_coach_keyholder.conversation_engine"):
            result = engine.generate_response(subject_key="s1", current_user_message="hi", language="en", identity_id="alex", now=FIXED_TIME)
        codes = [r.error_code for r in caplog.records if hasattr(r, "error_code")]
        assert "working_memory_unexpected_read_error" in codes
        assert "working_memory_read_failed" not in codes  # never mislabeled as the ordinary case
        assert result.text == "ok"  # still degrades gracefully, model still called

    def test_read_failure_does_not_block_a_later_successful_commit(self) -> None:
        wm = _working_memory()
        engine = ConversationEngine(
            model=_FakeModel(response="reply"), working_memory_reader=_FailingReader(WorkingMemoryError("boom")),
            working_memory_writer=wm, queue=SubjectConversationQueue(),
        )
        engine.generate_response(subject_key="s1", current_user_message="hi", language="en", identity_id="alex", now=FIXED_TIME)
        assert len(wm.read(subject_key="s1").turns) == 2  # commit still happened despite the read failure

    def test_log_does_not_contain_raw_content_or_subject_key(self, caplog: pytest.LogCaptureFixture) -> None:
        engine = _engine(_FakeModel(response="ok"), reader=_FailingReader(WorkingMemoryError("boom")))
        with caplog.at_level(logging.WARNING, logger="ai_coach_keyholder.conversation_engine"):
            engine.generate_response(
                subject_key="very-secret-subject-key-12345", current_user_message="sensitive user text",
                language="en", identity_id="alex", now=FIXED_TIME,
            )
        for record in caplog.records:
            assert "very-secret-subject-key-12345" not in record.getMessage()
            assert "sensitive user text" not in record.getMessage()


class TestWriteFailurePolicy:
    def test_expected_commit_failure_still_returns_the_validated_response(self) -> None:
        writer = _FailingWriter(WorkingMemoryError("boom"))
        engine = _engine(_FakeModel(response="a good, validated answer"), writer=writer)
        result = engine.generate_response(subject_key="s1", current_user_message="hi", language="en", identity_id="alex", now=FIXED_TIME)
        assert result.text == "a good, validated answer"
        assert writer.calls == 1

    def test_capacity_failure_still_returns_the_validated_response(self) -> None:
        writer = _FailingWriter(WorkingMemoryCapacityError("too big"))
        engine = _engine(_FakeModel(response="a good, validated answer"), writer=writer)
        result = engine.generate_response(subject_key="s1", current_user_message="hi", language="en", identity_id="alex", now=FIXED_TIME)
        assert result.text == "a good, validated answer"

    def test_unexpected_commit_failure_still_returns_the_validated_response(self) -> None:
        writer = _FailingWriter(RuntimeError("totally unexpected"))
        engine = _engine(_FakeModel(response="a good, validated answer"), writer=writer)
        result = engine.generate_response(subject_key="s1", current_user_message="hi", language="en", identity_id="alex", now=FIXED_TIME)
        assert result.text == "a good, validated answer"

    def test_expected_and_capacity_and_unexpected_commit_failures_log_different_codes(self, caplog: pytest.LogCaptureFixture) -> None:
        cases = [
            (WorkingMemoryError("boom"), "working_memory_commit_failed"),
            (WorkingMemoryCapacityError("too big"), "working_memory_capacity_exceeded"),
            (RuntimeError("unexpected"), "working_memory_unexpected_commit_error"),
        ]
        for exc, expected_code in cases:
            caplog.clear()
            engine = _engine(_FakeModel(response="ok"), writer=_FailingWriter(exc))
            with caplog.at_level(logging.WARNING, logger="ai_coach_keyholder.conversation_engine"):
                engine.generate_response(subject_key="s1", current_user_message="hi", language="en", identity_id="alex", now=FIXED_TIME)
            codes = [r.error_code for r in caplog.records if hasattr(r, "error_code")]
            assert expected_code in codes

    def test_no_retry_commit_is_attempted_exactly_once(self) -> None:
        writer = _FailingWriter(WorkingMemoryError("boom"))
        engine = _engine(_FakeModel(response="ok"), writer=writer)
        engine.generate_response(subject_key="s1", current_user_message="hi", language="en", identity_id="alex", now=FIXED_TIME)
        assert writer.calls == 1
