# exemptions

The Temporary Wear Exemption workflow (philosophy 3.6, migration 047). Canonical: `docs/architecture/backlog_completion.md`, section 4. On by default (`ENABLE_EXEMPTIONS`), needs `OWNER_USER_ID`.

`service.py` takes a request from the first message to `PenaltyEngine.freeze()`. Everything after that is the existing pipeline: the window's events, the hold, the barrier and the runtime
halt. `system/dm_actions.py` is the buttons' door.

- The keyholder's button decides. `decide()` reads the request only, never the Penalty Window (invariant I7), and a test proves it with an engine that fails on any touch.
- Nothing here calls Chaster or moves an hour. A pause is a Penalty Window freeze, and the redemption hold stays on.
- This is for planned life. A health or safety need never waits for it (3.9, 3.10).

## Decision log (owner, 2026-11-12)

The first live hygiene opening left no line in any log. Every step now goes to `logs/decisions.log`: asked and offered (within the allowance or not), declined, approved (allowance, health, keyholder), sent to the keyholder, Chaster's answer to the opening, the relock (on time, late, unverified) with whether the lock's own settings were restored, and the end.

## Only a request opens the hygiene card (2026-11-12)

Live, "You're not worried about me misusing the hygiene opening?" got a new opening offered. `is_hygiene_request` now needs a request word ("can I",
"I need", "please", Czech "chci", "potřebuju", "prosím", ...) or the phrase nearly on its own; talk about an opening goes to the conversation.
