"""`python -m hive_mind_receiver`: runs the receiver with uvicorn on RECEIVER_HOST:RECEIVER_PORT (default 127.0.0.1:8787, for a local tunnel)."""

import logging
import os

import uvicorn

from hive_mind_receiver.app import create_app, settings_from_env


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = settings_from_env()
    uvicorn.run(create_app(settings), host=os.environ.get("RECEIVER_HOST", "127.0.0.1"), port=int(os.environ.get("RECEIVER_PORT", "8787")),
                server_header=False, date_header=False, proxy_headers=False, log_level="info")


if __name__ == "__main__":
    main()
