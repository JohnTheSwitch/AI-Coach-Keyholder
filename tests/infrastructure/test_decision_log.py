"""Owner, 2026-11-10: anyone with the files can follow what was decided and why; release decisions have their own log. No network."""

from __future__ import annotations

import logging
from datetime import timedelta

import pytest

from infrastructure import decision_log
from infrastructure.consumer_registry import ConsumerRegistry, process_pending_events
from infrastructure.decision_log import DECISIONS_LOGGER, RELEASE_LOGGER, decision, install_decision_files, log_event, release_decision
from infrastructure.outbox import DomainEvent, write_event
from tests.infrastructure.test_consumer_registry import FIXED_TIME, db  # noqa: F401
from tests.release_engine.test_service import OK, U, core, make, ready  # noqa: F401


@pytest.fixture
def files(tmp_path):
    install_decision_files(tmp_path / "logs")
    yield tmp_path / "logs"
    for name in (DECISIONS_LOGGER, RELEASE_LOGGER):
        logger = logging.getLogger(name)
        for h in list(logger.handlers):
            logger.removeHandler(h)
            h.close()


def _read(path):
    return path.read_text(encoding="utf-8") if path.exists() else ""


class TestFiles:
    def test_a_decision_goes_to_decisions_log_only(self, files) -> None:
        decision("infractions", "Judge verdict on the explanation: explained.", reason="was at the doctor")
        assert "Judge verdict" in _read(files / "decisions.log") and "was at the doctor" in _read(files / "decisions.log")
        assert _read(files / "release.log") == ""

    def test_a_release_decision_goes_to_both(self, files) -> None:
        release_decision("Release planned.", planned_unfreeze_at="2026-11-11T17:05:00Z")
        assert "2026-11-11T17:05:00Z" in _read(files / "release.log") and "Release planned" in _read(files / "decisions.log")

    def test_installing_twice_writes_each_line_once(self, files) -> None:
        install_decision_files(files)
        decision("x", "once")
        assert _read(files / "decisions.log").count("once") == 1

    def test_trust_scores_are_never_written(self, files) -> None:
        log_event("trust_domain.recalculated", "trust_manager", {"domain": "chores", "previous_score": 61.5, "new_score": 58.0})
        text = _read(files / "decisions.log")
        assert "trust_domain.recalculated" in text and "61.5" not in text and "58.0" not in text and "[hidden]" in text


class TestWhatIsLogged:
    def test_every_published_event_is_logged(self, db, caplog) -> None:  # noqa: F811
        with db.transaction() as tx:
            write_event(tx, DomainEvent(event_type="incident.reported", source_module="trust_manager", payload={"incident_id": "i1"},
                                        occurred_at=FIXED_TIME))
        with caplog.at_level(logging.INFO, logger=DECISIONS_LOGGER):
            process_pending_events(db, ConsumerRegistry(), claimant="t", now=FIXED_TIME)
        assert any("incident.reported" in r.getMessage() and "i1" in r.getMessage() for r in caplog.records)

    def test_an_event_rolled_back_is_never_logged(self, db, caplog) -> None:  # noqa: F811
        with caplog.at_level(logging.INFO, logger=DECISIONS_LOGGER):
            with pytest.raises(RuntimeError):
                with db.transaction() as tx:
                    write_event(tx, DomainEvent(event_type="incident.reported", source_module="t", payload={}, occurred_at=FIXED_TIME))
                    raise RuntimeError
            process_pending_events(db, ConsumerRegistry(), claimant="t", now=FIXED_TIME)
        assert not any("incident.reported" in r.getMessage() for r in caplog.records)

    def test_the_release_plan_and_its_time_reach_the_release_log(self, core, caplog) -> None:  # noqa: F811
        eng = make(core)
        with caplog.at_level(logging.INFO, logger=RELEASE_LOGGER):
            _, target = ready(core, eng)
            eng.commit_authorization(user_id=U, gates=OK, now=target - timedelta(minutes=1))    # not due: nothing to say
            eng.commit_authorization(user_id=U, gates=OK, now=target)
        lines = [r.getMessage() for r in caplog.records if r.name == RELEASE_LOGGER]
        assert any("Release planned" in m and target.strftime("%Y-%m-%dT%H:%M") in m for m in lines)
        assert any("Release commit: authorized" in m for m in lines) and not any("not_due" in m for m in lines)

    def test_a_broken_log_never_stops_a_decision(self, monkeypatch) -> None:
        def boom(*_a, **_k):
            raise OSError("disk full")

        monkeypatch.setattr(decision_log._decisions, "info", boom)
        decision("x", "still fine")
        log_event("a.b", "c", {})


class TestReleaseTick:
    def test_each_change_of_outcome_is_logged_once_with_its_reasons(self, tmp_path, caplog) -> None:
        from tests.system.test_release_tick import World
        w = World(tmp_path)
        w.tokens()
        with caplog.at_level(logging.INFO, logger=RELEASE_LOGGER):
            w.cycle()
            w.control.set_halted(True, reason="test", now=w.now)
            for minutes in (0, 1, 2):
                w.cycle(w.target() + timedelta(minutes=minutes))
        ticks = [r.getMessage() for r in caplog.records if "Release tick" in r.getMessage()]
        assert any("plan_created" in m for m in ticks)
        halted = [m for m in ticks if "plan_due_but_halted" in m]
        assert len(halted) == 1 and '"halted": true' in halted[0] and '"token_balance": 1' in halted[0]


class TestModelLog:
    def test_the_whole_prompt_and_the_answer_are_written(self, files) -> None:
        from infrastructure.decision_log import model_call
        model_call(model="m", messages=[{"role": "system", "content": "Be blunt."}, {"role": "user", "content": "hi"}], tools=[],
                   structured=False, answer={"content": "Report in."}, error=None, seconds=1.2)
        text = _read(files / "model.log")
        assert "Be blunt." in text and "--- answer" in text and "Report in." in text
        assert "Be blunt." not in _read(files / "decisions.log")

    def test_the_adapter_logs_each_exchange_and_each_failure(self, monkeypatch, caplog) -> None:
        import requests

        from conversation_engine.model_types import ModelGenerationRequest, ModelMessage, ModelMessageRole as ModelRole
        from conversation_engine.ollama_adapter import LLMGenerationError, OllamaConversationModel
        from infrastructure.decision_log import MODEL_LOGGER
        logger = logging.getLogger(MODEL_LOGGER)
        logger.propagate = True
        try:
            model = OllamaConversationModel.__new__(OllamaConversationModel)
            model._model_name = "m"
            monkeypatch.setattr(model, "_post_chat_once", lambda **kw: {"message": {"content": "Fine."}}, raising=False)
            request = ModelGenerationRequest(messages=(ModelMessage(role=ModelRole.USER, content="hello"),), max_output_characters=50)
            with caplog.at_level(logging.INFO, logger=MODEL_LOGGER):
                model._post_chat(request=request, tools=())

                def fail(**kw):
                    raise LLMGenerationError("ollama_timeout")

                monkeypatch.setattr(model, "_post_chat_once", fail, raising=False)
                with pytest.raises(LLMGenerationError):
                    model._post_chat(request=request, tools=())
            text = "\n".join(r.getMessage() for r in caplog.records)
            assert "hello" in text and "Fine." in text and "ERROR=ollama_timeout" in text
        finally:
            logger.propagate = False
        _ = requests


class TestWhyNothingHappens:
    def test_a_token_held_by_a_veto_says_so_once(self, tmp_path, caplog) -> None:
        from tests.system.test_release_tick import World
        w = World(tmp_path, supervised=True)
        w.tokens()
        with caplog.at_level(logging.INFO, logger=RELEASE_LOGGER):
            for minutes in (0, 1, 2):
                w.cycle(w.now + timedelta(minutes=minutes))
        held = [r.getMessage() for r in caplog.records if "release_held:supervision_active" in r.getMessage()]
        assert len(held) == 1 and '"supervision_active": true' in held[0]

    def test_a_sent_check_in_is_logged_with_its_deadline(self, tmp_path, caplog) -> None:
        from tests.supervision.test_checkins import MON, Env, local
        e = Env(tmp_path)
        e.svc.tick(local(MON, 9))
        with caplog.at_level(logging.INFO, logger=DECISIONS_LOGGER):
            e.svc.tick(e.first_due())
        assert any("Check-in sent: delivered" in r.getMessage() and "answer_by" in r.getMessage() for r in caplog.records)
