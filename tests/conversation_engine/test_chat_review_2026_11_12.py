"""Owner's chat review, 2026-11-12: stale memories, the detector question the model did not know about, the release rule, a check-in answer
that met a loading model, and the voice. No network; no real conversation text."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from ai.identity_catalog import CommunicationProfile
from conversation_engine.context_providers.coaching_memory_provider import MAX_AGE, CoachingMemoryContextProvider
from conversation_engine.context_providers.detector_questions_provider import DetectorQuestionsContextProvider
from conversation_engine.models import ConversationContextFragment, ResponseCategory, ResponseContextSnapshot, ResponsePlan, SituationalConstraints
from conversation_engine.prompt_builder import build_generation_request
from infractions.signals import get_recent_questions
from memory_system.working_memory import InMemoryWorkingMemory
from tests.infractions.test_detectors import NOW, USER, World

T = datetime(2026, 11, 12, 20, 0, tzinfo=timezone.utc)


def _system_text(fragments: dict) -> str:
    snapshot = ResponseContextSnapshot(
        response_category=ResponseCategory.COACHING_DIALOGUE, current_user_message="hi", language="en",
        identity_profile=CommunicationProfile(warmth=0.5, humor=0.5, teasing=0.5, assertiveness=0.5, formality=0.5, verbosity=0.5),
        identity_id="alex", situational_constraints=SituationalConstraints(), context_fragments=fragments)
    request = build_generation_request(snapshot=snapshot, plan=ResponsePlan(response_category=ResponseCategory.COACHING_DIALOGUE),
                                       working_memory_turns=(), max_output_characters=1800)
    return "\n".join(m.content for m in request.messages)


class TestAOneOffIsNotTalkedAboutForMonths:
    def test_each_note_shows_the_day_it_was_saved(self) -> None:
        provider = CoachingMemoryContextProvider(get_entries=lambda _u: [("quote", "Got burned today", T - timedelta(days=2))])
        fragment = provider.provide_context(subject_key="u", now=T)
        text = _system_text({"coaching_memory": fragment})
        assert "[quote, saved Tue 10 Nov, 2 days ago] Got burned today" in text
        assert "mean THAT day" in text and "never turn it into a running nickname" in text

    def test_old_one_offs_fade_but_a_goal_stays_longer(self) -> None:
        entries = [("quote", "old quote", T - MAX_AGE["quote"] - timedelta(hours=1)),
                   ("behavior_pattern", "old pattern", T - MAX_AGE["behavior_pattern"] - timedelta(hours=1)),
                   ("stated_goal", "a goal", T - timedelta(days=30))]
        fragment = CoachingMemoryContextProvider(get_entries=lambda _u: entries).provide_context(subject_key="u", now=T)
        assert [e["content"] for e in fragment.data["entries"]] == ["a goal"]

    def test_notes_without_a_date_still_work(self) -> None:
        fragment = CoachingMemoryContextProvider(get_entries=lambda _u: [("quote", "x")]).provide_context(subject_key="u", now=T)
        assert [dict(e) for e in fragment.data["entries"]] == [{"category": "quote", "content": "x"}]

    def test_chat_from_days_ago_is_no_longer_the_current_conversation(self) -> None:
        now = [T]
        memory = InMemoryWorkingMemory(max_exchanges_per_subject=10, max_characters_per_subject=8000, clock=lambda: now[0], max_age=timedelta(hours=36))
        memory.commit_exchange(subject_key="u", user_content="I got burned", assistant_content="Ouch.", at=T - timedelta(days=2))
        memory.commit_exchange(subject_key="u", user_content="Morning", assistant_content="Report in.")
        assert [t.content for t in memory.read(subject_key="u").turns] == ["Morning", "Report in."]
        now[0] = T + timedelta(hours=37)
        assert memory.read(subject_key="u").turns == ()

    def test_other_people_may_be_noted_when_they_concern_the_user(self) -> None:
        from conversation_engine.structured_output import FORMAT_INSTRUCTIONS
        assert "never anything about other people" not in FORMAT_INSTRUCTIONS and "as they concern the user" in FORMAT_INSTRUCTIONS
        assert "with dates" in FORMAT_INSTRUCTIONS


class TestTheModelKnowsTheDetectorQuestions:
    def test_an_open_question_is_listed_with_its_deadline(self, tmp_path) -> None:
        w = World(tmp_path)
        w.break_(NOW, 45)
        questions = get_recent_questions(w.core, USER, NOW + timedelta(hours=1), local=lambda d: d)
        assert questions and questions[0]["stage"] == "waiting_for_answer" and questions[0]["until"]
        fragment = DetectorQuestionsContextProvider(read=lambda _u, _n: questions).provide_context(subject_key=USER, now=NOW)
        text = _system_text({"detector_questions": fragment})
        assert "Questions the system asked the user" in text and "waiting for the user's explanation until" in text

    def test_a_counted_one_is_never_reported_as_nothing(self) -> None:
        q = [{"detail": "Several check-ins in a row were missed.", "stage": "counted", "until": None, "closed": "Thu 12 Nov 20:46", "score": 0.3}]
        fragment = DetectorQuestionsContextProvider(read=lambda _u, _n: q).provide_context(subject_key=USER, now=T)
        assert "score" not in fragment.data["questions"][0]
        text = _system_text({"detector_questions": fragment})
        assert "counted as an incident on Thu 12 Nov 20:46" in text and "never invent others" in text

    def test_none_means_no_section(self) -> None:
        assert DetectorQuestionsContextProvider(read=lambda _u, _n: []).provide_context(subject_key=USER, now=T) is None


class TestTheReleaseRule:
    def test_it_may_say_missed_check_ins_stand_in_the_way_but_not_whether_one_is_held(self) -> None:
        for phase in ("requirements_met_pending", "requirements_not_met"):
            text = _system_text({"release": ConversationContextFragment(namespace="release", data={"release_phase": phase})})
            assert "surely not getting unlocked" in text and "never say whether anything is holding a release back right now" in text


class TestACheckInAnswerThatMetALoadingModel:
    def test_the_user_is_told_it_was_recorded(self) -> None:
        from application.service import CHECK_IN_RECEIVED_WITHOUT_MODEL
        assert "check-in answer" in CHECK_IN_RECEIVED_WITHOUT_MODEL and "recorded" in CHECK_IN_RECEIVED_WITHOUT_MODEL

    def test_the_model_is_loaded_once_ollama_is_up(self) -> None:
        from bot.discord_bot import warm_model
        calls = []
        states = iter([SimpleNamespace(reachable=False, installed=False), SimpleNamespace(reachable=True, installed=True)])
        model = SimpleNamespace(check_ready=lambda: next(states), generate=lambda request: calls.append(request) or "OK")
        assert asyncio.run(warm_model(model, attempts=3, pause_seconds=0)) is True and len(calls) == 1

    def test_it_gives_up_quietly(self) -> None:
        from bot.discord_bot import warm_model
        model = SimpleNamespace(check_ready=lambda: SimpleNamespace(reachable=False, installed=False), generate=lambda request: None)
        assert asyncio.run(warm_model(model, attempts=2, pause_seconds=0)) is False


class TestTheVoice:
    def test_no_section_asks_for_a_supportive_coach_any_more(self) -> None:
        text = _system_text({})
        assert "supportive, direct accountability coach" not in text and "Respond as the user's keyholder" in text
        assert "No counsellor talk" in text

    def test_the_guardrail_example_is_a_keyholder(self) -> None:
        from conversation_engine.guardrails import ENGLISH_BEHAVIOR_RULES
        assert "firm, warm and supportive" not in ENGLISH_BEHAVIOR_RULES and "not a counsellor" in ENGLISH_BEHAVIOR_RULES
