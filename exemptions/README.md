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
- **A clear health reason skips the category (owner, 2026-11-17):** when the request and what he wrote just before it already hold the reason and the judge's own code test finds it a health need, the category buttons are skipped: it is filed as Health and the length buttons come at once.
- **Code first:** the health category, or a reason mentioning health, pain, an injury or an emergency, is approved without any model (3.9, 3.10).
- **Then the model** answers approve / decline / unclear. Unclear gets exactly one follow-up question; unclear again is a decline. A request within
  24 hours of a declined one is judged as an appeal and must bring something new. The reason is data, never instructions.
- A model failure is never a decline: the request stays `requested`, is retried on every pass, and after an hour is closed as `unjudged` ("ask again").
- An extra hygiene opening past the week's planned breaks goes the same way ("Ask for an extra one" -> why -> the judge).
- Like the button, the judge reads only the request (I7), plus the reason of a decline in the last day for an appeal.
- `decide()` refuses a request's own wearer, also without a judge. A separate keyholder (OWNER_USER_ID not the wearer) only gets a note of the ruling.
- `decided_by`: `judge`, `judge_health` or `unjudged`; `verdict_reason` is what the wearer was told.
- **One appeal, then "Take it anyway"** (owner, 2026-11-13): a new request within 24 hours of a declined one is its one appeal. An appeal with no new
  word beyond the declined reasons is declined in code, without the model. After the appeal is declined, a further request that day is declined in
  code, and the decline carries a **Take it anyway** button (valid 2 hours): the lock opens as for an approved request (`decided_by`
  `wearer_override`) and, in the same transaction, it is filed as an incident the wearer disclosed themselves (`EXEMPTION_OVERRIDE`, warning only:
  Trust and a Warning, never a Penalty Window; Trust domain `routine`, or `hygiene` for an extra hygiene opening). If it cannot be recorded,
  nothing opens. A health or safety need is never held back by a used appeal and never needs the button.

## The relock picture is checked (owner, 2026-11-15; migration 072)

After a planned hygiene opening, while the relock picture is required (hygiene Trust below 0.70, see `chaster_execution/README.md`), the
bot reads Chaster's verification pictures 30 minutes after the relock. A picture from just before the relock to 30 minutes after it counts;
none = the wearer is asked to explain (infractions path); unexplained = `HYGIENE_PICTURE_MISSING`, a Warning and 20 lines ("I send my relock
picture every time."). A health opening is never checked; an unreadable Chaster is retried for 6 hours and then dropped (unknown never
counts against him). "I'm back" is never needed for any of this: the relock time is Chaster's own.

**Health cap (owner, 2026-11-15).** Code approves a health exemption for at most `HEALTH_AUTO_HOURS` (24). Longer, or a renewal while/within 24 hours after a health exemption, goes to the judge (`long_health`): it may approve the full time; if it refuses or cannot answer, the first 24 hours are still approved by code (a renewal the model cannot answer also gets 24 more hours). Urgent needs never come here.

**G2 (owner, 2026-11-15).** The relock picture is checked after health openings too (it restricts nothing; a health explanation is excused by code). `get_health_uses` feeds `system/health_pattern.py`: health openings, health exemptions, the obedience health button and detector questions excused by health are counted; from the 3rd in 7 days the keyholder asks once a week; the weekly review shows the count.

## The reason he already gave (owner, 2026-11-15)

Live: he explained the dancing school, asked for an exemption, and after the buttons was asked for the reason again. Now the request message and what he wrote in the 15 minutes before it (`Database.get_user_texts_since`) are kept as the reason when they make a real sentence (`SAID_MIN_WORDS`, 8 words): the judge decides right after the duration is chosen. A bare "I need an exemption" is still asked.
