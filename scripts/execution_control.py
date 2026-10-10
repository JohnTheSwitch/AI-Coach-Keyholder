"""
scripts/execution_control.py

Local-only control of the Chaster execution runtime halt (the DB kill switch).
This is deliberately a command-line script: it is not reachable from Discord,
the conversation engine or any model.

    python -m scripts.execution_control status
    python -m scripts.execution_control halt  "reason"
    python -m scripts.execution_control clear "reason"

The database path comes from the normal configuration (DB_PATH in .env). The
script prints no secrets and reads no token.
"""

from __future__ import annotations

import sys

from chaster_execution.control import ExecutionControl
from core.config import Config
from infrastructure.clock import SystemClock


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("status", "halt", "clear") or (argv[0] != "status" and len(argv) < 2):
        print(__doc__)
        return 2
    config = Config.load()
    control = ExecutionControl(config.db_path)
    if argv[0] == "status":
        print(f"halted={control.is_halted()} reason={control.reason()!r}")
        return 0
    control.set_halted(argv[0] == "halt", reason=" ".join(argv[1:]), now=SystemClock().now())
    print(f"halted={control.is_halted()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
