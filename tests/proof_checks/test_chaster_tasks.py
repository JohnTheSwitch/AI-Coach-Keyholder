"""The spot checks' Chaster calls (verified live 2026-11-13): the right endpoints, a 401 renews once, an ambiguous write says so."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from chaster.lock_client import ChasterLockApiError
from proof_checks.chaster_tasks import ChasterTaskError, ChasterTasks

NOW = datetime(2026, 11, 13, 21, 0, tzinfo=timezone.utc)


class Tokens:
    def __init__(self) -> None:
        self.calls = 0

    def call_with_token(self, user_id, *, now, operation):
        self.calls += 1
        try:
            return operation("T1")
        except ChasterLockApiError as exc:
            if exc.status_code != 401:
                raise
        return operation("T2")


class Locks:
    def __init__(self, current=None) -> None:
        self.current = current

    def get_lock(self, *, access_token, lock_id):
        return {"extensions": [{"slug": "temporary-opening", "_id": "e0"},
                               {"slug": "tasks", "_id": "ext9", "userData": {"currentTask": self.current}}]}

    def get_task_history(self, *, access_token, lock_id):
        return [{"id": "x1", "status": "pending_verification"}]


def tasks(locks, responses):
    sent = []

    def send(method, url, *, json, headers, timeout):
        sent.append((method, url, json, headers["Authorization"]))
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if response == 201:
            locks.current = {"id": "new1", "task": json["payload"]["task"]["task"]} if json else None
        return SimpleNamespace(status_code=response)

    return ChasterTasks(token_manager=Tokens(), account_user_id="kh", lock_client=locks, clock_now=lambda: NOW, send=send), sent


def test_assign_uses_the_extension_action_with_photo_and_code() -> None:
    locks = Locks()
    client, sent = tasks(locks, [201])
    assert client.assign("L1", "Show: Study") == "new1"
    method, url, body, _auth = sent[0]
    assert (method, url) == ("POST", "https://api.chaster.app/locks/L1/extensions/ext9/action")
    assert body == {"action": "assignTask", "payload": {"task": {"task": "Show: Study", "points": 0, "verificationRequired": True, "codeRequired": True}}}


def test_unassign_uses_the_lock_id() -> None:
    client, sent = tasks(Locks(), [201])
    client.unassign("L1")
    assert sent[0][1] == "https://api.chaster.app/extensions/tasks/L1/unassign-task"


def test_a_401_renews_once() -> None:
    client, sent = tasks(Locks(), [401, 201])
    client.unassign("L1")
    assert [s[3] for s in sent] == ["Bearer T1", "Bearer T2"]


def test_a_timeout_or_5xx_is_ambiguous_and_a_4xx_is_not() -> None:
    for responses, ambiguous in (([TimeoutError()], True), ([503], True), ([400], False)):
        client, _ = tasks(Locks(), responses)
        with pytest.raises(ChasterTaskError) as info:
            client.unassign("L1")
        assert info.value.ambiguous is ambiguous


def test_reads() -> None:
    client, _ = tasks(Locks(current={"id": "c", "task": "x"}), [])
    assert client.current_task("L1") == {"id": "c", "task": "x"} and client.has_tasks("L1")
    assert client.task_entry("L1", "x1") == {"id": "x1", "status": "pending_verification"} and client.task_entry("L1", "nope") is None
