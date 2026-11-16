"""Bug fix 2026-11-16: "You currently have no active locks" for the owner, whose Discord account is connected to the keyholder's Chaster account."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from application.service import ApplicationService
from chaster.llm_tools import ChasterToolOutcome, ChasterToolOutcomeKind
from conversation_engine.context_providers.held_lock_provider import HeldLockContextProvider
from conversation_engine.prompt_builder import _render_held_lock

NOW = datetime(2026, 11, 16, 15, 0, tzinfo=timezone.utc)
OBS = SimpleNamespace(status=SimpleNamespace(value="locked"), is_frozen=True, fetched_at=NOW)


def test_no_worn_lock_shows_the_lock_the_keyholder_holds() -> None:
    service = ApplicationService.__new__(ApplicationService)
    service.held_lock, service.held_lock_clock = (lambda: OBS), (lambda at: "16 Nov 16:00")
    reply = service._held_lock_message(ChasterToolOutcome(kind=ChasterToolOutcomeKind.LOCKS, locks=()), NOW)
    assert "**locked**" in reply.text and "frozen" in reply.text and "16 Nov 16:00" in reply.text
    assert service._held_lock_message(ChasterToolOutcome(kind=ChasterToolOutcomeKind.LOCKS, locks=({"x": 1},)), NOW) is None
    service.held_lock = lambda: None
    assert service._held_lock_message(ChasterToolOutcome(kind=ChasterToolOutcomeKind.LOCKS, locks=()), NOW) is None


def test_the_model_is_told_he_is_locked() -> None:
    fragment = HeldLockContextProvider(get_latest=lambda: OBS).provide_context(subject_key="u", now=NOW)
    assert "the user IS locked and frozen" in _render_held_lock(fragment.data) and "Never tell them they are not locked" in _render_held_lock(fragment.data)
    assert HeldLockContextProvider(get_latest=lambda: None).provide_context(subject_key="u", now=NOW) is None
