"""ApplicationService -> check-in listener (v0.39 Phase 2): which messages count as a check-in reply."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from application.service import ApplicationService
from infrastructure.database import Database as CoreDatabase
from supervision.checkin_evaluator import Verdict
from supervision.checkins import CheckInService, SendOutcome
from schedule_windows.windows import WindowConfig
from tests.application.test_chaster_llm_tool_integration import (  # noqa: F401
    NOW, ToolModel, _apply_migrations, _incoming, _onboard,
)

import logging


@pytest.fixture
def core(tmp_path: Path) -> CoreDatabase:
    c = CoreDatabase(tmp_path / "test.db")
    _apply_migrations(c)
    return c


def build(core, tmp_path, listener):
    from chaster.oauth_client import ChasterOAuthClient
    from chaster.repository import ChasterConnectionRepository
    from chaster.token_encryptor import TokenEncryptor
    from chaster.token_manager import ChasterTokenManager
    from conversation_engine.engine import ConversationEngine
    from conversation_engine.subject_queue import SubjectConversationQueue
    from memory_system.working_memory import InMemoryWorkingMemory

    model = ToolModel()
    connections = ChasterConnectionRepository(tmp_path / "test.db", core=core, encryptor=TokenEncryptor(Fernet.generate_key()))
    oauth = ChasterOAuthClient(client_id="cid", client_secret="secret", redirect_uri="https://example.com/cb")
    manager = ChasterTokenManager(connections=connections, oauth_client=oauth)
    memory = InMemoryWorkingMemory(max_exchanges_per_subject=10, max_characters_per_subject=8000)
    engine = ConversationEngine(model=model, working_memory_reader=memory, working_memory_writer=memory,
                                queue=SubjectConversationQueue())
    service = ApplicationService(
        core.db_path, core=core, conversation_engine=engine, chaster_connections=connections,
        chaster_oauth_client=oauth, chaster_token_manager=manager, check_in_listener=listener,
    )
    _onboard(service)
    return service


class TestListener:
    def test_free_text_is_forwarded_with_the_internal_user_id(self, core, tmp_path) -> None:
        calls = []
        service = build(core, tmp_path, lambda *a: calls.append(a))
        reply = service.handle_message(_incoming("I trained for an hour today", "m1"))
        user = service.user_service.get_or_create_user("discord", "42", now=NOW)
        assert calls == [(user.id, "I trained for an hour today", NOW)] and reply.text

    def test_commands_are_never_a_check_in_reply(self, core, tmp_path) -> None:
        calls = []
        service = build(core, tmp_path, lambda *a: calls.append(a))
        service.handle_message(_incoming("help", "m1"))
        service.handle_message(_incoming("preferences", "m2"))
        assert calls == []

    def test_chaster_status_questions_are_not_a_check_in_reply(self, core, tmp_path) -> None:
        calls = []
        service = build(core, tmp_path, lambda *a: calls.append(a))
        service.handle_message(_incoming("How much time is left on my lock?", "m1"))
        assert calls == []

    def test_onboarding_messages_are_not_forwarded(self, core, tmp_path) -> None:
        calls = []
        service = ApplicationService(core.db_path, core=core, check_in_listener=lambda *a: calls.append(a))
        service.handle_message(_incoming("first message", "o1"))
        service.handle_message(_incoming("english", "o2"))
        assert calls == []

    def test_a_failing_listener_never_breaks_the_conversation(self, core, tmp_path, caplog) -> None:
        def boom(*a):
            raise RuntimeError("listener bug")
        service = build(core, tmp_path, boom)
        with caplog.at_level(logging.ERROR):
            reply = service.handle_message(_incoming("a normal message", "m1"))
        assert reply.text == "plain conversational reply" and "Check-in listener failed" in caplog.text

    def test_no_listener_is_fine(self, core, tmp_path) -> None:
        service = build(core, tmp_path, None)
        assert service.handle_message(_incoming("a normal message", "m1")).text == "plain conversational reply"


class Sender:
    def send(self, *, user_id, text):
        return SendOutcome.DELIVERED


class TestEndToEndWithTheRealService:
    def test_a_conversation_message_answers_the_open_check_in(self, core, tmp_path) -> None:
        cfg = WindowConfig.build(timezone_name="Europe/Prague", quiet_start="22:00", quiet_end="07:00",
                                 work_start="07:00", work_end="16:30")
        holder = {}
        service = build(core, tmp_path, lambda uid, text, now: holder["svc"].on_user_message(uid, text, now))
        user = service.user_service.get_or_create_user("discord", "42", now=NOW)
        holder["svc"] = CheckInService(
            core.db_path, core=core, window_config=cfg, user_id=user.id, trust_domain="d", get_trust_score=lambda: 0.5,
            sender=Sender(), evaluate=lambda p, r: Verdict.SUFFICIENT,
        )
        from infrastructure.time_format import iso
        with core.transaction() as tx:
            tx.execute(
                "INSERT INTO check_ins (id, user_id, local_date, slot_index, level, due_at, status, prompt_text, sent_at, "
                "deadline_at, created_at, updated_at) VALUES ('c1', ?, '2026-01-01', 0, 2, ?, 'sent', 'How did it go?', ?, ?, ?, ?)",
                (user.id, iso(NOW - timedelta(minutes=5)), iso(NOW - timedelta(minutes=5)), iso(NOW + timedelta(minutes=55)),
                 iso(NOW), iso(NOW)),
            )
        service.handle_message(_incoming("I finished my workout and tidied up", "m1"))
        with core.transaction() as tx:
            row = tx.fetch_one("SELECT status, response_text FROM check_ins WHERE id = 'c1'")
        assert row["status"] == "responded" and row["response_text"] == "I finished my workout and tidied up"
        holder["svc"].tick(NOW + timedelta(minutes=1))
        with core.transaction() as tx:
            assert tx.fetch_one("SELECT status FROM check_ins WHERE id = 'c1'")["status"] == "answered"


class TestIndependence:
    def test_application_still_imports_nothing_from_supervision(self) -> None:
        import ast
        tree = ast.parse((Path(__file__).parent.parent.parent / "application" / "service.py").read_text(encoding="utf-8"))
        names = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.append(node.module)
        assert not [n for n in names if n.split(".")[0] in ("supervision", "release_engine", "schedule_windows")]
