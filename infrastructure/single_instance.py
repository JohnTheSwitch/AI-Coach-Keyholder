"""
infrastructure/single_instance.py

One bot per machine (owner, 2026-11-16: the bot now starts at boot through a scheduled task, and the old login script or a manual start
could otherwise run a second copy; two bots would both answer every DM). The guard holds a localhost TCP port for the life of the process:
the operating system frees it when the process ends, however it ends, so a crash never leaves a stale lock.

The same port takes a stop request (owner, 2026-11-16): a bot started by the boot task runs in its own logon session, where the owner's
restart script may not end its process. A client sends the secret from STOP_TOKEN_FILE (written at start, in the gitignored data folder,
readable only by this user) followed by a newline; the bot then shuts down cleanly. Anything else is ignored.
"""

from __future__ import annotations

import hmac
import logging
import secrets
import socket
import threading
from pathlib import Path
from typing import Callable

__all__ = ["INSTANCE_PORT", "STOP_TOKEN_FILE", "hold_single_instance", "serve_stop_requests", "request_stop"]

logger = logging.getLogger("ai_coach_keyholder.single_instance")

INSTANCE_PORT = 8429          # beside the OAuth callback (8420) and the emergency server (8421)
STOP_TOKEN_FILE = Path("data") / "instance_stop_token"


def hold_single_instance(port: int = INSTANCE_PORT) -> socket.socket | None:
    """The held socket (keep a reference for the process's life), or None when another instance already holds the port."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        sock.bind(("127.0.0.1", port))
        sock.listen(4)
    except OSError:
        sock.close()
        return None
    return sock


def serve_stop_requests(sock: socket.socket, on_stop: Callable[[], None], *, token_file: Path = STOP_TOKEN_FILE) -> str:
    """Writes a fresh secret to `token_file` and answers stop requests on the held socket in a daemon thread. Returns the secret."""
    token = secrets.token_hex(16)
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(token, encoding="ascii")

    def serve() -> None:
        while True:
            try:
                conn, _addr = sock.accept()
            except OSError:
                return                                   # the socket was closed: the process is ending
            try:
                conn.settimeout(5)
                data = conn.recv(128).decode("ascii", "ignore").strip()
                if hmac.compare_digest(data, token):
                    conn.sendall(b"stopping\n")
                    logger.info("A stop request arrived on the instance port; the bot shuts down.")
                    on_stop()
                else:
                    conn.sendall(b"no\n")
            except Exception:
                logger.warning("A connection to the instance port was not a valid stop request.", exc_info=True)
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    threading.Thread(target=serve, name="instance-stop", daemon=True).start()
    return token


def request_stop(port: int = INSTANCE_PORT, *, token_file: Path = STOP_TOKEN_FILE, timeout: float = 5.0) -> bool:
    """Client side: True when the running bot accepted the stop request."""
    try:
        token = token_file.read_text(encoding="ascii").strip()
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as conn:
            conn.sendall(token.encode("ascii") + b"\n")
            return conn.recv(32).startswith(b"stopping")
    except OSError:
        return False
