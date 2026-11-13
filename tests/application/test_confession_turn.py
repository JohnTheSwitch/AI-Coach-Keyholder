"""Live, 2026-11-12: a confession got praise from the model and was saved to memory inverted. A turn that reads as owning up now gets the
keyholder note as plain context: no coach turn, nothing stored as side data. No network; made-up text."""

from __future__ import annotations

from datetime import datetime, timezone

from application.service import CONFESSION_NOTE, ApplicationService


class _Engine:
    def __init__(self):
        self.kwargs = None

    def generate_response(self, **kwargs):
        self.kwargs = kwargs
        raise RuntimeError("stop here")


def _service(check):
    service = ApplicationService.__new__(ApplicationService)
    service.confession_check = check
    return service


def test_the_note_never_praises_and_never_mentions_the_card() -> None:
    assert "Never praise it" in CONFESSION_NOTE and "Do not mention any card" in CONFESSION_NOTE


def test_a_missing_or_failing_check_is_ordinary_conversation() -> None:
    assert _service(None)._is_confession("x") is False

    def boom(text):
        raise ValueError
    assert _service(boom)._is_confession("x") is False
    assert _service(lambda t: True)._is_confession("x") is True


def test_the_bot_wires_the_same_reading() -> None:
    from pathlib import Path
    source = Path("bot/discord_bot.py").read_text(encoding="utf-8")
    assert "application_service.confession_check = is_hygiene_confession" in source


def test_a_confession_turn_is_plain_context_and_skips_the_coach() -> None:
    from pathlib import Path
    source = Path("application/service.py").read_text(encoding="utf-8")
    assert "guidance = None if confession else self._coach_guidance(" in source
    assert "chat_context = CONFESSION_NOTE if chat_context is None" in source
