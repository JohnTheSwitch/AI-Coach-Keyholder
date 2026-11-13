"""Personas made real (owner, 2026-11-13; roadmap slice 3b): 15 voices on one shared core, and switching after onboarding."""

from __future__ import annotations

import pytest

from ai.identity_catalog import IDENTITY_CATALOG
from ai.persona_voices import PERSONA_VOICES, voice_for
from conversation_engine.prompt_builder import VOICE_GUIDE, persona_voice_text
from tests.application.test_lock_and_task_commands import FIXED_TIME, _complete_onboarding, _incoming, core  # noqa: F401
from application.service import ApplicationService


def test_every_persona_has_its_own_voice_and_all_are_keyholders() -> None:
    assert set(PERSONA_VOICES) == {e.identity_id for e in IDENTITY_CATALOG}
    assert len({v.style for v in PERSONA_VOICES.values()}) == 15
    for identity in PERSONA_VOICES:
        text = persona_voice_text(identity)
        assert text.startswith(f"PERSONA: you are {identity.capitalize()}, a keyholder.") and "warm and direct" in text
    assert voice_for("nobody") is PERSONA_VOICES["alex"] and voice_for(None) is PERSONA_VOICES["alex"]


def test_the_shared_core_no_longer_fixes_one_flavour() -> None:
    assert "Confident and a little possessive" not in VOICE_GUIDE and "Your persona below decides how you sound" in VOICE_GUIDE
    assert "drop the teasing" in VOICE_GUIDE and "never dodge or refuse the topic" in VOICE_GUIDE and "explicit is not" in VOICE_GUIDE
    assert "possessive" in PERSONA_VOICES["scarlett"].style            # the owner's pick keeps today's flavour


def test_the_persona_reaches_the_prompt(core) -> None:  # noqa: F811
    from conversation_engine import prompt_builder
    assert "persona_voice_text(snapshot.identity_id)" in open(prompt_builder.__file__, encoding="utf-8").read()


class TestSwitching:
    def say(self, service, text):
        return service.handle_message(_incoming(text, now=FIXED_TIME)).text

    def test_list_ask_confirm(self, core) -> None:  # noqa: F811
        service = ApplicationService(core.db_path, core=core)
        _complete_onboarding(service)
        listing = self.say(service, "persona")
        assert listing.count("\n") == 16 and "Send `persona <name>` to switch." in listing
        asked = self.say(service, "persona victoria")
        assert "Only the voice changes" in asked and "persona victoria confirm" in asked
        with core.transaction() as tx:
            before = tx.fetch_one("SELECT identity_id FROM user_preferences")["identity_id"]
        assert before != "victoria"
        assert self.say(service, "Persona Victoria confirm") == "Done. Victoria holds your key now."
        with core.transaction() as tx:
            assert tx.fetch_one("SELECT identity_id FROM user_preferences")["identity_id"] == "victoria"
        assert self.say(service, "persona victoria") == "Victoria already holds your key."
        assert "persona victoria" not in service.router.help_text() and "persona" in service.router.help_text()

    def test_switching_changes_nothing_else(self, core) -> None:  # noqa: F811
        service = ApplicationService(core.db_path, core=core)
        _complete_onboarding(service)
        with core.transaction() as tx:
            before = {t: tx.fetch_one(f"SELECT COUNT(*) AS n FROM {t}")["n"] for t in ("trust_domain_state", "trust_evidence", "domain_events", "incidents")}
        self.say(service, "persona damon confirm")
        with core.transaction() as tx:
            after = {t: tx.fetch_one(f"SELECT COUNT(*) AS n FROM {t}")["n"] for t in before}
        assert after == before

    def test_not_before_onboarding(self, core) -> None:  # noqa: F811
        service = ApplicationService(core.db_path, core=core)
        reply = self.say(service, "persona damon confirm")
        assert "Done." not in reply
