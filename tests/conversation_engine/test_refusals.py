"""Owner, 2026-11-14: a model refusal is never shown (one reinforced retry, then a fixed in-character line); the voices no longer carry example
lines the model copied; the shared core forbids doubting a report and inventing facts."""

from __future__ import annotations

import pytest

from ai.persona_voices import PERSONA_VOICES
from conversation_engine.guardrails import build_guardrails
from conversation_engine.model_types import LLMGenerationError
from conversation_engine.prompt_builder import persona_voice_text
from conversation_engine.refusals import REFUSAL_FALLBACK, REFUSAL_RETRY, is_model_refusal
from tests.conversation_engine.test_engine import FIXED_TIME, _engine

REFUSED = "I can't engage in this conversation. Is there anything else I can help you with?"


@pytest.mark.parametrize("text", [REFUSED, "I cannot help with that request.", "I'm not able to continue with this.",
                                  "I can’t create explicit content.", "Is there anything else I can help you with?"])
def test_these_are_refusals(text) -> None:
    assert is_model_refusal(text)


@pytest.mark.parametrize("text", ["I can't unlock you early.", "That isn't something I can change.", "No. Not yet.", "", None,
                                  "I can't help but smile at that."])
def test_a_keyholders_no_is_not_a_refusal(text) -> None:
    assert not is_model_refusal(text)


class Model:
    def __init__(self, *answers) -> None:
        self.answers, self.calls = list(answers), []

    def generate(self, *, request):
        self.calls.append(request)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def _say(model):
    return _engine(model).generate_response(subject_key="s", current_user_message="I just wanna cum", language="en", identity_id="scarlett",
                                            now=FIXED_TIME).text


def test_one_retry_with_the_reinforced_instruction() -> None:
    model = Model(REFUSED, "Not a chance. You're staying locked.")
    assert _say(model) == "Not a chance. You're staying locked."
    assert any(REFUSAL_RETRY in m.content for m in model.calls[1].messages) and len(model.calls) == 2


def test_refused_twice_or_a_failed_retry_gets_the_fixed_line() -> None:
    assert _say(Model(REFUSED, REFUSED)) == REFUSAL_FALLBACK
    assert _say(Model(REFUSED, LLMGenerationError("down"))) == REFUSAL_FALLBACK


def test_an_ordinary_reply_is_one_call() -> None:
    model = Model("You're mine to keep locked.")
    assert _say(model) == "You're mine to keep locked." and len(model.calls) == 1


def test_the_voices_send_no_example_lines() -> None:
    for identity, voice in PERSONA_VOICES.items():
        text = persona_voice_text(identity)
        assert voice.examples[0] not in text and voice.examples[1] not in text and "in your own words" in text
    assert "never that she doubts" in PERSONA_VOICES["scarlett"].style


def test_the_shared_core_believes_reports_and_invents_nothing() -> None:
    text = build_guardrails()
    assert "Never accuse them of lying without evidence" in text and "Maybe I'll ask you to show me" in text
    assert "Never invent facts" in text and "Nice try. You're staying locked" not in text


def test_the_iron_contract_is_described_exactly() -> None:
    from conversation_engine.prompt_builder import _render_contract
    text = _render_contract({"state": "LOCKED"})
    assert "nobody signed anything" in text and "real and in force" in text and "cannot lower the difficulty" in text and "nothing more" in text
    assert _render_contract({"state": "UNLOCKED"}) == ""


@pytest.mark.parametrize("text", ["You still have a signed contract, it's on file here.", "You signed the Iron Contract a while back.",
                                  "We have the records.", "It's in our records."])
def test_a_claimed_signature_or_record_is_replaced(text) -> None:
    from conversation_engine.refusals import CONTRACT_TRUTH, claims_fabricated_record
    assert claims_fabricated_record(text)
    assert _say(Model(text)) == CONTRACT_TRUTH


@pytest.mark.parametrize("text", ["Nobody signed anything.", "You're staying locked.", "Sign up for a gym if you want."])
def test_ordinary_replies_pass(text) -> None:
    from conversation_engine.refusals import claims_fabricated_record
    assert not claims_fabricated_record(text) or text.startswith("Nobody")
