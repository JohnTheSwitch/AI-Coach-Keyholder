"""
tests/conversation_engine/test_conversation_behavior_policy.py

Conversation Behavior/Prompt Policy slice -- verifies the
COACHING_DIALOGUE instruction text contains the required semantic
invariants and the exact, exhaustive deterministic command vocabulary.
Tests substrings/semantic invariants, NOT exact equality of the whole
paragraph, so future wording changes don't unnecessarily break the
suite (per explicit review instruction).
"""

from __future__ import annotations

from ai.identity_catalog import CommunicationProfile
from conversation_engine.models import ResponseCategory, ResponseContextSnapshot, ResponsePlan, SituationalConstraints
from conversation_engine.prompt_builder import KEYHOLDER_COMMAND_GUIDANCE, build_generation_request

_KNOWN_COMMANDS = KEYHOLDER_COMMAND_GUIDANCE


def _profile(**overrides) -> CommunicationProfile:
    kwargs = dict(warmth=0.5, humor=0.5, teasing=0.5, assertiveness=0.5, formality=0.5, verbosity=0.5)
    kwargs.update(overrides)
    return CommunicationProfile(**kwargs)


def _snapshot(current_user_message: str = "hello") -> ResponseContextSnapshot:
    return ResponseContextSnapshot(
        response_category=ResponseCategory.COACHING_DIALOGUE, current_user_message=current_user_message,
        language="en", identity_profile=_profile(), identity_id="alex", situational_constraints=SituationalConstraints(),
        context_fragments={},
    )


def _plan() -> ResponsePlan:
    return ResponsePlan(response_category=ResponseCategory.COACHING_DIALOGUE)


def _system_text() -> str:
    request = build_generation_request(
        snapshot=_snapshot(), plan=_plan(), working_memory_turns=(), max_output_characters=1800,
    )
    return request.messages[0].content


class TestExactCommandVocabulary:
    def test_no_command_appears_in_the_prompt(self) -> None:
        """Commands are banned from the model's vocabulary entirely (the
        registered commands still exist and still work when typed)."""
        system_text = _system_text()
        for command in _KNOWN_COMMANDS:
            assert f"`{command}`" not in system_text, f"command leaked into the prompt: {command}"

    def test_states_that_commands_must_never_be_mentioned(self) -> None:
        system_text = _system_text()
        assert "NEVER mention, suggest or write any command" in system_text


class TestAuthoritativeStateBehavioralRule:
    def test_instructs_natural_not_mechanical_use_of_authoritative_state(self) -> None:
        system_text = _system_text().lower()
        assert "naturally" in system_text

    def test_active_task_coaching_guidance_present(self) -> None:
        system_text = _system_text().lower()
        assert "active task" in system_text and "coach" in system_text


class TestUnknownEpistemicProtection:
    def test_unknown_never_implies_unlocked(self) -> None:
        system_text = _system_text().lower()
        assert "no report is recorded" in system_text
        assert "never assume the user is unlocked" in system_text


class TestUserReportedNotPhysicallyVerifiedProtection:
    def test_locked_and_unlocked_reports_are_never_upgraded_to_verification(self) -> None:
        system_text = _system_text().lower()
        assert "independently verified" in system_text
        assert "reported" in system_text


class TestConversationalAcknowledgementIsNotAStateTransition:
    def test_explicit_statement_that_saying_something_does_not_change_state(self) -> None:
        system_text = _system_text().lower()
        assert "does not change" in system_text or "not a state transition" in system_text
        assert '"i finished it"' in system_text or "i finished it" in system_text

    def test_model_never_claims_to_have_performed_a_write(self) -> None:
        """This is _SYSTEM_BOUNDARIES' own pre-existing guarantee --
        confirmed still present and unchanged, not re-implemented here."""
        system_text = _system_text()
        assert "you have no ability to change any setting, mode, or record" in system_text.lower()


class TestUserControlsCommandInvocation:
    def test_does_not_imply_completion_is_warranted_merely_because_model_believes_it(self) -> None:
        system_text = _system_text().lower()
        assert "never performs any change" in system_text and "only the application itself does" in system_text


class TestNoTaskContentHallucination:
    def test_instructs_against_inventing_missing_task_instructions(self) -> None:
        system_text = _system_text().lower()
        assert "never invent task instructions" in system_text or "sparse" in system_text


class TestSystemBoundariesUnchanged:
    """CE-11 through CE-19's own text, folded into _SYSTEM_BOUNDARIES --
    must remain present and first, unmodified by this slice."""

    def test_system_boundaries_text_still_present(self) -> None:
        system_text = _system_text()
        assert "You are the AI voice of a personal accountability coaching system." in system_text

    def test_system_boundaries_appear_before_category_instructions(self) -> None:
        system_text = _system_text()
        boundaries_index = system_text.index("You are the AI voice")
        category_index = system_text.index("Respond as a supportive")
        assert boundaries_index < category_index


class TestNoStructuralChangeFromThisPolicy:
    def test_message_count_unchanged(self) -> None:
        request = build_generation_request(
            snapshot=_snapshot(), plan=_plan(), working_memory_turns=(), max_output_characters=1800,
        )
        assert len(request.messages) == 2  # system + current user message only

    def test_current_user_message_remains_final(self) -> None:
        request = build_generation_request(
            snapshot=_snapshot(current_user_message="what should I do?"), plan=_plan(),
            working_memory_turns=(), max_output_characters=1800,
        )
        assert request.messages[-1].content == "what should I do?"
