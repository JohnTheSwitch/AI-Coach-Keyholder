"""Zero-Command UX: natural questions for read-only commands, the natural `help`, and no internal metric or machine command in anything a user reads. No network."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from application.natural_queries import CAPABILITIES_TEXT, READ_ONLY_TARGETS, read_only_command
from application.service import ApplicationService
from tests.application.test_lock_and_task_commands import FIXED_TIME, _complete_onboarding, _incoming, core, service  # noqa: F401

ROOT = Path(__file__).parent.parent.parent


class TestMatching:
    @pytest.mark.parametrize("text,command", [
        ("what's my task?", "task active"), ("What is my current task", "task active"), ("do I have a task today?", "task active"), ("jaký mám úkol?", "task active"),
        ("am I on vacation?", "vacation"), ("Am I on a vacation", "vacation"), ("mám dovolenou?", "vacation"),
        ("how long is left on my penalty window?", "status"), ("am I in a penalty window?", "status"),
        ("what do you remember about me?", "memory"), ("what are you tracking about me?", "telemetry"),
        ("what mode am I in?", "mode"), ("what did I report?", "lock status"), ("what can you do?", "help"), ("co umíš?", "help"),
        ("where do you send your messages?", "channel status"), ("show me my preferences", "preferences"),
    ])
    def test_a_whole_question_matches(self, text, command) -> None:
        assert read_only_command(text) == command

    @pytest.mark.parametrize("text", [
        "my task was really hard today", "what's my task supposed to teach me about patience?", "I wish I were on vacation", "hello", "", "   ", None,
        "am i on vacation " + "x" * 200, "forget all", "yes", "vacation end", "task complete", "task cancel",
    ])
    def test_everything_else_stays_conversation(self, text) -> None:
        assert read_only_command(text) is None

    def test_every_target_is_read_only(self) -> None:
        """What changes something keeps an exact phrase or a button, because it must never be guessed."""
        allowed = {"help", "task active", "vacation", "status", "memory", "telemetry", "mode", "lock status", "channel status", "preferences"}
        assert READ_ONLY_TARGETS <= allowed


class TestThroughTheApplication:
    def test_the_answer_comes_from_the_handler_not_a_model(self, service: ApplicationService) -> None:
        _complete_onboarding(service)
        assert service.handle_message(_incoming("am I on vacation?", external_message_id="m1")).text == service.handle_message(_incoming("vacation", external_message_id="m2")).text

    def test_help_describes_and_lists_no_commands(self, service: ApplicationService) -> None:
        _complete_onboarding(service)
        text = service.handle_message(_incoming("help", external_message_id="m1")).text
        assert text == CAPABILITIES_TEXT and "commands" not in text.lower() and "`" not in text

    def test_the_exact_phrases_keep_working(self, service: ApplicationService) -> None:
        """Retired from view, not from use: nobody's habit breaks."""
        _complete_onboarding(service)
        assert "Recorded" in service.handle_message(_incoming("lock report locked", external_message_id="m1")).text


class TestLockReportButtons:
    def test_lock_status_offers_the_two_buttons(self, service: ApplicationService) -> None:
        _complete_onboarding(service)
        reply = service.handle_message(_incoming("lock status", external_message_id="m1"))
        assert [a.label for a in reply.actions] == ["I'm locked", "I'm unlocked"]

    def test_every_card_has_its_own_reference(self, service: ApplicationService) -> None:
        _complete_onboarding(service)
        first = service.handle_message(_incoming("lock status", external_message_id="m1")).actions[0].action_id
        second = service.handle_message(_incoming("lock status", external_message_id="m2")).actions[0].action_id
        assert first != second

    def test_a_press_is_the_same_report_as_the_phrase(self, core) -> None:  # noqa: F811
        from infrastructure.clock import FrozenClock
        from lock_state.models import LockKnowledgeState
        service = ApplicationService(core.db_path, core=core, clock=FrozenClock(FIXED_TIME))
        _complete_onboarding(service)
        with core.transaction() as tx:
            row = tx.fetch_one("SELECT user_account_id, external_id FROM user_channel_identities WHERE channel = 'discord'")
        ident = "12345678-1234-1234-1234-123456789012"
        assert service.press(discord_user_id=row["external_id"], action="locked", ident=ident).text == "Recorded: you reported yourself as locked."
        assert service.lock_state.get_current_knowledge_state(row["user_account_id"]) is LockKnowledgeState.LOCKED_USER_REPORTED

    def test_a_stranger_reports_nothing(self, core) -> None:  # noqa: F811
        from infrastructure.clock import FrozenClock
        service = ApplicationService(core.db_path, core=core, clock=FrozenClock(FIXED_TIME))
        assert service.press(discord_user_id="999", action="locked", ident="x").text == "This isn't yours to answer."


def _user_strings(path: Path) -> list[str]:
    """Every string a module defines, without docstrings."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {id(n.body[0].value) for n in ast.walk(tree) if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                  and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
    return [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings]


class TestNothingInternalReachesADm:
    NEW_DM_MODULES = ("notifications/queue.py", "system/penalty_notices.py", "system/dm_actions.py", "exemptions/service.py", "infractions/hygiene.py", "infractions/lock_claims.py",
                      "application/natural_queries.py")

    def test_no_machine_command_in_the_notices(self) -> None:
        for name in self.NEW_DM_MODULES + ("peer_review/notices.py",):
            assert not any("python -m" in s for s in _user_strings(ROOT / name)), name

    @pytest.mark.parametrize("name", ["system/penalty_notices.py", "exemptions/service.py", "infractions/hygiene.py", "infractions/lock_claims.py", "application/natural_queries.py"])
    def test_no_new_text_names_trust(self, name) -> None:
        for text in _user_strings(ROOT / name):
            if " " in text and not text.startswith(("SELECT", "UPDATE", "INSERT", "DELETE")) and "trust_domain" not in text:
                assert not re.search(r"\b(trust|score)\b", text, re.IGNORECASE), (name, text)

    def test_no_public_commands_exist(self) -> None:
        """DM only: no slash command tree and no prefix command framework."""
        for path in (ROOT / "bot").glob("*.py"):
            source = path.read_text(encoding="utf-8")
            assert "app_commands" not in source and "commands.Bot" not in source and "CommandTree" not in source, path.name
