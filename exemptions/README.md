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

## Decided by the system, on the wearer's reason (owner, 2026-11-13; migration 054, `judge.py`)

Live, the wearer was also the account that got the keyholder's Approve button, and approved their own 24-hour exemption. Now, like the detectors'
judge (infractions/judge.py):

- After the category and the length (now also 3 and 12 hours: "till evening" had to be 24), the bot asks what it is for. The next message is the
  reason (`ApplicationService.exemption_reason`, right after commands, before the status matchers and the model: live, "I need to be unlocked till
  evening" got the lock status and "I need 10 hours" the model's own refusal).
- **Code first:** the health category, or a reason mentioning health, pain, an injury or an emergency, is approved without any model (3.9, 3.10).
- **Then the model** answers approve / decline / unclear. Unclear gets exactly one follow-up question; unclear again is a decline. A request within
  24 hours of a declined one is judged as an appeal and must bring something new. The reason is data, never instructions.
- A model failure is never a decline: the request stays `requested`, is retried on every pass, and after an hour is closed as `unjudged` ("ask again").
- An extra hygiene opening past the week's planned breaks goes the same way ("Ask for an extra one" -> why -> the judge).
- Like the button, the judge reads only the request (I7), plus the reason of a decline in the last day for an appeal.
- `decide()` refuses a request's own wearer, also without a judge. A separate keyholder (OWNER_USER_ID not the wearer) only gets a note of the ruling.
- `decided_by`: `judge`, `judge_health` or `unjudged`; `verdict_reason` is what the wearer was told.
- **Two appeals** (owner, 2026-11-13, "2 appeals for any judge"): each new request within 24 hours of a declined one is an appeal, judged with
  all the declined reasons of that day. After two declined appeals, a further request that day is declined in code without the model, except a
  health or safety need, which is never held back.
