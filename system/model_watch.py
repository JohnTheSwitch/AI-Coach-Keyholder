"""
system/model_watch.py

Owner, 2026-11-25 (proposal 1, "model-down alert"): on 2026-11-25 Ollama was broken from 07:25 to about 17:00 (an auto-update had removed its
runner), the judge failed 281 times and nobody knew. Now:

- every CHECK_EVERY the watch looks at the last model call (`decision_log.model_health()`); with no successful call in PROBE_AFTER it asks the
  model one tiny question itself;
- RESTART_AFTER without an answer: when nothing listens on Ollama's port, the boot task "AI Coach Ollama" (windows/install_boot_task.ps1) is
  started once. A server that answers but fails is left alone (a restart does not fix a broken install);
- ALERT_AFTER without an answer: one DM to the owner. When the model answers again, one DM that it is back.

Operational only: no state in the database besides the two notifications, nothing decided, nothing about any lock.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import subprocess
from datetime import datetime, timedelta
from typing import Callable

from infrastructure.decision_log import decision, model_health

__all__ = ["ModelWatch", "start_ollama_task", "DOWN_TEXT", "BACK_TEXT"]

logger = logging.getLogger("ai_coach_keyholder.model_watch")

CHECK_EVERY = timedelta(minutes=5)
PROBE_AFTER = timedelta(minutes=30)
RESTART_AFTER = timedelta(minutes=15)
ALERT_AFTER = timedelta(minutes=30)
BOOT_TASK = "AI Coach Ollama"
DOWN_TEXT = ("Heads-up: my model hasn't answered for 30 minutes, so I can't judge explanations or talk freely right now (Ollama on the PC "
             "needs a look). Explanations you send wait until it's back, and a model failure never counts against you.")
BACK_TEXT = "My model is answering again."


def _listening(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def start_ollama_task(host: str = "127.0.0.1", port: int = 11434, *, run=subprocess.run, listening=_listening) -> str:
    """Starts the boot task that runs `ollama serve`, only when nothing listens on its port. Returns what happened, for the log."""
    if listening(host, port):
        return "running_but_failing"
    try:
        done = run(["schtasks", "/Run", "/TN", BOOT_TASK], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as exc:
        return f"start_failed:{type(exc).__name__}"
    return "started" if getattr(done, "returncode", 1) == 0 else "start_failed"


class ModelWatch:
    def __init__(self, *, probe: Callable[[], bool], notify: Callable[[str, str], None], restart: Callable[[], str] | None = None,
                 health: Callable[[], dict] = model_health) -> None:
        """`probe()`: True when the model answered a tiny question. `notify(dedupe_key, text)`: a DM to the owner."""
        self._probe, self._notify, self._restart, self._health = probe, notify, restart, health
        self.down_since: datetime | None = None
        self._alerted = False
        self._restarted = False

    def check(self, now: datetime) -> bool:
        """One look. True when the model is answering."""
        seen = self._health()
        ok_at, error_at = seen.get("ok_at"), seen.get("error_at")
        if ok_at is not None and now - ok_at < PROBE_AFTER and (error_at is None or ok_at >= error_at):
            up = True
        elif error_at is not None and now - error_at < CHECK_EVERY and (ok_at is None or error_at > ok_at):
            up = False                                          # a real call just failed: no need to ask again
        else:
            try:
                up = bool(self._probe())
            except Exception:
                up = False
        return self._up(now) if up else self._down(now)

    def _up(self, now: datetime) -> bool:
        if self.down_since is not None:
            decision("model", "The model answers again.", down_minutes=round((now - self.down_since).total_seconds() / 60))
            if self._alerted:
                self._notify(f"model:up:{self.down_since.isoformat()}", BACK_TEXT)
        self.down_since, self._alerted, self._restarted = None, False, False
        return True

    def _down(self, now: datetime) -> bool:
        if self.down_since is None:
            self.down_since = now
            decision("model", "The model stopped answering.")
            return False
        down = now - self.down_since
        if self._restart is not None and not self._restarted and down >= RESTART_AFTER:
            self._restarted = True
            try:
                what = self._restart()
            except Exception as exc:
                what = f"start_failed:{type(exc).__name__}"
            decision("model", "Tried to start Ollama.", result=what)
        if not self._alerted and down >= ALERT_AFTER:
            self._alerted = True
            self._notify(f"model:down:{self.down_since.isoformat()}", DOWN_TEXT)
            decision("model", "The owner was told the model is down.", down_minutes=round(down.total_seconds() / 60))
        return False

    async def loop(self, clock) -> None:
        while True:
            await asyncio.sleep(CHECK_EVERY.total_seconds())
            try:
                await asyncio.to_thread(self.check, clock.now())
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("The model watch failed this round.")
