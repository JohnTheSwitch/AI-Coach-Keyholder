# coach — Phase J3

Canonical: `docs/architecture/phase_j_proposal.md`, section 5. On by default (`ENABLE_COACH`).

The coach lets the local model propose goals and daily commitments in the Discord DM. The model proposes and the user decides (philosophy 2.5): nothing the
model writes changes anything, and only the user's own button applies a proposal.

```
model reply (JSON) -> the reply text goes to the user
                   -> `proposal` -> validate_action -> the gates -> a pending row -> a card -> the user's button -> GoalManager
```

## Files

- **`actions.py`** (pure): the closed catalogue, `propose_goal`, `adapt_goal` and `propose_commitment`, and `validate_action`, which rejects and never repairs. To
  add an action, add it here with a test. Nothing exists for a lock, time, a release, a token, a penalty, Trust, a rule, consent, a message or a task assignment.
- **`safety.py`** (pure): the content check. Regime words and anything that could hurt the user are refused, English and Czech.
- **`desk.py`**: the gates, the `coach_proposals` / `coach_refusals` / `daily_commitments` ledger (migration 046), the decision on a button press, and the
  context the model sees. It is the only way from a model request to `GoalManager`.

`system/coach_dm.py` holds what touches the channel: the engine's port, the cards, the buttons' door and `build_coach`. `bot/action_buttons.py::CoachButton` is
the persistent button. `conversation_engine/context_providers/coach_provider.py` passes a field whitelist into the prompt.

## Live chat fixes (2026-11-13)

A check-in answer ("at the focus conference again") got "That's not the goal." `CHECK_IN_GUIDANCE` now tells the model the message is the
wearer's report on their day, never to be called wrong or "not the goal". After a restart only replies the model wrote come back into its
memory (`conversation_messages.from_model`, migration 058; `system/memory_seed.py`): command replies and status cards had come back as its own
words. While supervision or the penalty hold holds the release, the model is told the requirements are not met (`release_phase`).

## The gates

Each refusal stores a reason code in `coach_refusals`, never the text. `python -m cli coach-status` counts them.

| Code | Meaning |
|---|---|
| `unknown_action`, `unknown_parameter`, `missing_parameter`, `invalid_*` | failed validation |
| `not_this_user` | the coach serves one wearer per database |
| `paused` | a vacation is in force |
| `safety_topic` | the user's message mentions pain, injury, distress or an emergency |
| `regime_topic`, `unsafe_content` | `safety.py` |
| `locked` | `adapt_goal` unless the wearer is known to be unlocked; checked again at the button |
| `unknown_goal`, `too_many_goals`, `too_many_commitments` | a goal number must reach an active goal; at most 5 goals, 3 commitments a day |
| `other_kind_pending` | a counter-offer stays the kind it was |
| `already_waiting` | the same proposal again: no second card, and it uses up nothing |
| `daily_limit` | at most 3 proposals a local day |
| `declined_recently` | nothing similar to a declined proposal for 14 days |

## What it never does

- It reads no Trust state and imports no execution, release, penalty, Trust, rules or Iron Contract code (guard test). Locks and vacations arrive as injected callables.
- A commitment reported as not kept is a minor Trust reduction (-0.01, through `coach.commitment_missed`); owner, 2026-11-13: kept is the mirror, +0.01
  (`coach.commitment_kept`). Neither takes a token or reaches a lock. Partly kept, unanswered, and anything reported during a vacation do nothing for Trust.
- Owner, 2026-11-13: a KEPT report sooner than `MIN_KEPT_AFTER` (30 minutes) after the commitment was made, sooner than the time its text names
  ("study for an hour"), or before its day has begun is recorded but earns nothing (no Trust, no goal result); a decision line says so.
- Owner, 2026-11-13 (before, nothing ever recorded goal evidence, so goals counted for nothing): a commitment tied to a goal and reported kept or not kept
  is that day's goal result (`coach.goal_result_reported` -> `goal_management.record_reported_result_in_transaction`, MET or MISSED, period inside the
  commitment's local day). The daily evaluation then counts the goal for the token pool and the clean day. Partly kept records nothing.
- An untrusted channel never reaches it, and multi-wearer tenants are not wired, because a tenant's invitation does not disclose it.

`forget all` erases proposals, refusals and commitments. Goals stay as agreed records, and Trust evidence is append-only.

A commitment's text never says "today", "tomorrow" or "tonight" (`without_day_words`): it has its own date, and the word turned false the next day. Older rows
are shown without the words too. An earlier day's open commitment is asked about before anything new is planned: its Kept / Partly / Not kept card comes first,
with every proposal, after a check-in, when the user says it is done, and otherwise once a local day in any conversation.

**Plan the day ahead (owner, 2026-11-10).** Before 18:00 local the check-in asks for today's plan when there is none; from 18:00 on it asks for tomorrow's, and a proposal "for today" made in the evening is filed for tomorrow by code. A bare filler like "go to sleep", "rest" or "relax" is refused (`filler`): a commitment is real work, specific enough to check.

With `ENABLE_COACHING_MEMORY` on, check-in replies are kept as dated quotes through Coaching Memory's own filters (`system/coach_dm.py`).

## Turning it on

`ENABLE_COACH=true`, a model that follows a JSON schema (`OLLAMA_MODEL`; `llama3.1` and `qwen3-vl` were tested), `OLLAMA_NUM_CTX=8192` or similar, a Trust domain
to label goals with (`CHECK_IN_TRUST_DOMAIN`), and a user (`CHECK_IN_USER_ID`, `CHASTER_EXECUTION_WEARER_USER_ID` or `OWNER_USER_ID`). The check-in negotiation also
needs `ENABLE_CHECK_INS`, and multi-day memory needs `ENABLE_COACHING_MEMORY`.

## Only yesterday's commitment is asked about (owner, 2026-11-12)

`OUTCOME_WINDOW` is one day: a Tuesday card shown on Thursday was stale. Older open commitments lapse, at no cost.

## Commitment time windows (owner, 2026-11-13; honesty and proof, slice 1; migration 062)

"To prove something, it has to be reported right after it." Design: `docs/architecture/honesty_and_proof_proposal.md`.

- **Accepting picks the part of the day.** A commitment card offers **Morning** (end of quiet hours to 12:00), **Afternoon** (12:00-18:00),
  **Evening** (18:00 to the waking day's end) and **Any time**, only the parts that have not ended; one tap accepts it. The waking day ends
  `ASK_LEAD` (30 minutes) before quiet hours, so the question below is asked and answered before them. Accepted after its waking day: it can
  still be reported for `REPORT_GRACE`, and is never asked about.
- **"Done?" when it ends.** `CoachDesk.due_asks` (every minute, `CoachDm.ask_loop`) queues the card once per commitment through the notification
  queue: **Done / Partly / Not done / Still doing it**. Nothing is asked during a vacation, or after the report window (the bot was off).
  **Still doing it** asks once more, up to `EXTENSION` (1 hour) later, never in quiet hours and never past the report window.
- **The report window.** A Kept counts from the window start plus the duration its text names (and the existing `MIN_KEPT_AFTER` rule) until
  `REPORT_GRACE` (2 hours) after the window ends. Outside it, it is recorded (`report_timing` too_soon / late) and earns nothing: no Trust, no goal
  result. A late Kept is told why; the review card says so beforehand. A **Not kept always counts**: owning up is never too late.
- Commitments from before migration 062 have no window: the whole waking day of their date applies.
- Without `to_utc` (tests, a wiring without a time zone) there are no windows and only the too-soon rule applies.

## Night commitments (owner, 2026-11-13; roadmap slice 2)

A bedtime or sleep commitment ("Go to bed 30 minutes earlier", `is_night_commitment`, English and Czech; "make the bed" is not one) is kept at
night, so the part of the day does not apply: the card offers a single **Accept** ("I'll ask you the next morning"), the window runs from
`NIGHT_LEAD` (2 hours) before quiet hours until 10:00 the next day, "Did you?" (Done / Partly / Not done) is asked when quiet hours end, and a Kept
counts until **12:00 the next day**. A Kept before the window is too soon. Older bedtime commitments without a window get the same noon limit.
Never spot-checked (`proof_checks.service.can_be_shown`).
