"""Phase J3: the closed action catalogue and its strict validator. Pure."""

from __future__ import annotations

import json

import pytest

from coach.actions import (
    ACTION_INSTRUCTIONS, ACTION_SCHEMAS, MAX_COMMITMENT, MAX_GOAL_REF, MAX_REASON, MAX_TARGET, MAX_TITLE, ActionKind, Refusal, ValidatedAction, validate_action,
)
from coach.safety import content_refusal

GOAL = {"action": "propose_goal", "title": "Run", "target_description": "Three runs a week"}
ADAPT = {"action": "adapt_goal", "goal": 1, "target_description": "Two runs a week", "reason": "Three was too many."}
COMMIT = {"action": "propose_commitment", "text": "Study for 30 minutes", "when": "tomorrow"}


def reason(raw) -> str:
    result = validate_action(raw)
    assert isinstance(result, Refusal), result
    return result.reason


class TestTheCatalogueIsClosed:
    def test_exactly_three_proposals_exist(self) -> None:
        assert {k.value for k in ActionKind} == {"propose_goal", "adapt_goal", "propose_commitment"}
        assert [s["properties"]["action"]["enum"] for s in ACTION_SCHEMAS] == [["propose_goal"], ["adapt_goal"], ["propose_commitment"]]

    def test_every_schema_is_closed(self) -> None:
        assert all(s["additionalProperties"] is False for s in ACTION_SCHEMAS)

    @pytest.mark.parametrize("name", ["unlock", "release", "freeze", "unfreeze", "add_time", "remove_time", "set_trust", "award_token", "create_penalty",
                                      "revoke_consent", "send_message", "assign_task", "PROPOSE_GOAL", "propose goal", "", None, 1, ["propose_goal"]])
    def test_anything_else_is_refused_by_name(self, name) -> None:
        assert reason({"action": name, "text": "x", "when": "today"}) == "unknown_action"

    def test_nothing_in_a_schema_can_name_the_regime(self) -> None:
        text = json.dumps(ACTION_SCHEMAS).lower()
        assert not any(word in text for word in ("lock", "unlock", "freeze", "release", "token", "trust", "penalty", "seconds", "hours", "duration"))

    def test_the_instructions_say_it_does_nothing_by_itself(self) -> None:
        assert "only the user's own" in ACTION_INSTRUCTIONS and "never in reply_to_user" in ACTION_INSTRUCTIONS


class TestValidRequests:
    def test_a_goal(self) -> None:
        assert validate_action(GOAL) == ValidatedAction(kind=ActionKind.PROPOSE_GOAL, title="Run", target="Three runs a week")

    def test_an_adaptation(self) -> None:
        result = validate_action(ADAPT)
        assert (result.kind, result.goal_ref, result.target, result.reason) == (ActionKind.ADAPT_GOAL, 1, "Two runs a week", "Three was too many.")

    def test_a_commitment_with_its_optional_links(self) -> None:
        result = validate_action({**COMMIT, "goal": 2, "active_task": True})
        assert (result.when, result.goal_ref, result.active_task) == ("tomorrow", 2, True)

    def test_null_optional_fields_are_fine(self) -> None:
        result = validate_action({**COMMIT, "goal": None, "active_task": None})
        assert result.goal_ref is None and result.active_task is False

    def test_whitespace_collapses_and_compatibility_forms_are_normalised(self) -> None:
        assert validate_action({**COMMIT, "text": "  Read\n\n ｂｏｏｋ  "}).text == "Read book"

    def test_texts_lists_every_free_text(self) -> None:
        assert set(validate_action(ADAPT).texts()) == {"Two runs a week", "Three was too many."}


class TestItRejectsAndNeverRepairs:
    @pytest.mark.parametrize("raw", [None, "propose_goal", 3, [], ["action"]])
    def test_not_an_object(self, raw) -> None:
        assert reason(raw) == "invalid_parameters"

    @pytest.mark.parametrize("raw", [{**GOAL, "extra": 1}, {**COMMIT, "hours": 2}, {**ADAPT, "trust": 1.0}, {**COMMIT, "goal_group_id": "x"}])
    def test_an_unknown_argument(self, raw) -> None:
        assert reason(raw) == "unknown_parameter"

    @pytest.mark.parametrize("raw", [{"action": "propose_goal", "title": "x"}, {"action": "adapt_goal", "goal": 1, "target_description": "x"},
                                     {"action": "propose_commitment", "text": "x"}, {**GOAL, "title": None}])
    def test_a_missing_argument(self, raw) -> None:
        assert reason(raw) == "missing_parameter"

    @pytest.mark.parametrize("value", ["1", 1.0, True, 0, -1, MAX_GOAL_REF + 1, [1]])
    def test_a_goal_number_is_a_real_small_integer(self, value) -> None:
        assert reason({**ADAPT, "goal": value}) == "invalid_goal"

    @pytest.mark.parametrize("value", ["yesterday", "next week", "TODAY", 1, True])
    def test_when_is_today_or_tomorrow(self, value) -> None:
        assert reason({**COMMIT, "when": value}) == "invalid_when"

    @pytest.mark.parametrize("value", ["true", 1, 0])
    def test_active_task_is_a_boolean(self, value) -> None:
        assert reason({**COMMIT, "active_task": value}) == "invalid_parameters"

    @pytest.mark.parametrize("value", ["", "   ", 5, ["x"], "a\x00b", "a\x1bb", "a​b", "a‮b", "see https://x.example", "go to www.example.com",
                                       "discord.gg/abc"])
    def test_a_bad_text(self, value) -> None:
        assert reason({**COMMIT, "text": value}) == "invalid_text"

    @pytest.mark.parametrize("raw", [{**GOAL, "title": "x" * (MAX_TITLE + 1)}, {**GOAL, "target_description": "x" * (MAX_TARGET + 1)},
                                     {**COMMIT, "text": "x" * (MAX_COMMITMENT + 1)}, {**ADAPT, "reason": "x" * (MAX_REASON + 1)}])
    def test_too_long(self, raw) -> None:
        assert reason(raw) == "invalid_text"

    def test_exactly_at_the_limit_is_accepted(self) -> None:
        assert isinstance(validate_action({**COMMIT, "text": "x" * MAX_COMMITMENT}), ValidatedAction)


class TestContentCheck:
    @pytest.mark.parametrize("text", [
        "I will immediately unlock you right now", "Take a 1-hour break from your lock", "Stay locked for a week", "Ask your keyholder", "Check Chaster daily",
        "Earn a token", "Raise your trust score", "Accept a penalty", "Punish yourself", "Follow the rules", "Skip your check-in", "Extend the session",
        "Freeze it", "Your release comes first", "Odemknu tě hned", "Zámek zůstane", "Dostaneš trest", "Nouzové otevření", "Poslouchej pravidla",
    ])
    def test_the_regime_is_never_a_goal(self, text) -> None:
        assert content_refusal((text,)) == "regime_topic"

    @pytest.mark.parametrize("text", [
        "Fasting for two days", "Skip meals tomorrow", "No sleep tonight", "Stay up all night", "Run without water", "Take pills to stay awake", "Starve a little",
        "Work through the pain", "Ignore the numbness", "Bez jídla celý den", "Dnes v noci nespat",
    ])
    def test_nothing_that_could_hurt_the_user(self, text) -> None:
        assert content_refusal((text,)) == "unsafe_content"

    @pytest.mark.parametrize("text", ["Run three times a week", "Study for 30 minutes", "Go to bed by eleven", "Read twenty pages", "Keep a healthy sleep routine",
                                      "Meditate for 10 minutes"])
    def test_ordinary_development_passes(self, text) -> None:
        assert content_refusal((text,)) is None

    def test_any_field_can_trip_it(self) -> None:
        assert content_refusal(("Run more", "because then I will unlock you")) == "regime_topic"
