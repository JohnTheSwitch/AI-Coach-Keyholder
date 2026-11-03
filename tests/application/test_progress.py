"""Phase J1 (Zero-Command UX): natural "time left" and "progress" questions are answered deterministically, never with the Trust score."""

from __future__ import annotations

import ast
from datetime import timedelta
from pathlib import Path

import pytest

from application.progress import ProgressSnapshot, ReviewCounts, is_progress_query, render_progress
from application.service import ApplicationService
from chaster.llm_tools import is_chaster_status_query
from tests.application.test_lock_and_task_commands import (FIXED_TIME, _complete_onboarding, _create_template, _incoming,  # noqa: F401
                                                           core)
from task_catalog.models import LockRequirement

ROOT = Path(__file__).parent.parent.parent


class TestTimeLeftQuestions:
    @pytest.mark.parametrize("message", [
        "How much time is left?", "how much time remaining?", "How long until I'm out?", "how long still?",
        "Kolik mi zbývá?", "Jak dlouho ještě?", "jak dlouho mi zbývá?", "Kolik času zbývá?",
    ])
    def test_matched_without_naming_the_lock(self, message) -> None:
        assert is_chaster_status_query(message) is True

    @pytest.mark.parametrize("message", [
        "How much time is left on my task?", "how long is the meeting?", "Kolik zbývá do konce úkolu?",
        "How much time is left in the movie?", "how much time do you have?", "How long?", "time left",
        "Kolik mi zbývá do dovolené?", "how long until the timer ends?", "how much time is left " + "x" * 80 + "?",
    ])
    def test_other_things_that_run_out_are_not_matched(self, message) -> None:
        assert is_chaster_status_query(message) is False


class TestProgressMatcher:
    @pytest.mark.parametrize("message", [
        "Show my progress", "show my progress please", "my progress?", "How am I doing?", "how am i doing",
        "Jak mi to jde?", "Jak se mi daří?", "Můj pokrok", "ukaž můj přehled", "show me my stats",
    ])
    def test_matched(self, message) -> None:
        assert is_progress_query(message) is True

    @pytest.mark.parametrize("message", [
        "How are you doing?", "progress is slow at work today, any tips on focus?", "Jak se máš?", "help", "", "   ",
        "the progress bar is stuck", "x" * 200, None, 5,
    ])
    def test_not_matched(self, message) -> None:
        assert is_progress_query(message) is False


class TestRender:
    def test_owner_view_never_contains_trust(self) -> None:
        text = render_progress(ProgressSnapshot(active_task_title="Stretch", tasks_completed=1,
                                                reviews=ReviewCounts(verified=3, rejected=1, ongoing=2, awaiting_decision=1),
                                                penalty_running=False))
        assert 'one active ("Stretch"); 1 task completed' in text
        assert "3 verified, 1 rejected, 2 still being voted on" in text and "1 rejection still waits for the keyholder's decision" in text
        assert "No penalty window is running." in text
        assert "trust" not in text.lower() and "score" not in text.lower()

    def test_minimal_view(self) -> None:
        text = render_progress(ProgressSnapshot(active_task_title=None, tasks_completed=0))
        assert text.splitlines() == ["Here is how you are doing (last 7 days):", "- Tasks: none active right now; 0 tasks completed."]


def _review(core, review_id, status, *, baseline=0, incident=False, decided=None, at=FIXED_TIME):
    stamp = at.isoformat().replace("+00:00", "Z")
    with core.raw_connection() as conn:
        conn.execute("INSERT INTO peer_reviews (lock_id, review_id, source, status, baseline, first_seen_at, updated_at, incident_id, "
                     "incident_registered_at, owner_decision) VALUES ('lock1', ?, 'tasks', ?, ?, ?, ?, ?, ?, ?)",
                     (review_id, status, baseline, stamp, stamp, f"inc-{review_id}" if incident else None, stamp if incident else None, decided))
        conn.commit()


class TestThroughTheService:
    def _owner_service(self, core) -> tuple[ApplicationService, str]:
        plain = ApplicationService(core.db_path, core=core)
        _complete_onboarding(plain)
        user = plain.user_service.get_or_create_user("discord", "42", now=FIXED_TIME)
        return ApplicationService(core.db_path, core=core, owner_user_id=user.id), user.id

    def test_the_owner_sees_tasks_reviews_and_the_penalty_window(self, core) -> None:
        service, _ = self._owner_service(core)
        _create_template(service, template_id="t1", lock_requirement=LockRequirement.NONE, title="Stretch")
        service.handle_message(_incoming("task request", external_message_id="m1"))
        _review(core, "a", "verified")
        _review(core, "b", "rejected", incident=True)
        _review(core, "c", "ongoing")
        _review(core, "old", "rejected", baseline=1)                         # ended before the watch: not counted
        _review(core, "d", "verified", at=FIXED_TIME - timedelta(days=30))   # outside the window
        reply = service.handle_message(_incoming("How am I doing?", now=FIXED_TIME + timedelta(hours=1), external_message_id="m2"))
        assert 'one active ("Stretch")' in reply.text
        assert "1 verified, 1 rejected, 1 still being voted on" in reply.text and "1 rejection still waits" in reply.text
        assert "No penalty window is running." in reply.text and "trust" not in reply.text.lower()

    def test_another_user_never_sees_the_owners_reviews(self, core) -> None:
        service, _ = self._owner_service(core)
        _review(core, "b", "rejected", incident=True)
        _complete_onboarding(service, external_user_id="77")
        reply = service.handle_message(_incoming("show my progress", external_user_id="77", external_message_id="x1"))
        assert reply.text.startswith("Here is how you are doing") and "Community" not in reply.text and "penalty" not in reply.text

    def test_without_an_owner_configured_nobody_sees_reviews(self, core) -> None:
        service = ApplicationService(core.db_path, core=core)
        _complete_onboarding(service)
        _review(core, "b", "rejected", incident=True)
        reply = service.handle_message(_incoming("Jak mi to jde?", external_message_id="m1"))
        assert "Community" not in reply.text

    def test_a_completed_task_counts(self, core) -> None:
        service, _ = self._owner_service(core)
        _create_template(service, template_id="t1", lock_requirement=LockRequirement.NONE, title="Stretch")
        service.handle_message(_incoming("task request", external_message_id="m1"))
        service.handle_message(_incoming("task complete", external_message_id="m2"))
        reply = service.handle_message(_incoming("my progress", now=FIXED_TIME + timedelta(hours=1), external_message_id="m3"))
        assert "none active right now; 1 task completed" in reply.text


def test_the_progress_module_reads_no_trust_state() -> None:
    tree = ast.parse((ROOT / "application" / "progress.py").read_text(encoding="utf-8"))
    names = [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names] + \
            [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module]
    assert not [n for n in names if "trust" in n]
    text = (ROOT / "application" / "progress.py").read_text(encoding="utf-8")
    assert "trust_domain_state" not in text and "get_domain_state" not in text
