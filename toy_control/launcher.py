"""
toy_control/launcher.py

Owner, 2026-11-25: "Could we make it open Intiface in the background on its own, so the only thing I would need to do is insert, turn on and
press that it's in?" When a session finds nothing listening on Intiface's port, the bot starts Intiface Central itself, minimized and without
taking the focus, and waits for its server. Intiface Central starts its server on launch (its own setting "start server on startup", on on
this PC). Only Intiface's own program at its installed path is ever started; nothing is downloaded, and the bot never closes it.
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

from infrastructure.decision_log import toy_log

__all__ = ["IntifaceLauncher", "intiface_path", "LAUNCH_WAIT_SECONDS"]

logger = logging.getLogger("ai_coach_keyholder.toy_control")

LAUNCH_WAIT_SECONDS = 45.0
_SW_SHOWMINNOACTIVE = 7


def intiface_path() -> Path | None:
    """Intiface Central's installed program (the per-user install), or None when it isn't there. `INTIFACE_PATH` overrides it."""
    given = os.environ.get("INTIFACE_PATH")
    if given:
        return Path(given) if Path(given).is_file() else None
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    path = Path(appdata) / "IntifaceCentral" / "intiface_central.exe"
    return path if path.is_file() else None


def _minimize_windows(pid: int) -> int:
    """Live, 2026-11-25: Intiface (a Flutter app) ignores "start minimized" and opened in front of him. Its visible windows are minimized
    without taking the focus once they exist. Returns how many were minimized."""
    if os.name != "nt":
        return 0
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    found: list = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _l):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True

    user32.EnumWindows(each, 0)
    for hwnd in found:
        user32.ShowWindow(hwnd, _SW_SHOWMINNOACTIVE)
    return len(found)


def _listening(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


class IntifaceLauncher:
    """Callable: True when Intiface's server is listening (started now, or already up), False when it could not be started."""

    def __init__(self, url: str, *, path=intiface_path, popen=subprocess.Popen, listening=_listening, sleep=time.sleep,
                 wait_seconds: float = LAUNCH_WAIT_SECONDS, minimize=_minimize_windows) -> None:
        parsed = urlparse(url)
        self._host, self._port = parsed.hostname or "127.0.0.1", parsed.port or 12345
        self._path, self._popen, self._listening, self._sleep, self._wait = path, popen, listening, sleep, wait_seconds
        self._minimize = minimize

    def __call__(self) -> bool:
        if self._host not in ("127.0.0.1", "localhost", "::1"):
            return False                                    # only Intiface on this PC
        if self._listening(self._host, self._port):
            return True
        exe = self._path()
        if exe is None:
            toy_log("Intiface is not running and its program was not found.")
            return False
        try:
            kwargs = {}
            if os.name == "nt":
                info = subprocess.STARTUPINFO()
                info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                info.wShowWindow = _SW_SHOWMINNOACTIVE      # minimized, never stealing the focus
                kwargs = {"startupinfo": info, "creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
            process = self._popen([str(exe)], cwd=str(Path(exe).parent), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, close_fds=True, **kwargs)
        except OSError as exc:
            toy_log("Intiface could not be started.", error=str(exc))
            return False
        toy_log("Intiface was not running: started it in the background.")
        pid = getattr(process, "pid", None)
        waited, hidden = 0.0, 0
        while waited < self._wait:
            self._sleep(1.0)
            waited += 1.0
            if pid is not None and not hidden:
                hidden = self._safe_minimize(pid)
            if self._listening(self._host, self._port):
                toy_log("Intiface's server is up.", seconds=round(waited))
                self._sleep(2.0)                            # its Bluetooth manager comes up just after the port
                if pid is not None and not hidden:
                    hidden = self._safe_minimize(pid)       # a window that appeared late
                toy_log("Intiface's window minimized." if hidden else "Intiface's window was not found to minimize.")
                return True
        toy_log("Intiface was started but its server never came up.", seconds=round(waited))
        return False

    def _safe_minimize(self, pid: int) -> int:
        try:
            return int(self._minimize(pid) or 0)
        except Exception:
            logger.exception("Intiface's window could not be minimized; the session goes on.")
            return 0
