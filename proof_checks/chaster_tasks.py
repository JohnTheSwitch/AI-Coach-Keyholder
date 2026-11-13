"""
proof_checks/chaster_tasks.py

The only Chaster calls of the spot checks, as the keyholder account (verified live 2026-11-13, docs/architecture/chaster_capability_map.md section 8):

  read    GET  /locks/{lockId}                                   the `tasks` extension id and the wearer's current task
  read    GET  /extensions/tasks/{lockId}/history                a submitted task, its submission time and its community review
  write   POST /locks/{lockId}/extensions/{extensionId}/action   {"action": "assignTask", ...} with verificationRequired and codeRequired
  write   POST /extensions/tasks/{lockId}/unassign-task          takes a task back that was not done in time

Nothing here changes time, freezes, unfreezes or unlocks. The writes are made only while the runtime halt is clear (the service checks it first).
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from chaster import CHASTER_USER_AGENT
from chaster._http import send_with_ordered_headers
from chaster.lock_client import ChasterLockApiError

__all__ = ["ChasterTasks", "ChasterTaskError"]

API = "https://api.chaster.app"
_TIMEOUT = (10.0, 30.0)


class ChasterTaskError(RuntimeError):
    def __init__(self, message: str, *, ambiguous: bool = False) -> None:
        super().__init__(message)
        self.ambiguous = ambiguous          # the request may have been processed (timeout, 5xx): read back before anything else


class ChasterTasks:
    def __init__(self, *, token_manager, account_user_id: str, lock_client, clock_now: Callable[[], datetime],
                 send: Callable = send_with_ordered_headers) -> None:
        self._tokens = token_manager
        self._account = account_user_id
        self._locks = lock_client
        self._now = clock_now
        self._send = send

    def _call(self, operation):
        return self._tokens.call_with_token(self._account, now=self._now(), operation=operation)

    def _extension(self, lock_id: str) -> dict | None:
        lock = self._call(lambda token: self._locks.get_lock(access_token=token, lock_id=lock_id))
        return next((e for e in lock.get("extensions") or () if isinstance(e, dict) and e.get("slug") == "tasks"), None)

    def current_task(self, lock_id: str) -> dict | None:
        ext = self._extension(lock_id)
        data = ext.get("userData") if ext else None
        task = data.get("currentTask") if isinstance(data, dict) else None
        return task if isinstance(task, dict) else None

    def has_tasks(self, lock_id: str) -> bool:
        return self._extension(lock_id) is not None

    def assign(self, lock_id: str, text: str) -> str:
        """The new task's id. Raises ChasterTaskError (ambiguous=True when it may have been assigned anyway)."""
        ext = self._extension(lock_id)
        if ext is None or not isinstance(ext.get("_id"), str):
            raise ChasterTaskError("the lock has no tasks extension")
        body = {"action": "assignTask", "payload": {"task": {"task": text, "points": 0, "verificationRequired": True, "codeRequired": True}}}
        self._post(f"{API}/locks/{lock_id}/extensions/{ext['_id']}/action", body)
        task = self.current_task(lock_id)
        if task is None or task.get("task") != text or not isinstance(task.get("id"), str):
            raise ChasterTaskError("the assigned task could not be read back", ambiguous=True)
        return task["id"]

    def unassign(self, lock_id: str) -> None:
        self._post(f"{API}/extensions/tasks/{lock_id}/unassign-task", {})

    def task_entry(self, lock_id: str, task_id: str) -> dict | None:
        """The history entry of a task once it was submitted (or removed), else None. Raises when the history cannot be read."""
        history = self._call(lambda token: self._locks.get_task_history(access_token=token, lock_id=lock_id))
        return next((h for h in history or () if isinstance(h, dict) and h.get("id") == task_id), None)

    def _post(self, url: str, body: dict) -> None:
        def operation(token: str):
            try:
                response = self._send("POST", url, json=body, headers={"Authorization": f"Bearer {token}", "User-Agent": CHASTER_USER_AGENT},
                                      timeout=_TIMEOUT)
            except Exception as exc:                                     # a timeout or a dropped connection: it may have been processed
                raise ChasterTaskError(f"request failed: {type(exc).__name__}", ambiguous=True) from None
            if response.status_code == 401:
                raise ChasterLockApiError("unauthorized", status_code=401)     # not processed: the token manager renews once and retries
            return response

        response = self._call(operation)
        if response.status_code not in (200, 201, 204):
            raise ChasterTaskError(f"HTTP {response.status_code}", ambiguous=response.status_code >= 500)
