"""
infrastructure/single_instance.py

One bot per machine (owner, 2026-11-16: the bot now starts at boot through a scheduled task, and the old login script or a manual start
could otherwise run a second copy; two bots would both answer every DM). The guard holds a localhost TCP port for the life of the process:
the operating system frees it when the process ends, however it ends, so a crash never leaves a stale lock.
"""

from __future__ import annotations

import socket

__all__ = ["INSTANCE_PORT", "hold_single_instance"]

INSTANCE_PORT = 8429          # beside the OAuth callback (8420) and the emergency server (8421)


def hold_single_instance(port: int = INSTANCE_PORT) -> socket.socket | None:
    """The held socket (keep a reference for the process's life), or None when another instance already holds the port."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        sock.bind(("127.0.0.1", port))
        sock.listen(1)
    except OSError:
        sock.close()
        return None
    return sock
