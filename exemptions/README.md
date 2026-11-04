# exemptions

The Temporary Wear Exemption workflow (philosophy 3.6, migration 047). Canonical: `docs/architecture/backlog_completion.md`, section 4. On by default (`ENABLE_EXEMPTIONS`), needs `OWNER_USER_ID`.

`service.py` takes a request from the first message to `PenaltyEngine.freeze()`. Everything after that is the existing pipeline: the window's events, the hold, the barrier and the runtime
halt. `system/dm_actions.py` is the buttons' door.

- The keyholder's button decides. `decide()` reads the request only, never the Penalty Window (invariant I7), and a test proves it with an engine that fails on any touch.
- Nothing here calls Chaster or moves an hour. A pause is a Penalty Window freeze, and the redemption hold stays on.
- This is for planned life. A health or safety need never waits for it (3.9, 3.10).
