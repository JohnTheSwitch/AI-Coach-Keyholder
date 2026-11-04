"""tests/conversation_engine/test_application_conversation_integration.py"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from application.models import IncomingMessage
from application.service import ApplicationService
from conversation_engine.engine import ConversationEngine
from memory_system.working_memory import InMemoryWorkingMemory
from conversation_engine.subject_queue import SubjectConversationQueue
from infrastructure.database import Database as CoreDatabase

FIXED_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


class _SlowModel:
    """Simulates a model response slower than typical, but still under
    the read timeout -- proves a slow-but-successful call still works,
    without ever actually sleeping for a real timeout duration."""

    def __init__(self, response: str, *, delay_seconds: float = 0.05) -> None:
        self._response = response
        self._delay = delay_seconds

    def generate(self, *, request) -> str:
        time.sleep(self._delay)
        return self._response


def _apply_migrations(core: CoreDatabase) -> None:
    migrations_dir = Path(__file__).parent.parent.parent / "database" / "migrations"
    with core.raw_connection() as conn:
        for path in sorted(migrations_dir.glob("*.sql")):
            conn.executescript(path.read_text(encoding="utf-8"))


def _incoming(text: str, *, external_message_id: str | None = None, external_user_id: str = "1") -> IncomingMessage:
    return IncomingMessage(
        channel="discord", external_user_id=external_user_id, text=text, received_at=FIXED_TIME,
        external_message_id=external_message_id,
    )


def _complete_onboarding(service: ApplicationService, *, external_user_id: str = "1") -> None:
    for i, text in enumerate(("anything", "english", "neutral", "alex")):
        service.handle_message(_incoming(text, external_message_id=f"onboard-{external_user_id}-{i}", external_user_id=external_user_id))


class TestIdentityFallback:
    def test_no_identity_selected_never_calls_the_model(self, tmp_path: Path) -> None:
        """identity_id is None until onboarding's own personality step
        completes -- but even a fully onboarded UserPreferences row
        could in principle have identity_id left None; this must fall
        back deterministically, never call the model."""
        core = CoreDatabase(tmp_path / "test.db")
        _apply_migrations(core)

        class _AssertNeverCalledModel:
            def generate(self, *, request):
                raise AssertionError("model must never be called when identity_id is None")

        wm = InMemoryWorkingMemory(max_exchanges_per_subject=5, max_characters_per_subject=5000)
        engine = ConversationEngine(
            model=_AssertNeverCalledModel(),
            working_memory_reader=wm, working_memory_writer=wm,
            queue=SubjectConversationQueue(),
        )
        service = ApplicationService(core.db_path, core=core, conversation_engine=engine)

        service.handle_message(_incoming("anything", external_message_id="a"))
        service.handle_message(_incoming("english", external_message_id="b"))
        service.handle_message(_incoming("neutral", external_message_id="c"))
        service.handle_message(_incoming("alex", external_message_id="d"))

        with core.transaction() as tx:
            tx.execute("UPDATE user_preferences SET identity_id = NULL WHERE user_id = (SELECT id FROM user_accounts LIMIT 1)")

        result = service.handle_message(_incoming("hello there", external_message_id="e"))
        assert "don't have enough information" in result.text.lower()


class TestSlowModelDoesNotBreakAnything:
    def test_a_slow_but_successful_model_call_still_returns_correctly(self, tmp_path: Path) -> None:
        core = CoreDatabase(tmp_path / "test.db")
        _apply_migrations(core)
        wm = InMemoryWorkingMemory(max_exchanges_per_subject=5, max_characters_per_subject=5000)
        engine = ConversationEngine(
            model=_SlowModel("delayed but fine"),
            working_memory_reader=wm, working_memory_writer=wm,
            queue=SubjectConversationQueue(),
        )
        service = ApplicationService(core.db_path, core=core, conversation_engine=engine)
        _complete_onboarding(service)

        result = service.handle_message(_incoming("hi", external_message_id="m1"))
        assert result.text == "delayed but fine"


class TestKnownCommandServedDuringAnotherSubjectsGeneration:
    def test_known_command_for_one_user_is_not_blocked_by_another_users_generation(self, tmp_path: Path) -> None:
        core = CoreDatabase(tmp_path / "test.db")
        _apply_migrations(core)
        wm = InMemoryWorkingMemory(max_exchanges_per_subject=5, max_characters_per_subject=5000)
        engine = ConversationEngine(
            model=_SlowModel("slow reply", delay_seconds=0.2),
            working_memory_reader=wm, working_memory_writer=wm,
            queue=SubjectConversationQueue(),
        )
        service = ApplicationService(core.db_path, core=core, conversation_engine=engine)
        _complete_onboarding(service, external_user_id="user-a")
        _complete_onboarding(service, external_user_id="user-b")

        results: dict[str, str] = {}

        def slow_conversation() -> None:
            r = service.handle_message(_incoming("chat with me", external_message_id="slow1", external_user_id="user-a"))
            results["a"] = r.text

        def fast_command() -> None:
            time.sleep(0.05)
            r = service.handle_message(_incoming("help", external_message_id="fast1", external_user_id="user-b"))
            results["b"] = r.text

        t1 = threading.Thread(target=slow_conversation)
        t2 = threading.Thread(target=fast_command)
        start = time.monotonic()
        t1.start()
        t2.start()
        t2.join(timeout=5)
        b_done_at = time.monotonic() - start
        t1.join(timeout=5)

        assert "just talk to me" in results["b"]
        assert results["a"] == "slow reply"
        assert b_done_at < 0.2
