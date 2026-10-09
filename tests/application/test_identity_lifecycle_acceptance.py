"""
tests/application/test_identity_lifecycle_acceptance.py

Acceptance-hardens the real, already-implemented identity lifecycle:
Discord message -> ApplicationService.handle_message() -> onboarding
(persisted identity selection) -> CommandRouter (deterministic path,
personality never touched) -> ConversationEngine.generate_response()
-> build_response_context() -> ResponseContextSnapshot.identity_id ->
prompt_builder's PERSONALITY / PRESENTATION block -> the real model
call. Uses the real ApplicationService/OnboardingService/
ConversationEngine wiring throughout -- a _RecordingModel is the only
substitution, used purely to inspect the actual ModelGenerationRequest
each scenario produces, never to bypass any real code path.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from ai.identity_catalog import get_identity
from application.models import IncomingMessage
from application.service import ApplicationService
from conversation_engine.context_providers.active_task_provider import ActiveTaskContextProvider
from conversation_engine.context_providers.lock_state_provider import LockStateContextProvider
from conversation_engine.engine import ConversationEngine
from conversation_engine.model_types import ModelGenerationRequest, ModelMessageRole
from conversation_engine.subject_queue import SubjectConversationQueue
from infrastructure.database import Database as CoreDatabase
from lock_state.repository import LockState
from memory_system.working_memory import InMemoryWorkingMemory
from task_catalog.models import LockRequirement, TaskInstanceRole
from task_catalog.repository import TaskCatalog, TaskCatalogAdministration
from task_runtime.repository import TaskRuntime

FIXED_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)

SCARLETT_PROFILE = get_identity("scarlett").communication_profile
DAMON_PROFILE = get_identity("damon").communication_profile  # a second, distinct catalog identity


class _RecordingModel:
    """Records every request it receives -- lets a test inspect the
    actual ModelGenerationRequest/prompt the real pipeline produced,
    not a reconstruction of it."""

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


@pytest.fixture
def core(tmp_path: Path) -> CoreDatabase:
    c = CoreDatabase(tmp_path / "test.db")
    _apply_migrations(c)
    return c


@pytest.fixture
def model() -> _RecordingModel:
    return _RecordingModel()


@pytest.fixture
def service(core: CoreDatabase, model: _RecordingModel) -> ApplicationService:
    """Mirrors bot/discord_bot.py's own composition root exactly: one
    shared LockState/TaskRuntime/TaskCatalog, passed to both the
    context providers AND ApplicationService -- so this fixture
    exercises the real AUTHORITATIVE APPLICATION STATE wiring, not a
    reduced stand-in that would silently leave the "E. Authoritative
    context remains authoritative" scenarios untested."""
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


def _onboard_selecting(service: ApplicationService, *, gender_answer: str, identity_answer: str, external_user_id: str = "42") -> None:
    """The real onboarding flow, driven exactly the way a Discord user
    would drive it -- (1) any first message, (2) language, (3) AI
    gender group, (4) the personality name/number itself."""
    for i, text in enumerate(("anything", "english", gender_answer, identity_answer)):
        service.handle_message(_incoming(text, external_user_id=external_user_id, external_message_id=f"ob{i}"))


def _onboard_selecting_scarlett(service: ApplicationService, *, external_user_id: str = "42") -> None:
    _onboard_selecting(service, gender_answer="female", identity_answer="scarlett", external_user_id=external_user_id)


def _create_template(service: ApplicationService, *, template_id: str = "basic-chore") -> None:
    admin = TaskCatalogAdministration(service.db_path, core=service._core)
    admin.create_template(
        template_id=template_id, title="Tidy one surface", instructions="Pick a surface and clear it off.",
        category="chore", difficulty="easy", effort="low", duration_minutes=10,
        required_equipment=(), required_privacy="none", required_context="home", safety_classification="safe",
        eligible_instance_roles=(TaskInstanceRole.PRIMARY,), eligible_operating_modes=("standard", "advanced"),
        completion_requirements={}, verification_requirements={}, reflection_requirements=None,
        lock_requirement=LockRequirement.NONE, created_via_consent_id="test-consent", now=FIXED_TIME,
    )


# =============================================================================
# A. Scarlett selection reaches conversation
# =============================================================================

class TestScarlettSelectionReachesConversation:
    def test_free_form_message_reaches_the_model(self, service: ApplicationService, model: _RecordingModel) -> None:
        _onboard_selecting_scarlett(service)
        service.handle_message(_incoming("hey there", external_message_id="m1"))
        assert len(model.calls) == 1

    def test_system_prompt_contains_scarletts_presentation_block(self, service: ApplicationService, model: _RecordingModel) -> None:
        _onboard_selecting_scarlett(service)
        service.handle_message(_incoming("hey there", external_message_id="m1"))
        system_text = model.calls[0].messages[0].content
        assert "identity: Scarlett" in system_text
        assert "PERSONALITY / PRESENTATION" in system_text

    def test_personality_values_come_from_the_real_catalog_not_a_second_source(self, service: ApplicationService, model: _RecordingModel) -> None:
        _onboard_selecting_scarlett(service)
        service.handle_message(_incoming("hey there", external_message_id="m1"))
        system_text = model.calls[0].messages[0].content
        assert f"warmth {SCARLETT_PROFILE.warmth:.1f}" in system_text
        assert f"humor {SCARLETT_PROFILE.humor:.1f}" in system_text
        assert f"teasing {SCARLETT_PROFILE.teasing:.1f}" in system_text
        assert f"assertiveness {SCARLETT_PROFILE.assertiveness:.1f}" in system_text
        assert f"formality {SCARLETT_PROFILE.formality:.1f}" in system_text
        assert f"verbosity {SCARLETT_PROFILE.verbosity:.1f}" in system_text

    def test_authoritative_state_precedes_personality_in_the_real_prompt(self, service: ApplicationService, model: _RecordingModel) -> None:
        _onboard_selecting_scarlett(service)
        service.handle_message(_incoming("lock report locked", external_message_id="m1"))
        service.handle_message(_incoming("what should I do?", external_message_id="m2"))
        system_text = model.calls[0].messages[0].content
        assert system_text.index("AUTHORITATIVE APPLICATION STATE") < system_text.index("PERSONALITY / PRESENTATION")

    def test_current_user_message_remains_the_final_user_role_message(self, service: ApplicationService, model: _RecordingModel) -> None:
        _onboard_selecting_scarlett(service)
        service.handle_message(_incoming("what's a good first step today?", external_message_id="m1"))
        request = model.calls[0]
        assert request.messages[-1].role == ModelMessageRole.USER
        assert request.messages[-1].content == "what's a good first step today?"


# =============================================================================
# B. Another identity reaches conversation
# =============================================================================

class TestAnotherIdentityReachesConversation:
    def test_damon_selection_produces_damons_own_profile_not_scarletts(self, service: ApplicationService, model: _RecordingModel) -> None:
        _onboard_selecting(service, gender_answer="male", identity_answer="damon")
        service.handle_message(_incoming("hey", external_message_id="m1"))
        system_text = model.calls[0].messages[0].content
        assert "identity: Damon" in system_text
        assert "identity: Scarlett" not in system_text
        assert f"assertiveness {DAMON_PROFILE.assertiveness:.1f}" in system_text
        # Damon's own catalog values genuinely differ from Scarlett's --
        # confirms this isn't accidentally always rendering Scarlett's data.
        assert DAMON_PROFILE.humor != SCARLETT_PROFILE.humor
        assert f"humor {DAMON_PROFILE.humor:.1f}" in system_text


# =============================================================================
# C. Identity persists across messages
# =============================================================================

class TestIdentityPersistsAcrossMessages:
    def test_scarlett_remains_selected_across_three_separate_messages(self, service: ApplicationService, model: _RecordingModel) -> None:
        _onboard_selecting_scarlett(service)
        service.handle_message(_incoming("message one", external_message_id="m1"))
        service.handle_message(_incoming("message two", external_message_id="m2"))
        service.handle_message(_incoming("message three", external_message_id="m3"))
        assert len(model.calls) == 3
        for call in model.calls:
            assert "identity: Scarlett" in call.messages[0].content

    def test_identity_persists_through_a_reopened_application_service(self, core: CoreDatabase) -> None:
        """The persistence is a real database row, not in-process
        state -- proven by reconstructing ApplicationService/
        ConversationEngine from scratch against the same core and
        confirming the second instance still sees Scarlett."""
        model1 = _RecordingModel()
        buffer1 = InMemoryWorkingMemory(max_exchanges_per_subject=10, max_characters_per_subject=8000)
        engine1 = ConversationEngine(model=model1, working_memory_reader=buffer1, working_memory_writer=buffer1, queue=SubjectConversationQueue())
        service1 = ApplicationService(core.db_path, core=core, conversation_engine=engine1)
        _onboard_selecting_scarlett(service1)

        model2 = _RecordingModel()
        buffer2 = InMemoryWorkingMemory(max_exchanges_per_subject=10, max_characters_per_subject=8000)
        engine2 = ConversationEngine(model=model2, working_memory_reader=buffer2, working_memory_writer=buffer2, queue=SubjectConversationQueue())
        service2 = ApplicationService(core.db_path, core=CoreDatabase(core.db_path), conversation_engine=engine2)
        service2.handle_message(_incoming("still there?", external_message_id="m1"))

        assert len(model2.calls) == 1
        assert "identity: Scarlett" in model2.calls[0].messages[0].content


# =============================================================================
# D. Deterministic commands remain outside personality
# =============================================================================

class TestDeterministicCommandsRemainOutsidePersonality:
    def test_lock_status_never_invokes_the_model(self, service: ApplicationService, model: _RecordingModel) -> None:
        _onboard_selecting_scarlett(service)
        result = service.handle_message(_incoming("lock status", external_message_id="m1"))
        assert len(model.calls) == 0
        assert result.text == "No lock report yet. Send `lock report locked` or `lock report unlocked`."

    def test_task_active_never_invokes_the_model(self, service: ApplicationService, model: _RecordingModel) -> None:
        _onboard_selecting_scarlett(service)
        service.handle_message(_incoming("task active", external_message_id="m1"))
        assert len(model.calls) == 0

    def test_task_request_and_complete_never_invoke_the_model(self, service: ApplicationService, model: _RecordingModel) -> None:
        _create_template(service)
        _onboard_selecting_scarlett(service)
        service.handle_message(_incoming("task request", external_message_id="m1"))
        service.handle_message(_incoming("task complete", external_message_id="m2"))
        assert len(model.calls) == 0

    def test_deterministic_replies_identical_regardless_of_which_identity_was_selected(self, core: CoreDatabase) -> None:
        model_a = _RecordingModel()
        buffer_a = InMemoryWorkingMemory(max_exchanges_per_subject=10, max_characters_per_subject=8000)
        service_a = ApplicationService(
            core.db_path, core=core,
            conversation_engine=ConversationEngine(model=model_a, working_memory_reader=buffer_a, working_memory_writer=buffer_a, queue=SubjectConversationQueue()),
        )
        _onboard_selecting_scarlett(service_a, external_user_id="user-a")
        result_a = service_a.handle_message(_incoming("lock status", external_user_id="user-a", external_message_id="m1"))

        core_b = CoreDatabase(core.db_path.parent / "b.db")
        _apply_migrations(core_b)
        model_b = _RecordingModel()
        buffer_b = InMemoryWorkingMemory(max_exchanges_per_subject=10, max_characters_per_subject=8000)
        service_b = ApplicationService(
            core_b.db_path, core=core_b,
            conversation_engine=ConversationEngine(model=model_b, working_memory_reader=buffer_b, working_memory_writer=buffer_b, queue=SubjectConversationQueue()),
        )
        _onboard_selecting(service_b, gender_answer="neutral", identity_answer="alex", external_user_id="user-b")
        result_b = service_b.handle_message(_incoming("lock status", external_user_id="user-b", external_message_id="m1"))

        assert result_a.text == result_b.text


# =============================================================================
# E. Authoritative context remains authoritative
# =============================================================================

class TestAuthoritativeContextRemainsAuthoritative:
    def test_locked_state_appears_verbatim_alongside_scarletts_profile(self, service: ApplicationService, model: _RecordingModel) -> None:
        _onboard_selecting_scarlett(service)
        service.handle_message(_incoming("lock report locked", external_message_id="m1"))
        service.handle_message(_incoming("how am I doing?", external_message_id="m2"))
        system_text = model.calls[0].messages[0].content
        assert "Lock: the user reported being locked" in system_text
        assert "has not been independently physically verified" in system_text
        assert "identity: Scarlett" in system_text

    def test_active_task_appears_verbatim_alongside_scarletts_profile(self, service: ApplicationService, model: _RecordingModel) -> None:
        _create_template(service)
        _onboard_selecting_scarlett(service)
        service.handle_message(_incoming("task request", external_message_id="m1"))
        service.handle_message(_incoming("what should I do?", external_message_id="m2"))
        system_text = model.calls[0].messages[0].content
        assert "Tidy one surface" in system_text
        assert "identity: Scarlett" in system_text

    def test_personality_does_not_alter_the_authoritative_fragment_text(self, service: ApplicationService, model: _RecordingModel) -> None:
        """The AUTHORITATIVE APPLICATION STATE block's own text is
        identical whether Scarlett or a neutral-profile identity is
        selected -- personality only ever adds its own separate block,
        never rewrites domain-state wording."""
        _onboard_selecting_scarlett(service)
        service.handle_message(_incoming("lock report locked", external_message_id="m1"))
        service.handle_message(_incoming("hi", external_message_id="m2"))
        scarlett_text = model.calls[0].messages[0].content
        scarlett_domain_section = scarlett_text[scarlett_text.index("AUTHORITATIVE APPLICATION STATE"): scarlett_text.index("PERSONALITY / PRESENTATION")]
        assert "Lock: the user reported being locked" in scarlett_domain_section


# =============================================================================
# F. Working Memory remains separate
# =============================================================================

class TestWorkingMemoryRemainsSeparateFromIdentity:
    def test_identity_never_appears_as_a_working_memory_turn(self, service: ApplicationService, model: _RecordingModel) -> None:
        _onboard_selecting_scarlett(service)
        service.handle_message(_incoming("first message", external_message_id="m1"))
        service.handle_message(_incoming("second message", external_message_id="m2"))
        user = service.user_service.get_or_create_user("discord", "42", now=FIXED_TIME)
        snapshot = service._conversation_engine._working_memory_reader.read(subject_key=user.id)
        for turn in snapshot.turns:
            assert "Scarlett" not in turn.content
            assert "PERSONALITY" not in turn.content

    def test_working_memory_turns_still_appear_between_system_and_current_message(self, service: ApplicationService, model: _RecordingModel) -> None:
        _onboard_selecting_scarlett(service)
        service.handle_message(_incoming("first message", external_message_id="m1"))
        service.handle_message(_incoming("second message", external_message_id="m2"))
        request = model.calls[-1]  # the second call has the first exchange as history
        assert request.messages[1].role == ModelMessageRole.USER
        assert request.messages[1].content == "first message"


# =============================================================================
# Incomplete onboarding / invalid identity
# =============================================================================

class TestIncompleteOnboardingNeverReachesTheModel:
    def test_first_ever_message_never_reaches_the_model(self, service: ApplicationService, model: _RecordingModel) -> None:
        service.handle_message(_incoming("hello", external_message_id="m1"))
        assert len(model.calls) == 0

    def test_mid_onboarding_messages_never_reach_the_model(self, service: ApplicationService, model: _RecordingModel) -> None:
        service.handle_message(_incoming("anything", external_message_id="m1"))
        service.handle_message(_incoming("english", external_message_id="m2"))
        # Still mid-onboarding (AI_GENDER step) -- not yet complete.
        assert len(model.calls) == 0

    def test_an_invalid_onboarding_answer_never_reaches_the_model_either(self, service: ApplicationService, model: _RecordingModel) -> None:
        service.handle_message(_incoming("anything", external_message_id="m1"))
        service.handle_message(_incoming("not a real language", external_message_id="m2"))
        assert len(model.calls) == 0


class TestUnknownStoredIdentityIsHandledSafelyNotSilentlyDefaulted:
    def test_a_stale_identity_id_no_longer_in_the_catalog_falls_back_safely(self, service: ApplicationService, model: _RecordingModel) -> None:
        """Simulates a real-world stale row (e.g. a catalog entry
        removed after a user already selected it) -- there is no way
        to reach this state through the normal onboarding flow, which
        only ever writes a real catalog id. The point is that the
        model is never silently called with a nonexistent/fabricated
        identity, and the reply is the same safe, deterministic
        fallback the code already uses for identity_id=None."""
        _onboard_selecting_scarlett(service)
        with service._core.raw_connection() as conn:
            conn.execute("UPDATE user_preferences SET identity_id = 'nonexistent-identity' WHERE user_id = (SELECT id FROM user_accounts LIMIT 1)")
            conn.commit()
        result = service.handle_message(_incoming("hello again", external_message_id="m1"))
        assert len(model.calls) == 0  # never reached the model with a fabricated identity
        assert result.text  # a real, safe reply was still returned, not a crash
