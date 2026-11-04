"""
tests/application/test_lock_and_task_commands.py

Tests for `lock status`/`lock report locked`/`lock report unlocked`
and `task request`/`task active`/`task complete`/`task cancel` wired
into ApplicationService (First Testable Keyholder Milestone, Slice C).
Uses ApplicationService.handle_message() end-to-end (through the real
CommandRouter) -- domain-level behavior is already covered by
tests/lock_state/ and tests/task_runtime/.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from application.models import IncomingMessage
from application.service import ApplicationService
from infrastructure.database import Database as CoreDatabase
from task_catalog.models import LockRequirement, TaskInstanceRole

FIXED_TIME = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _apply_migrations(core: CoreDatabase) -> None:
    migrations_dir = Path(__file__).parent.parent.parent / "database" / "migrations"
    with core.raw_connection() as conn:
        for path in sorted(migrations_dir.glob("*.sql")):
            conn.executescript(path.read_text(encoding="utf-8"))


@pytest.fixture
def core(tmp_path: Path) -> CoreDatabase:
    c = CoreDatabase(tmp_path / "test.db")
    _apply_migrations(c)
    return c


@pytest.fixture
def service(core: CoreDatabase) -> ApplicationService:
    return ApplicationService(core.db_path, core=core)


def _incoming(
    text: str, *, external_user_id: str = "42", now: datetime = FIXED_TIME, external_message_id: str | None = None,
) -> IncomingMessage:
    return IncomingMessage(
        channel="discord", external_user_id=external_user_id, text=text, received_at=now,
        external_message_id=external_message_id,
    )


def _complete_onboarding(service: ApplicationService, *, external_user_id: str = "42", now: datetime = FIXED_TIME) -> None:
    service.handle_message(_incoming("anything", external_user_id=external_user_id, now=now, external_message_id="ob0"))
    service.handle_message(_incoming("english", external_user_id=external_user_id, now=now, external_message_id="ob1"))
    service.handle_message(_incoming("neutral", external_user_id=external_user_id, now=now, external_message_id="ob2"))
    service.handle_message(_incoming("alex", external_user_id=external_user_id, now=now, external_message_id="ob3"))


def _create_template(
    service: ApplicationService, *, template_id: str, lock_requirement: LockRequirement, title: str = "Test task",
) -> None:
    from task_catalog.repository import TaskCatalogAdministration
    admin = TaskCatalogAdministration(service.db_path, core=service._core)
    admin.create_template(
        template_id=template_id, title=title, instructions="Do the test task.",
        category="chore", difficulty="easy", effort="low", duration_minutes=10,
        required_equipment=(), required_privacy="none", required_context="home", safety_classification="safe",
        eligible_instance_roles=(TaskInstanceRole.PRIMARY,), eligible_operating_modes=("standard", "advanced"),
        completion_requirements={}, verification_requirements={}, reflection_requirements=None,
        lock_requirement=lock_requirement, created_via_consent_id="test-consent", now=FIXED_TIME,
    )


def _create_template_with_null_title(service: ApplicationService, *, template_id: str) -> None:
    """
    Simulates a legacy (pre-migration-021) row -- there is no way to
    construct one through the governed write API, which always
    requires real title/instructions. Mirrors the same raw-SQL
    approach already used in
    tests/conversation_engine/context_providers/test_active_task_provider.py's
    own TestLegacyRowWithNoRecordedContent.
    """
    _create_template(service, template_id=template_id, lock_requirement=LockRequirement.NONE, title="placeholder")
    with service._core.raw_connection() as conn:
        conn.execute(
            "UPDATE task_template_versions SET title = NULL WHERE template_id = ? AND version = 1", (template_id,),
        )
        conn.commit()


class TestLockStatus:
    def test_unknown_before_any_report(self, service: ApplicationService) -> None:
        _complete_onboarding(service)
        result = service.handle_message(_incoming("lock status", external_message_id="m1"))
        assert "haven't told me your lock status" in result.text.lower()

    def test_reports_locked(self, service: ApplicationService) -> None:
        _complete_onboarding(service)
        service.handle_message(_incoming("lock report locked", external_message_id="m1"))
        result = service.handle_message(_incoming("lock status", external_message_id="m2"))
        assert "locked (as you reported)" in result.text.lower()

    def test_reports_unlocked(self, service: ApplicationService) -> None:
        _complete_onboarding(service)
        service.handle_message(_incoming("lock report unlocked", external_message_id="m1"))
        result = service.handle_message(_incoming("lock status", external_message_id="m2"))
        assert "unlocked (as you reported)" in result.text.lower()

    def test_second_report_supersedes_first(self, service: ApplicationService) -> None:
        _complete_onboarding(service)
        service.handle_message(_incoming("lock report locked", external_message_id="m1"))
        service.handle_message(_incoming("lock report unlocked", external_message_id="m2"))
        result = service.handle_message(_incoming("lock status", external_message_id="m3"))
        assert "unlocked" in result.text.lower()

    def test_bare_lock_word_gets_family_reply(self, service: ApplicationService) -> None:
        """Command Family Fallback Precision: the bare family word
        alone still gets the family-specific reply."""
        _complete_onboarding(service)
        result = service.handle_message(_incoming("lock", external_message_id="m1"))
        assert "lock status" in result.text.lower() and "didn't catch that" in result.text.lower()

    def test_lock_frobnicate_no_longer_gets_the_family_reply(self, service: ApplicationService) -> None:
        """Command Family Fallback Precision (Option A): a near-miss
        multi-word input starting with "lock" is no longer intercepted
        by the family fallback -- it now falls through as ordinary
        unmatched text. This fixture has no Conversation Engine
        configured, so the generic unrecognized-text reply is what
        surfaces here (see test_lock_task_conversation_boundary.py for
        the same case WITH a real engine configured, where it reaches
        the model instead)."""
        _complete_onboarding(service)
        result = service.handle_message(_incoming("lock frobnicate", external_message_id="m1"))
        assert "not a recognized `lock` command" not in result.text.lower()
        assert "i don't recognize that yet" in result.text.lower()

    def test_persists_across_a_reopened_application_service(self, core: CoreDatabase) -> None:
        service1 = ApplicationService(core.db_path, core=core)
        _complete_onboarding(service1)
        service1.handle_message(_incoming("lock report locked", external_message_id="m1"))

        service2 = ApplicationService(core.db_path, core=CoreDatabase(core.db_path))
        result = service2.handle_message(_incoming("lock status", external_user_id="42", external_message_id="m2"))
        assert "locked" in result.text.lower()

    def test_user_isolation(self, service: ApplicationService) -> None:
        _complete_onboarding(service, external_user_id="user-a")
        _complete_onboarding(service, external_user_id="user-b")
        service.handle_message(_incoming("lock report locked", external_user_id="user-a", external_message_id="ma1"))
        result_a = service.handle_message(_incoming("lock status", external_user_id="user-a", external_message_id="ma2"))
        result_b = service.handle_message(_incoming("lock status", external_user_id="user-b", external_message_id="mb1"))
        assert "locked" in result_a.text.lower()
        assert "haven't told me your lock status" in result_b.text.lower()


class TestTaskActiveWithoutAssignment:
    def test_no_active_task_message(self, service: ApplicationService) -> None:
        _complete_onboarding(service)
        result = service.handle_message(_incoming("task active", external_message_id="m1"))
        assert "no active task" in result.text.lower()


class TestTaskRequest:
    def test_request_with_no_eligible_template_at_all(self, service: ApplicationService) -> None:
        _complete_onboarding(service)
        result = service.handle_message(_incoming("task request", external_message_id="m1"))
        assert "no eligible task available" in result.text.lower()

    def test_request_assigns_a_no_lock_requirement_task(self, service: ApplicationService) -> None:
        _create_template(service, template_id="basic-chore", lock_requirement=LockRequirement.NONE)
        _complete_onboarding(service)
        result = service.handle_message(_incoming("task request", external_message_id="m1"))
        assert "assigned: test task" in result.text.lower()

    def test_request_skips_locked_requirement_task_when_lock_state_unknown(self, service: ApplicationService) -> None:
        _create_template(service, template_id="locked-chore", lock_requirement=LockRequirement.REQUIRES_LOCKED)
        _complete_onboarding(service)
        result = service.handle_message(_incoming("task request", external_message_id="m1"))
        assert "no eligible task available" in result.text.lower()

    def test_request_assigns_locked_requirement_task_once_locked(self, service: ApplicationService) -> None:
        _create_template(service, template_id="locked-chore", lock_requirement=LockRequirement.REQUIRES_LOCKED)
        _complete_onboarding(service)
        service.handle_message(_incoming("lock report locked", external_message_id="m1"))
        result = service.handle_message(_incoming("task request", external_message_id="m2"))
        assert "assigned: test task" in result.text.lower()

    def test_request_selection_is_deterministic_lowest_template_id(self, service: ApplicationService) -> None:
        _create_template(service, template_id="zzz-chore", lock_requirement=LockRequirement.NONE)
        _create_template(service, template_id="aaa-chore", lock_requirement=LockRequirement.NONE)
        _complete_onboarding(service)
        result = service.handle_message(_incoming("task request", external_message_id="m1"))
        # The "Assigned: {title}." reply no longer contains template_id
        # by design (Deterministic Task Command Human-Readable Content) --
        # both templates share the same "Test task" title via _create_template's
        # own fixed default, so *which* template was actually selected must be
        # verified against real domain state, not the reply text.
        assert "assigned: test task" in result.text.lower()
        user = service.user_service.get_or_create_user("discord", "42", now=FIXED_TIME)
        active = service.task_runtime.get_active_assignment(user.id)
        assert active is not None
        assert active.template_id == "aaa-chore"

    def test_request_with_existing_active_task_refuses(self, service: ApplicationService) -> None:
        _create_template(service, template_id="basic-chore", lock_requirement=LockRequirement.NONE)
        _complete_onboarding(service)
        service.handle_message(_incoming("task request", external_message_id="m1"))
        result = service.handle_message(_incoming("task request", external_message_id="m2"))
        assert "already have an active task" in result.text.lower()

    def test_bare_task_word_gets_family_reply(self, service: ApplicationService) -> None:
        """Command Family Fallback Precision: the bare family word
        alone still gets the family-specific reply."""
        _complete_onboarding(service)
        result = service.handle_message(_incoming("task", external_message_id="m1"))
        assert "your task" in result.text.lower() and "didn't catch that" in result.text.lower()

    def test_task_frobnicate_no_longer_gets_the_family_reply(self, service: ApplicationService) -> None:
        """Command Family Fallback Precision (Option A): see the
        matching "lock frobnicate" test above for the full rationale."""
        _complete_onboarding(service)
        result = service.handle_message(_incoming("task frobnicate", external_message_id="m1"))
        assert "not a recognized `task` command" not in result.text.lower()
        assert "i don't recognize that yet" in result.text.lower()


class TestTaskRequestHumanReadableContent:
    """Deterministic Task Command Human-Readable Content."""

    def test_success_reply_shows_title(self, service: ApplicationService) -> None:
        _create_template(service, template_id="basic-chore", lock_requirement=LockRequirement.NONE, title="Tidy one surface")
        _complete_onboarding(service)
        result = service.handle_message(_incoming("task request", external_message_id="m1"))
        assert result.text == "Assigned: Tidy one surface."

    def test_success_reply_with_null_title_shows_not_recorded(self, service: ApplicationService) -> None:
        _create_template_with_null_title(service, template_id="legacy-chore")
        _complete_onboarding(service)
        result = service.handle_message(_incoming("task request", external_message_id="m1"))
        assert result.text == "Assigned: (not recorded)."

    def test_success_reply_does_not_include_instructions_text(self, service: ApplicationService) -> None:
        _create_template(service, template_id="basic-chore", lock_requirement=LockRequirement.NONE, title="Tidy one surface")
        _complete_onboarding(service)
        result = service.handle_message(_incoming("task request", external_message_id="m1"))
        assert "Do the test task." not in result.text

    def test_already_active_branch_shows_title(self, service: ApplicationService) -> None:
        _create_template(service, template_id="basic-chore", lock_requirement=LockRequirement.NONE, title="Tidy one surface")
        _complete_onboarding(service)
        service.handle_message(_incoming("task request", external_message_id="m1"))
        result = service.handle_message(_incoming("task request", external_message_id="m2"))
        assert "You already have an active task: Tidy one surface." in result.text

    def test_already_active_branch_with_null_title_shows_not_recorded(self, service: ApplicationService) -> None:
        _create_template_with_null_title(service, template_id="legacy-chore")
        _complete_onboarding(service)
        service.handle_message(_incoming("task request", external_message_id="m1"))
        result = service.handle_message(_incoming("task request", external_message_id="m2"))
        assert "You already have an active task: (not recorded)." in result.text

    def test_already_active_branch_resolves_the_exact_pinned_historical_version(self, service: ApplicationService) -> None:
        from task_catalog.repository import TaskCatalogAdministration
        _create_template(service, template_id="versioned-chore", lock_requirement=LockRequirement.NONE, title="Original title")
        _complete_onboarding(service)
        service.handle_message(_incoming("task request", external_message_id="m1"))

        admin = TaskCatalogAdministration(service.db_path, core=service._core)
        admin.add_version(
            "versioned-chore", title="REVISED v2 title", instructions="Do the test task.",
            category="chore", difficulty="hard", effort="high", duration_minutes=30,
            required_equipment=(), required_privacy="none", required_context="home", safety_classification="safe",
            eligible_instance_roles=(TaskInstanceRole.PRIMARY,), eligible_operating_modes=("standard", "advanced"),
            completion_requirements={}, verification_requirements={}, reflection_requirements=None,
            lock_requirement=LockRequirement.NONE, created_via_consent_id="test-consent", now=FIXED_TIME,
        )
        admin.set_current_version("versioned-chore", version=2, via_consent_id="test-consent", now=FIXED_TIME)

        result = service.handle_message(_incoming("task request", external_message_id="m2"))
        assert "Original title" in result.text
        assert "REVISED v2 title" not in result.text


class TestTaskActiveWithAssignment:
    def test_shows_the_active_task(self, service: ApplicationService) -> None:
        _create_template(service, template_id="basic-chore", lock_requirement=LockRequirement.NONE)
        _complete_onboarding(service)
        service.handle_message(_incoming("task request", external_message_id="m1"))
        result = service.handle_message(_incoming("task active", external_message_id="m2"))
        assert "basic-chore" in result.text.lower()


class TestTaskActiveHumanReadableContent:
    """Deterministic Task Command Human-Readable Content."""

    def test_task_active_shows_title(self, service: ApplicationService) -> None:
        _create_template(service, template_id="basic-chore", lock_requirement=LockRequirement.NONE, title="Tidy one surface")
        _complete_onboarding(service)
        service.handle_message(_incoming("task request", external_message_id="m1"))
        result = service.handle_message(_incoming("task active", external_message_id="m2"))
        assert "Tidy one surface" in result.text
        # template ID and assigned timestamp remain present alongside the title.
        assert "basic-chore" in result.text
        assert "assigned" in result.text.lower()

    def test_task_active_with_null_title_shows_not_recorded(self, service: ApplicationService) -> None:
        _create_template_with_null_title(service, template_id="legacy-chore")
        _complete_onboarding(service)
        service.handle_message(_incoming("task request", external_message_id="m1"))
        result = service.handle_message(_incoming("task active", external_message_id="m2"))
        assert "(not recorded)" in result.text
        assert "legacy-chore" in result.text  # template ID still present
        assert "none" not in result.text.lower()  # never the literal Python None

    def test_task_active_does_not_include_instructions_text(self, service: ApplicationService) -> None:
        _create_template(service, template_id="basic-chore", lock_requirement=LockRequirement.NONE, title="Tidy one surface")
        _complete_onboarding(service)
        service.handle_message(_incoming("task request", external_message_id="m1"))
        result = service.handle_message(_incoming("task active", external_message_id="m2"))
        assert "Do the test task." not in result.text  # the fixed instructions text from _create_template

    def test_task_active_resolves_the_exact_pinned_historical_version(self, service: ApplicationService) -> None:
        """The assignment stays pinned to v1's title even after
        add_version()/set_current_version() advance the template to a
        different, current v2 -- same invariant already proven for
        ActiveTaskContextProvider, now also verified for the
        deterministic command path."""
        from task_catalog.repository import TaskCatalogAdministration
        _create_template(service, template_id="versioned-chore", lock_requirement=LockRequirement.NONE, title="Original title")
        _complete_onboarding(service)
        service.handle_message(_incoming("task request", external_message_id="m1"))

        admin = TaskCatalogAdministration(service.db_path, core=service._core)
        admin.add_version(
            "versioned-chore", title="REVISED v2 title", instructions="Do the test task.",
            category="chore", difficulty="hard", effort="high", duration_minutes=30,
            required_equipment=(), required_privacy="none", required_context="home", safety_classification="safe",
            eligible_instance_roles=(TaskInstanceRole.PRIMARY,), eligible_operating_modes=("standard", "advanced"),
            completion_requirements={}, verification_requirements={}, reflection_requirements=None,
            lock_requirement=LockRequirement.NONE, created_via_consent_id="test-consent", now=FIXED_TIME,
        )
        admin.set_current_version("versioned-chore", version=2, via_consent_id="test-consent", now=FIXED_TIME)

        result = service.handle_message(_incoming("task active", external_message_id="m2"))
        assert "Original title" in result.text
        assert "REVISED v2 title" not in result.text


class TestTaskComplete:
    def test_completes_the_active_task(self, service: ApplicationService) -> None:
        _create_template(service, template_id="basic-chore", lock_requirement=LockRequirement.NONE)
        _complete_onboarding(service)
        service.handle_message(_incoming("task request", external_message_id="m1"))
        result = service.handle_message(_incoming("task complete", external_message_id="m2"))
        assert result.text == "Completed: Test task."
        follow_up = service.handle_message(_incoming("task active", external_message_id="m3"))
        assert "no active task" in follow_up.text.lower()

    def test_complete_with_no_active_task(self, service: ApplicationService) -> None:
        _complete_onboarding(service)
        result = service.handle_message(_incoming("task complete", external_message_id="m1"))
        assert "no active task to resolve" in result.text.lower()

    def test_completing_twice_gives_a_safe_reply(self, service: ApplicationService) -> None:
        """The second 'task complete' finds no active task (the first
        one already resolved it) -- not a transition error, since
        get_active_assignment() correctly returns None by then."""
        _create_template(service, template_id="basic-chore", lock_requirement=LockRequirement.NONE)
        _complete_onboarding(service)
        service.handle_message(_incoming("task request", external_message_id="m1"))
        service.handle_message(_incoming("task complete", external_message_id="m2"))
        result = service.handle_message(_incoming("task complete", external_message_id="m3"))
        assert "no active task to resolve" in result.text.lower()


class TestTaskCompleteHumanReadableContent:
    """Title-aware task complete confirmation."""

    def test_complete_shows_title(self, service: ApplicationService) -> None:
        _create_template(service, template_id="basic-chore", lock_requirement=LockRequirement.NONE, title="Tidy one surface")
        _complete_onboarding(service)
        service.handle_message(_incoming("task request", external_message_id="m1"))
        result = service.handle_message(_incoming("task complete", external_message_id="m2"))
        assert result.text == "Completed: Tidy one surface."

    def test_complete_with_null_title_shows_not_recorded(self, service: ApplicationService) -> None:
        _create_template_with_null_title(service, template_id="legacy-chore")
        _complete_onboarding(service)
        service.handle_message(_incoming("task request", external_message_id="m1"))
        result = service.handle_message(_incoming("task complete", external_message_id="m2"))
        assert result.text == "Completed: (not recorded)."
        assert "none" not in result.text.lower()  # never the literal Python None

    def test_complete_does_not_include_instructions_text(self, service: ApplicationService) -> None:
        _create_template(service, template_id="basic-chore", lock_requirement=LockRequirement.NONE, title="Tidy one surface")
        _complete_onboarding(service)
        service.handle_message(_incoming("task request", external_message_id="m1"))
        result = service.handle_message(_incoming("task complete", external_message_id="m2"))
        assert "Do the test task." not in result.text  # the fixed instructions text from _create_template

    def test_complete_resolves_the_exact_pinned_historical_version(self, service: ApplicationService) -> None:
        """The confirmation uses the assignment's own pinned v1 title,
        even after add_version()/set_current_version() advance the
        template to a different, current v2 -- same invariant already
        proven for task active/task request."""
        from task_catalog.repository import TaskCatalogAdministration
        _create_template(service, template_id="versioned-chore", lock_requirement=LockRequirement.NONE, title="Original title")
        _complete_onboarding(service)
        service.handle_message(_incoming("task request", external_message_id="m1"))

        admin = TaskCatalogAdministration(service.db_path, core=service._core)
        admin.add_version(
            "versioned-chore", title="REVISED v2 title", instructions="Do the test task.",
            category="chore", difficulty="hard", effort="high", duration_minutes=30,
            required_equipment=(), required_privacy="none", required_context="home", safety_classification="safe",
            eligible_instance_roles=(TaskInstanceRole.PRIMARY,), eligible_operating_modes=("standard", "advanced"),
            completion_requirements={}, verification_requirements={}, reflection_requirements=None,
            lock_requirement=LockRequirement.NONE, created_via_consent_id="test-consent", now=FIXED_TIME,
        )
        admin.set_current_version("versioned-chore", version=2, via_consent_id="test-consent", now=FIXED_TIME)

        result = service.handle_message(_incoming("task complete", external_message_id="m2"))
        assert result.text == "Completed: Original title."
        assert "REVISED v2 title" not in result.text

    def test_complete_still_transitions_active_to_completed(self, service: ApplicationService) -> None:
        _create_template(service, template_id="basic-chore", lock_requirement=LockRequirement.NONE)
        _complete_onboarding(service)
        service.handle_message(_incoming("task request", external_message_id="m1"))
        user = service.user_service.get_or_create_user("discord", "42", now=FIXED_TIME)
        assignment_id = service.task_runtime.get_active_assignment(user.id).id
        service.handle_message(_incoming("task complete", external_message_id="m2"))
        with service._core.raw_connection() as conn:
            row = conn.execute("SELECT status FROM task_assignments WHERE id = ?", (assignment_id,)).fetchone()
        assert row["status"] == "completed"


class TestTaskCancel:
    def test_cancels_the_active_task(self, service: ApplicationService) -> None:
        _create_template(service, template_id="basic-chore", lock_requirement=LockRequirement.NONE)
        _complete_onboarding(service)
        service.handle_message(_incoming("task request", external_message_id="m1"))
        result = service.handle_message(_incoming("task cancel", external_message_id="m2"))
        assert result.text == "Cancelled: Test task."

    def test_cancel_with_no_active_task(self, service: ApplicationService) -> None:
        _complete_onboarding(service)
        result = service.handle_message(_incoming("task cancel", external_message_id="m1"))
        assert "no active task to resolve" in result.text.lower()

    def test_after_cancel_a_new_request_can_be_made(self, service: ApplicationService) -> None:
        _create_template(service, template_id="basic-chore", lock_requirement=LockRequirement.NONE)
        _complete_onboarding(service)
        service.handle_message(_incoming("task request", external_message_id="m1"))
        service.handle_message(_incoming("task cancel", external_message_id="m2"))
        result = service.handle_message(_incoming("task request", external_message_id="m3"))
        assert "assigned: test task" in result.text.lower()


class TestTaskCancelHumanReadableContent:
    """Title-aware task cancel confirmation."""

    def test_cancel_shows_title(self, service: ApplicationService) -> None:
        _create_template(service, template_id="basic-chore", lock_requirement=LockRequirement.NONE, title="Tidy one surface")
        _complete_onboarding(service)
        service.handle_message(_incoming("task request", external_message_id="m1"))
        result = service.handle_message(_incoming("task cancel", external_message_id="m2"))
        assert result.text == "Cancelled: Tidy one surface."

    def test_cancel_with_null_title_shows_not_recorded(self, service: ApplicationService) -> None:
        _create_template_with_null_title(service, template_id="legacy-chore")
        _complete_onboarding(service)
        service.handle_message(_incoming("task request", external_message_id="m1"))
        result = service.handle_message(_incoming("task cancel", external_message_id="m2"))
        assert result.text == "Cancelled: (not recorded)."
        assert "none" not in result.text.lower()  # never the literal Python None

    def test_cancel_does_not_include_instructions_text(self, service: ApplicationService) -> None:
        _create_template(service, template_id="basic-chore", lock_requirement=LockRequirement.NONE, title="Tidy one surface")
        _complete_onboarding(service)
        service.handle_message(_incoming("task request", external_message_id="m1"))
        result = service.handle_message(_incoming("task cancel", external_message_id="m2"))
        assert "Do the test task." not in result.text  # the fixed instructions text from _create_template

    def test_cancel_resolves_the_exact_pinned_historical_version(self, service: ApplicationService) -> None:
        """The confirmation uses the assignment's own pinned v1 title,
        even after add_version()/set_current_version() advance the
        template to a different, current v2."""
        from task_catalog.repository import TaskCatalogAdministration
        _create_template(service, template_id="versioned-chore", lock_requirement=LockRequirement.NONE, title="Original title")
        _complete_onboarding(service)
        service.handle_message(_incoming("task request", external_message_id="m1"))

        admin = TaskCatalogAdministration(service.db_path, core=service._core)
        admin.add_version(
            "versioned-chore", title="REVISED v2 title", instructions="Do the test task.",
            category="chore", difficulty="hard", effort="high", duration_minutes=30,
            required_equipment=(), required_privacy="none", required_context="home", safety_classification="safe",
            eligible_instance_roles=(TaskInstanceRole.PRIMARY,), eligible_operating_modes=("standard", "advanced"),
            completion_requirements={}, verification_requirements={}, reflection_requirements=None,
            lock_requirement=LockRequirement.NONE, created_via_consent_id="test-consent", now=FIXED_TIME,
        )
        admin.set_current_version("versioned-chore", version=2, via_consent_id="test-consent", now=FIXED_TIME)

        result = service.handle_message(_incoming("task cancel", external_message_id="m2"))
        assert result.text == "Cancelled: Original title."
        assert "REVISED v2 title" not in result.text

    def test_cancel_still_transitions_active_to_cancelled(self, service: ApplicationService) -> None:
        _create_template(service, template_id="basic-chore", lock_requirement=LockRequirement.NONE)
        _complete_onboarding(service)
        service.handle_message(_incoming("task request", external_message_id="m1"))
        user = service.user_service.get_or_create_user("discord", "42", now=FIXED_TIME)
        assignment_id = service.task_runtime.get_active_assignment(user.id).id
        service.handle_message(_incoming("task cancel", external_message_id="m2"))
        with service._core.raw_connection() as conn:
            row = conn.execute("SELECT status FROM task_assignments WHERE id = ?", (assignment_id,)).fetchone()
        assert row["status"] == "cancelled"


class TestPersistenceAndIsolation:
    def test_task_assignment_persists_across_a_reopened_application_service(self, core: CoreDatabase) -> None:
        service1 = ApplicationService(core.db_path, core=core)
        _create_template(service1, template_id="basic-chore", lock_requirement=LockRequirement.NONE)
        _complete_onboarding(service1)
        service1.handle_message(_incoming("task request", external_message_id="m1"))

        service2 = ApplicationService(core.db_path, core=CoreDatabase(core.db_path))
        result = service2.handle_message(_incoming("task active", external_user_id="42", external_message_id="m2"))
        assert "basic-chore" in result.text.lower()

    def test_task_user_isolation(self, service: ApplicationService) -> None:
        _create_template(service, template_id="basic-chore", lock_requirement=LockRequirement.NONE)
        _complete_onboarding(service, external_user_id="user-a")
        _complete_onboarding(service, external_user_id="user-b")
        service.handle_message(_incoming("task request", external_user_id="user-a", external_message_id="ma1"))
        result_a = service.handle_message(_incoming("task active", external_user_id="user-a", external_message_id="ma2"))
        result_b = service.handle_message(_incoming("task active", external_user_id="user-b", external_message_id="mb1"))
        assert "basic-chore" in result_a.text.lower()
        assert "no active task" in result_b.text.lower()
