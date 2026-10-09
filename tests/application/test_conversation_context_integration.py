"""
tests/application/test_conversation_context_integration.py

Slice D end-to-end: ApplicationService wired with real
LockStateContextProvider/ActiveTaskContextProvider, verifying ordinary
conversation actually receives authoritative lock/task context, that
Working Memory contradiction never overrides it, and that everything
persists correctly across a fresh service/engine after a "restart."
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from application.models import IncomingMessage
from application.service import ApplicationService
from conversation_engine.context_providers.active_task_provider import ActiveTaskContextProvider
from conversation_engine.context_providers.lock_state_provider import LockStateContextProvider
from conversation_engine.engine import ConversationEngine
from conversation_engine.model_types import ModelGenerationRequest
from conversation_engine.subject_queue import SubjectConversationQueue
from infrastructure.database import Database as CoreDatabase
from lock_state.models import LockReportStatus
from lock_state.repository import LockState, LockStateAdministration
from memory_system.working_memory import InMemoryWorkingMemory
from task_catalog.models import LockRequirement, TaskInstanceRole
from task_catalog.repository import TaskCatalog, TaskCatalogAdministration
from task_runtime.repository import TaskRuntime

FIXED_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


class _RecordingModel:
    def __init__(self, response: str = "a plain conversational reply") -> None:
        self._response = response
        self.calls: list[ModelGenerationRequest] = []

    def generate(self, *, request: ModelGenerationRequest) -> str:
        self.calls.append(request)
        return self._response


def _apply_migrations(core: CoreDatabase) -> None:
    migrations_dir = Path(__file__).parent.parent.parent / "database" / "migrations"
    with core.raw_connection() as conn:
        for path in sorted(migrations_dir.glob("*.sql")):
            conn.executescript(path.read_text(encoding="utf-8"))


def _incoming(text: str, *, external_user_id: str = "42", external_message_id: str | None = None) -> IncomingMessage:
    return IncomingMessage(
        channel="discord", external_user_id=external_user_id, text=text, received_at=FIXED_TIME,
        external_message_id=external_message_id,
    )


def _complete_onboarding(service: ApplicationService, *, external_user_id: str = "42") -> None:
    for i, text in enumerate(("anything", "english", "neutral", "alex")):
        service.handle_message(_incoming(text, external_user_id=external_user_id, external_message_id=f"ob{i}"))


def _create_template(service: ApplicationService, *, template_id: str = "basic-chore") -> None:
    admin = TaskCatalogAdministration(service.db_path, core=service._core)
    admin.create_template(
        template_id=template_id, title="Test task", instructions="Do the test task.",
        category="chore", difficulty="easy", effort="low", duration_minutes=10,
        required_equipment=(), required_privacy="none", required_context="home", safety_classification="safe",
        eligible_instance_roles=(TaskInstanceRole.PRIMARY,), eligible_operating_modes=("standard", "advanced"),
        completion_requirements={}, verification_requirements={}, reflection_requirements=None,
        lock_requirement=LockRequirement.NONE, created_via_consent_id="test-consent", now=FIXED_TIME,
    )


@pytest.fixture
def core(tmp_path: Path) -> CoreDatabase:
    c = CoreDatabase(tmp_path / "test.db")
    _apply_migrations(c)
    return c


def _build_service(core: CoreDatabase, model: "_RecordingModel") -> ApplicationService:
    """Mirrors bot/discord_bot.py's own composition root: one shared
    LockState/TaskRuntime/TaskCatalog, passed to both the context
    providers AND ApplicationService."""
    lock_state = LockState(core.db_path, core=core)
    task_runtime = TaskRuntime(core.db_path, core=core)
    task_catalog = TaskCatalog(core.db_path, core=core)
    providers = (
        LockStateContextProvider(lock_state=lock_state),
        ActiveTaskContextProvider(task_runtime=task_runtime, task_catalog=task_catalog),
    )
    buffer = InMemoryWorkingMemory(max_exchanges_per_subject=10, max_characters_per_subject=8000)
    engine = ConversationEngine(
        model=model, working_memory_reader=buffer, working_memory_writer=buffer,
        queue=SubjectConversationQueue(), providers=providers,
    )
    return ApplicationService(
        core.db_path, core=core, conversation_engine=engine,
        lock_state=lock_state, task_runtime=task_runtime, task_catalog=task_catalog,
    )


class TestOrdinaryConversationReceivesAuthoritativeContext:
    def test_lock_context_reaches_the_model(self, core: CoreDatabase) -> None:
        model = _RecordingModel()
        service = _build_service(core, model)
        _complete_onboarding(service)
        LockStateAdministration(service.db_path, core=core).report_status(
            user_id=service.user_service.get_or_create_user("discord", "42", now=FIXED_TIME).id,
            status=LockReportStatus.LOCKED_USER_REPORTED, reported_via_consent_id="c1", now=FIXED_TIME,
        )
        service.handle_message(_incoming("how am I doing?", external_message_id="m1"))
        assert len(model.calls) == 1
        system_text = model.calls[0].messages[0].content
        assert "the user reported being locked" in system_text

    def test_active_task_context_reaches_the_model(self, core: CoreDatabase) -> None:
        model = _RecordingModel()
        service = _build_service(core, model)
        _create_template(service)
        _complete_onboarding(service)
        service.handle_message(_incoming("task request", external_message_id="m1"))
        model.calls.clear()
        service.handle_message(_incoming("what should I do?", external_message_id="m2"))
        system_text = model.calls[0].messages[0].content
        assert "template_id=basic-chore" in system_text

    def test_no_active_task_represented_correctly(self, core: CoreDatabase) -> None:
        model = _RecordingModel()
        service = _build_service(core, model)
        _complete_onboarding(service)
        service.handle_message(_incoming("what should I do?", external_message_id="m1"))
        system_text = model.calls[0].messages[0].content
        assert "Active task: none." in system_text


class TestWorkingMemoryDoesNotOverrideAuthoritativeState:
    def test_wm_saying_unlocked_does_not_suppress_locked_authoritative_fact(self, core: CoreDatabase) -> None:
        model = _RecordingModel()
        service = _build_service(core, model)
        _complete_onboarding(service)
        user_id = service.user_service.get_or_create_user("discord", "42", now=FIXED_TIME).id

        # First exchange: model says something implying unlocked, gets committed to WM.
        model._response = "Sounds like you're unlocked then!"
        service.handle_message(_incoming("hey", external_message_id="m1"))

        # Now the authoritative state says LOCKED.
        LockStateAdministration(service.db_path, core=core).report_status(
            user_id=user_id, status=LockReportStatus.LOCKED_USER_REPORTED, reported_via_consent_id="c1", now=FIXED_TIME,
        )
        model._response = "ok"
        service.handle_message(_incoming("what's my status?", external_message_id="m2"))

        system_text = model.calls[-1].messages[0].content
        assert "the user reported being locked" in system_text
        # WM's own earlier "unlocked" text lives only as a role="user"/"assistant" turn, never in the system message:
        wm_turns_text = "".join(m.content for m in model.calls[-1].messages[1:-1])
        # (not asserting absence of the word "unlocked" in WM turns -- it's legitimately there as conversation history;
        #  the real assertion is that the AUTHORITATIVE section in the system message says LOCKED regardless)
        assert "AUTHORITATIVE APPLICATION STATE" in system_text


class TestPersistenceAcrossRestart:
    def test_new_service_engine_still_sees_persisted_lock_and_task_facts(self, core: CoreDatabase) -> None:
        model1 = _RecordingModel()
        service1 = _build_service(core, model1)
        _create_template(service1)
        _complete_onboarding(service1)
        service1.handle_message(_incoming("lock report locked", external_message_id="m1"))
        service1.handle_message(_incoming("task request", external_message_id="m2"))

        # "restart" -- brand new service/engine over the same DB file
        model2 = _RecordingModel()
        service2 = _build_service(core, model2)
        service2.handle_message(_incoming("what's going on?", external_user_id="42", external_message_id="m3"))

        system_text = model2.calls[0].messages[0].content
        assert "the user reported being locked" in system_text
        assert "template_id=basic-chore" in system_text


class TestKnownCommandsStillBypassModelAndWorkingMemory:
    def test_lock_report_locked_still_never_calls_the_model(self, core: CoreDatabase) -> None:
        model = _RecordingModel()
        service = _build_service(core, model)
        _complete_onboarding(service)
        service.handle_message(_incoming("lock report locked", external_message_id="m1"))
        assert model.calls == []

    def test_task_request_still_never_calls_the_model(self, core: CoreDatabase) -> None:
        model = _RecordingModel()
        service = _build_service(core, model)
        _create_template(service)
        _complete_onboarding(service)
        service.handle_message(_incoming("task request", external_message_id="m1"))
        assert model.calls == []
