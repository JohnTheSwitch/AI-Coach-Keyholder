"""
tests/conversation_engine/test_system_independence.py

CE-20. Per explicit review instruction: does NOT run the existing
pytest suite from inside a pytest test (that's the final, separate
regression-suite step of the implementation, not something this file
does). Instead:

1. Statically parses every domain module / application / bot source
   file's own AST and confirms none of them import `conversation_engine`.
2. Constructs a real ApplicationService the ordinary way and directly
   exercises a few existing command paths, proving their behavior is
   unaffected by conversation_engine's mere existence on disk.
"""

from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path

import pytest

from application.models import IncomingMessage
from application.service import ApplicationService
from infrastructure.database import Database as CoreDatabase

FIXED_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)
PROJECT_ROOT = Path(__file__).parent.parent.parent

# Every first-party package this project's own domain/application/bot
# layer consists of, excluding conversation_engine itself and test
# directories -- exactly the set CE-20 requires to have zero import of
# conversation_engine.
_CHECKED_PACKAGES = [
    "trust_manager", "penalty_engine", "recovery_plan",
    "goal_management", "task_catalog", "advanced_mode", "infrastructure", "ai",
]
# application/ and bot/ are each checked separately below: service.py
# (Slice 2's own ApplicationService integration point) and
# discord_bot.py (the composition root, per the approved plan's own
# point 14) are EXPECTED to import conversation_engine now -- every
# other file in either package must still not.
_APPLICATION_FILES_ALLOWED_TO_IMPORT = {"service.py"}
_BOT_FILES_ALLOWED_TO_IMPORT = {"discord_bot.py"}


def _imports_conversation_engine(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name == "conversation_engine" or alias.name.startswith("conversation_engine.") for alias in node.names):
                return True
        if isinstance(node, ast.ImportFrom):
            if node.module and (node.module == "conversation_engine" or node.module.startswith("conversation_engine.")):
                return True
    return False


class TestNoExistingPackageImportsConversationEngine:
    def test_static_ast_scan(self) -> None:
        offending: list[str] = []
        for package in _CHECKED_PACKAGES:
            package_dir = PROJECT_ROOT / package
            if not package_dir.is_dir():
                continue
            for py_file in package_dir.rglob("*.py"):
                if _imports_conversation_engine(py_file):
                    offending.append(str(py_file.relative_to(PROJECT_ROOT)))
        assert offending == [], f"Found conversation_engine import(s) in files that must not have one: {offending}"

    def test_application_package_only_service_py_imports_it(self) -> None:
        """Slice 2's own approved integration point -- service.py is
        EXPECTED to import conversation_engine now; every other file in
        application/ (router.py, models.py, onboarding_service.py,
        user_service.py) must still not."""
        offending: list[str] = []
        application_dir = PROJECT_ROOT / "application"
        for py_file in application_dir.rglob("*.py"):
            if py_file.name in _APPLICATION_FILES_ALLOWED_TO_IMPORT:
                continue
            if _imports_conversation_engine(py_file):
                offending.append(str(py_file.relative_to(PROJECT_ROOT)))
        assert offending == []

    def test_service_py_does_in_fact_import_it(self) -> None:
        """Positive proof the approved integration actually exists --
        not just that nothing ELSE imports it."""
        service_py = PROJECT_ROOT / "application" / "service.py"
        assert _imports_conversation_engine(service_py)

    def test_bot_package_only_discord_bot_py_imports_it(self) -> None:
        """The composition root (bot/discord_bot.py's own main()) is
        EXPECTED to import conversation_engine to construct
        OllamaConversationModel/the buffer/the queue/the engine -- no
        other file in bot/ should."""
        offending: list[str] = []
        bot_dir = PROJECT_ROOT / "bot"
        for py_file in bot_dir.rglob("*.py"):
            if py_file.name in _BOT_FILES_ALLOWED_TO_IMPORT:
                continue
            if _imports_conversation_engine(py_file):
                offending.append(str(py_file.relative_to(PROJECT_ROOT)))
        assert offending == []

    def test_discord_bot_py_does_in_fact_import_it(self) -> None:
        discord_bot_py = PROJECT_ROOT / "bot" / "discord_bot.py"
        assert _imports_conversation_engine(discord_bot_py)


class TestExistingCommandBehaviorUnaffected:
    """Constructs a real ApplicationService WITHOUT injecting a
    conversation_engine (the default, conversation_engine=None) and
    exercises a few known command paths directly -- proves their
    behavior is unchanged by Slice 2's own integration, which only
    activates for unmatched text and only when an engine is actually
    injected."""

    @pytest.fixture
    def service(self, tmp_path: Path) -> ApplicationService:
        core = CoreDatabase(tmp_path / "test.db")
        migrations_dir = PROJECT_ROOT / "database" / "migrations"
        with core.raw_connection() as conn:
            for path in sorted(migrations_dir.glob("*.sql")):
                conn.executescript(path.read_text(encoding="utf-8"))
        return ApplicationService(core.db_path, core=core)

    def _onboard(self, service: ApplicationService, external_user_id: str = "1") -> None:
        for text in ("anything", "english", "neutral", "alex"):
            service.handle_message(
                IncomingMessage(channel="discord", external_user_id=external_user_id, text=text, received_at=FIXED_TIME),
            )

    def test_help_command_unchanged(self, service: ApplicationService) -> None:
        self._onboard(service)
        result = service.handle_message(
            IncomingMessage(channel="discord", external_user_id="1", text="help", received_at=FIXED_TIME),
        )
        assert "just talk to me" in result.text

    def test_status_command_unchanged(self, service: ApplicationService) -> None:
        self._onboard(service)
        result = service.handle_message(
            IncomingMessage(channel="discord", external_user_id="1", text="status", received_at=FIXED_TIME),
        )
        assert isinstance(result.text, str) and result.text

    def test_mode_status_command_unchanged(self, service: ApplicationService) -> None:
        self._onboard(service)
        result = service.handle_message(
            IncomingMessage(channel="discord", external_user_id="1", text="mode status", received_at=FIXED_TIME),
        )
        assert "current mode: standard" in result.text.lower()

    def test_unrecognized_text_unchanged(self, service: ApplicationService) -> None:
        self._onboard(service)
        result = service.handle_message(
            IncomingMessage(channel="discord", external_user_id="1", text="asdkfjhasdkjfh", received_at=FIXED_TIME),
        )
        assert "don't recognize" in result.text.lower()
