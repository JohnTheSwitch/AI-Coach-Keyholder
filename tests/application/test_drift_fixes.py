"""Owner, 2026-11-16: the bot doesn't drift into the Iron Contract; small talk gets no coach proposal."""

from __future__ import annotations

from conversation_engine.prompt_builder import LOCKED_MARKER, _render_contract
from system.coach_dm import has_something_to_do


def test_the_contract_text_only_when_he_asks_about_rules() -> None:
    locked = {"state": "LOCKED"}
    assert _render_contract(locked, "The task is done") == LOCKED_MARKER
    assert _render_contract(locked, "What time is it?") == LOCKED_MARKER
    for asked in ("What is the Iron Contract?", "can I change the difficulty", "did I sign something", "jaká jsou pravidla"):
        assert "The Iron Contract is ONLY this" in _render_contract(locked, asked), asked
    assert _render_contract({"state": "UNLOCKED"}, "rules?") == ""


def test_small_talk_is_not_a_to_do() -> None:
    for talk in ("I don't have a dancing school tomorrow. I have it only every sundays and next saturday I have a ball",
                 "That suit looks fine", "The task is done", "What?"):
        assert not has_something_to_do(talk), talk
    for todo in ("I need to clean my room", "tomorrow I want to finish the bike repair", "musím dodělat projekt", "I'll start running on Monday"):
        assert has_something_to_do(todo), todo
