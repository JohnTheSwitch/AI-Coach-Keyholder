# Phase J: Zero-Command Conversational UX, Live Consequences, Local Coach

Status: COMPLETE (owner, 2026-11-04). J1 slices 1 and 2 (sections 3.1, 3.2), J2 with its refinements (section 4) and J3 (section 5) are built. The two J1 slices in section 3.3 were not built and are carried forward, not part of this completion (section 6.2).

## 1. Design principles (owner, 2026-11-03)

1. **Zero-Command UX.** No public slash or prefix commands on Discord (`/status`, `/incidents` and similar are dropped; none were ever
   registered as Discord application commands). The wearer talks to the bot in plain language in DMs; questions like "How much time is
   left?" or "Show my progress" are understood conversationally.
2. **Trust is a backend variable.** The Trust score (and the Trust level derived from it) is an internal decision input only. It is never
   shown to the user: not in replies, not in notices, not in the model's prompt context (philosophy 4.2, 2.6/2.22: transparency of
   reasons, not of internal computation).
3. **Diagnostics are CLI-only.** Every debug, audit and system-status view stays in `python -m cli ...` on the bot's machine.
4. **Safety and consent stay deterministic.** Conversational does not mean model-decided: anything that changes consent, data or a live
   lock is recognised by plain code (exact phrases or buttons), never by the language model, and is confirmed explicitly (philosophy 2.5).

### 1.1 Where the repository stands

- No Discord slash or prefix command exists. The bot does, however, have about 40 plain-word text commands (`help`, `status`,
  `lock status`, `task request`, `mode confirm`, `forget all`, `research on`, `chaster connect`, ...) routed before the model, and `help`
  prints them as a command list. Several are consent- or safety-critical (`mode request/confirm`, `forget all` + `yes`, `research on/off`,
  `conditioning on/off`, `vacation ...`, and in multi-wearer mode `halt`, `resume`, `leave confirm`, `accept`, `decline`).
- Natural-language lock-status questions are already answered deterministically (`chaster/llm_tools.py::is_chaster_status_query`), but
  only when the message names the lock or Chaster: "How much time is left?" alone reached the model.
- The Trust score is not exposed anywhere today (no context provider, reply or notice contains it); J1 adds a guard test so it stays so.
- Incident notices with buttons exist (Phase I4).

## 2. Modules

| Module | Content | Live effect |
|---|---|---|
| J1 | Natural conversational responses and event notifications: status / time-left / progress questions answered deterministically from real data; proactive notices with buttons; wearer consent by conversational trigger or DM buttons; the zero-command migration of the text commands | read-only, except the consent path (section 6) |
| J2 | Execution layer and Penalty Engine: confirmed Incidents lead to live Chaster consequences; the periodic evaluation cycle (`ENABLE_RULES_ENGINE=true`) and outbox processing run continuously | LIVE Chaster writes (runtime halt, audit) |
| J3 | Local Ollama coach: daily check-ins, goal negotiation and coaching dialogues on the local model | conversation only; goal changes need consent |

## 3. J1: natural conversational response and event notifications

### 3.1 Slice 1 (BUILT 2026-11-03)

- **Time-left questions.** `is_chaster_status_query` gained one rule: a short question (with `?`, at most 80 characters) asking how much
  time is left / how long / until when (English and Czech: "how much time is left?", "how long until I'm out?", "kolik mi zbývá?",
  "jak dlouho ještě?") goes to the same read-only Chaster status answer. Messages about a task timer ("task", "úkol") and about physical
  locks are excluded, as before.
- **Progress questions.** `application/progress.py`: `is_progress_query()` recognises "show my progress", "how am I doing?", "my
  progress", "jak mi to jde?", "můj pokrok" and similar short messages, and `ProgressReader` answers from real data, without the model:
  - the user's local task (active one, tasks completed in the last 7 days);
  - a running or planned vacation;
  - for the OWNER only (the wearer of the connected account's locks): the community reviews of the last 7 days (verified / rejected /
    still voting) and how many rejections still wait for the keyholder's decision, and whether a penalty window is running.
  The reply never contains the Trust score or level, and never internal identifiers.
- **Guard.** Tests assert the progress answer and the status answer contain no Trust value, and that `application/progress.py` reads no
  Trust Manager state.

### 3.2 Slice 2 (BUILT 2026-11-03): DM-native consent and formatted notices

- **Buttons on any reply.** `OutgoingMessage.actions` (channel-agnostic `ActionSpec`: id, label, style); the Discord layer turns them into
  persistent buttons (`bot/action_buttons.py`, built as the registered `DynamicItem`, so clicks still work after a restart; an action
  without a registered handler is dropped, never shown dead). Other channels ignore them and show the text, which always describes the
  choice.
- **Consent by conversation.** `application/review_consent_query.py` recognises "who can see my photos?", "stop community review",
  "I don't want strangers to see my pictures", "kdo vidí moje fotky?", "komunitní hodnocení". For the OWNER the reply is the I3 notice with
  each locked lock's consent state and exposure and the buttons that fit (Accept + Decline when undecided, Accept when declined or
  revoked, Revoke when approved). The text never decides anything; only a button press does. Other users' messages are not intercepted.
- **The decision path.** `system/review_consent_dm.py` (`ReviewConsentDm`) is the bot's ONLY door to the extension-review service and
  exposes nothing but the overview and `decide_consent()` (a test asserts it never calls `apply`, `preview` or `set_extensions`). Only the
  owner's verified Discord account can decide; the record is written exactly like the CLI's. A lock held as keyholder can be decided in
  Discord only after the owner attested once on the CLI (`--wearer-is-my-account`); the attestation is carried into each new record.
  Decline / Revoke of an exposed lock runs the I3 automatic revert (keyholder-only review), which the owner approved in I3 (revocation
  reverts automatically, also while halted); this answers decision 2 for the revert direction only. The forward write stays CLI-only.
- **Formatted proactive notices.** `peer_review/notices.py`: the rejection notice is an embed ("Photo review rejected", red: lock, votes,
  incident id, how to decide) with the Confirm / Dismiss buttons; the watch alert (red) and recovery note (green) are embeds too. The
  plain text stays the fallback. `DiscordMessagingAdapter.send_message(..., embed=)` sends the embed and the buttons as one message.

### 3.3 J1 slices carried forward (BUILT 2026-11-04, see `backlog_completion.md`)

- **Zero-command migration.** Replace `help`'s command list with a short natural description of what the bot can do; accept natural
  phrasings for the read-only commands ("what's my task?", "am I on vacation?"); keep exact, deterministic phrases (or buttons) for every
  consent/safety action, because those must never be guessed.
- **Notification engine.** Generalise the I4 notice delivery (DM, retry, buttons, owner-only) into one outbound queue for every proactive
  message (incidents, poll alerts, consent prompts, J2 consequences), so all of them share retry, quiet hours and the Chaster-chat channel
  preference.

## 4. J2: Penalty Engine, Release Engine and the barrier (BUILT 2026-11-04, owner's aligned ruleset)

The owner's conflict review (six decisions) supersedes the earlier raw-hours design. Sections 4.5 to 4.7 record the refinements that followed.

### 4.1 Ruleset

| Event | Consequence |
|---|---|
| Task or goal failure | Trust and token earning ONLY. Never a Penalty Window, a freeze or a Chaster change. The shipped `DEFAULT_RULESET` has no `ADD_TIME` rule. |
| Missed check-ins | Trust (per-miss evidence) and, at 3 (consecutive, or within 48 h), a Warning DM. Never a release veto. The escalation's Incident stays UNCONFIRMED; the keyholder's `check-in-escalations confirm` records a Warning and nothing more (`backlog_completion.md` 5). |
| Confirmed infraction (rejected photo; `HYGIENE_MISUSE`; lying about lock status) | Trust drops; the Penalty Engine alone decides: a first minor offense is a Warning, a repeated or severe one a Penalty Window. |
| Emergency Override, Mandatory Health Access | NEVER an Incident or a penalty (philosophy 3.8, 3.9). The hygiene catalog holds only `HYGIENE_MISUSE`. |

### 4.2 Pipeline

```
Confirm button / CLI -> incident.confirmation_changed -> (outbox, every cycle) Penalty Engine -> Warning | penalty_window.started
penalty_window.* -> rules_engine/penalty_hold.py records the hold and the barrier intent -> chaster_execution/penalty_barrier.py -> freeze
```

- **Outbox.** The periodic cycle now runs whenever the community review watch is on, every `CHASTER_EXECUTION_INTERVAL_SECONDS` (60), beside the
  10-minute watch loop, under the existing single-instance lease. It also ends a window whose time is up (`ensure_current_state`), which
  nothing did periodically before.
- **Engine boundaries.** The Penalty Engine is the single source of truth. `rules_engine/penalty_hold.py` only records state from the window's own
  events and imports neither the Penalty Engine nor the Trust Manager; `RulesEngineService` still reacts to goal assessments only.
- **Tokens.** Earning continues during a window (the daily pool has no penalty gate). Redemption is blocked globally by the `penalty_holds` row and
  the Penalty Engine's own state, through the Release Engine's existing hard veto, until the window expires.

### 4.3 The barrier

| Window event | Intent | Executed when |
|---|---|---|
| started | freeze | the lock is not frozen yet (only then does the window count as having raised the barrier) |
| paused for an approved exemption | unfreeze | always: an approved exemption takes precedence (the window timer is paused, redemption stays blocked) |
| resumed after an exemption | freeze | the lock is not frozen |
| completed | unfreeze | only if this window raised the barrier; a barrier it found already up stays up |
| paused for an Emergency Override | none | that path stays outside this mechanism |

- **72-hour backstop.** Untouched and inviolable: it releases during a window. The barrier acts on window events only, never on an observed state, so
  it never re-freezes a lock the backstop released.
- **Halt.** Intents live in their own durable queue instead of `lock_decisions`, so the 15-minute decision expiry stays intact for everything
  else and a penalty survives a halt of any length. While `halted=True` (or the feature is off) nothing is read from or sent to Chaster, the
  intended action is logged once and the intent stays `pending`; it executes when the halt clears. A freeze whose window ended meanwhile is
  skipped. The halt is checked again immediately before the call.
- **Targeting.** Token blocking is global. Live calls go only to `PENALTY_FREEZE_LOCK_ID` (`6aca95cd...`), and only if a fresh read shows role
  `keyholder`; a worn lock such as `6ae98ca...` is never called, its penalty is the redemption hold alone.
- **At most once.** The `executing` claim is committed before the call; an ambiguous outcome is `needs_review` and never retried; only a call that
  provably reached nothing stays queued.

Diagnostics: `python -m cli penalty-status --owner-id <id>`. Switches: `ENABLE_PENALTY_BARRIER` (default false: a dry run that logs and queues),
`PENALTY_FREEZE_LOCK_ID`, and the runtime halt.

### 4.4 Not built, or for the owner to decide

- Exemptions and both detectors: built, `backlog_completion.md`.
- The escalation's unconfirmed Incident is kept for the keyholder's manual review (owner decision, 2026-11-04): `check-in-escalations confirm` leads
  to a Warning and never further (superseded 2026-11-04, `backlog_completion.md` 5).
- The Release Engine stays off (`ENABLE_RELEASE_ENGINE`, it needs `CHASTER_EXECUTION_WEARER_USER_ID`), so tokens and releases are not live yet.

### 4.5 Zero-Time-Modification (owner, 2026-11-04)

The bot never changes a lock's duration, up or down. Removed, not merely unused: `ActionType.ADD_TIME` / `REMOVE_TIME`, the time limits, the executor's
time branch, and the transport's `add_time` / `remove_time` and its `update-time` call. `DEFAULT_RULESET` is empty. Lock progression is governed
purely by the frozen state and the Release Engine. A leftover time row in an old database is never selected. Chaster's own extensions (`pillory`,
`link`) can still change time by themselves, on Chaster's side; that is not the bot.

### 4.6 Communication-First release (owner, 2026-11-04)

A release is never held back silently (`system/release_notice.py`, migration 045):

1. When every condition is met (plan due, no veto, barrier confirmed, discretionary window, a token), and only then, so the plan's timing stays
   secret, the user is told they are eligible, before anything else happens.
2. If today's check-in is incomplete, that same message says: "You're eligible for release today! Please complete your quick daily check-in first
   to execute the unfreeze." The release waits and no token is spent.
3. The reply triggers the release on the next periodic cycle (at most `CHASTER_EXECUTION_INTERVAL_SECONDS`, 60 s), whatever a rubric makes of it.

Never a trap: an unanswered prompt expires as `skipped` with no Trust effect. Never a silent wait: if no check-in applies today (no check-ins, Trust
level 4, a vacation) or the prompt cannot be delivered, the release goes ahead; if the day's window closes while it waits, the user is told once.
The 72-hour backstop never passes through this. No text carries a time or a token count.

### 4.7 Simplified consent (owner, 2026-11-04)

A personal workflow: the I3 notice is informational (proceeding implies agreement, with a link to the repository), shown at the end of onboarding, in
the `chaster connect` reply (`application/terms_notice.py`) and in the consent overview. There is no re-acceptance when its wording changes; `stale` is
gone. Kept on purpose: an explicit decline or revocation still refuses (2.5: revocable), and a lock worn by ANOTHER account still needs that wearer's
approval or the owner's attestation, because that wearer may never have seen any notice. Multi-wearer invitations keep their explicit `accept`.
`philosophy.md` was not changed; 2.5 says consent is "specific", so implied agreement rests on this being the owner's own use.

## 5. J3: local Ollama coach (proposal)

Code: `coach/` (the catalogue, the gates, the ledger), `system/coach_dm.py` (cards, buttons, wiring), `CoachContextProvider`, a coach turn in
`ConversationEngine`, migration 046. Off by default (`ENABLE_COACH`), and the bot needs a restart for any of it. See `coach/README.md`.

### 5.1 Owner decisions (2026-11-04)

1. Consent: an explicit decline or revocation, and the approval or attestation for a third party's lock, stay fully enforced.
2. Live testing: physical lock destruction and unlock (the Chaster `/unlock` endpoint) stay BANNED. Unfreezing through the Release Engine (`frozen=False`)
   is allowed once the execution halt is lifted. The transport has no unlock method at all, and a test pins that public surface.
3. Unconfirmed check-in escalation Incidents stay for manual review.

### 5.2 Objectives and what they became

1. **Ollama integration.** The connection already existed (`OllamaConversationModel`). J3 adds what running it needs: `OLLAMA_NUM_CTX` (Ollama's default
   window is the model's maximum: `llama3.1` loaded at 23 GB, `qwen3-vl` at 46 GB with an 80 second first reply) and a read-only readiness check at
   startup and in `python -m cli coach-status`. Decision 5 below: the coach uses `OLLAMA_MODEL`; there is no separate coach model.
2. **Context-aware dialogue.** The prompt gains the user's goals with their numbers, recent commitments, the last 7 days of tasks and any waiting proposal,
   next to the lock facts it already had. The fragment comes from a whitelist of fields, text is cleaned and quoted as data, and the coach reads no Trust
   state, so the score cannot reach the prompt. Guard tests cover the package imports and the rendered prompt.
3. **Goal negotiation.** A check-in reply is a coach turn with a negotiation instruction. The model may offer one commitment; the user can accept, decline, or
   answer in words, and a revised offer replaces the waiting one. Scheduled check-ins add today's commitment and an invitation to the question (plain code:
   no model call in the periodic cycle). The release check-in keeps the owner's wording. Goals can also come up in ordinary conversation.
4. **Action safeguards.** Section 5.3.

### 5.3 The action path

```
model reply (JSON, Ollama `format`) -> reply text goes to the user
                                    -> `proposal` -> validate_action -> gates -> pending row -> card -> the user's button -> GoalManager
```

- **Measured first.** Native tool calling was tried on the owner's models and rejected: `llama3.1` called a tool for "hi", proposed "Take a 1-hour break from
  your lock" and "I will immediately unlock you right now", and every call loses the reply text and costs a second generation. The JSON schema behaved on
  both models in one call. So the request is a field of the reply, and the existing read-only tools stay exactly as they were.
- **Closed catalogue.** `propose_goal`, `adapt_goal`, `propose_commitment`. All three are proposals. Nothing exists for a lock, time, a release, a token, a
  penalty, Trust, a rule, consent, a message, or a task assignment.
- **Strict validation** (`coach/actions.py`). Exact keys, exact types, length limits, no control or invisible characters, no links. It rejects, it never repairs.
- **Deterministic gates** (`coach/desk.py`). Right user; no vacation; no negotiation when the user's message mentions pain, injury or distress; no regime
  words and nothing risky in the text (`coach/safety.py`); goal numbers only reach active goals; at most 3 proposals a day, 5 active goals and 3 commitments a
  day; nothing similar to a declined proposal for 14 days (2.5); a negotiation cannot change kind, and the same proposal again is no second card. A refusal stores a reason code, never the text.
- **Deterministic confirmation.** Only the user's own button applies a proposal, from the stored record. The card is built from that record, never from the
  model's sentence. A second press, an expired card or another account's press changes nothing, and the lock state is checked again at the press.
- **Never a broken reply.** A failing port, an unusable output or a model failure leaves the conversation as it was. A proposal the model pasted into its
  reply is cut out before the user sees it (measured on `llama3.1`).

Philosophy 2.5 is this order: proposal, explanation, user approval, application. `_SYSTEM_BOUNDARIES` is unchanged, and an untrusted channel never reaches any of it.

### 5.4 Commitment weight (owner, 2026-11-04)

A commitment reported as **Not kept** is a minor Trust reduction and nothing else: the `coach.commitment_missed` event, whose only consumer is the Trust Manager,
writes one piece of evidence of -0.01 (half a first missed check-in, flat, no streak). It is not goal evidence, so the daily token evaluation never sees it, and it
makes no Incident, no Penalty Window, no freeze and no token taken away. Kept and Partly do nothing, and neither does a report during a vacation. A commitment
nobody answered lapses after 3 days at no cost (`_SYSTEM_BOUNDARIES`: no fact inferred from silence). Someone reporting pain or an emergency is not asked about a
commitment in that reply. `forget all` erases proposals, refusals and commitments, but Trust evidence is append-only (TI3) and stays; goals stay as agreed records.

What a lower Trust score does, through machinery that already existed: daily token progress grows with Trust, so tokens are earned very slightly slower (no token
is ever removed), and crossing a level threshold changes the number of check-ins a day. No rule in `DEFAULT_RULESET` reacts to Trust; a Trust rule added to a
future ruleset would see the lowered score like any other.

### 5.4.1 Coaching Memory (owner, 2026-11-04)

With `ENABLE_COACHING_MEMORY` on, every check-in reply is kept as a dated quote in Coaching Memory, in the user's own words, for multi-day context. It goes through
the service's own filters: nothing about health or distress is stored, nothing instruction-like, at most 5 notes a day. `memory` shows it and `forget all` erases
it. The prompt shows the 5 most recent notes. With the coach on, Coaching Memory no longer needs `CONVERSATION_STRUCTURED_OUTPUT`, and coach turns can also save
the model's own notes.

### 5.5 Limits, and what is not built

- **The Iron Contract and goals (owner, 2026-11-04).** Rule 7 forbids negotiating while locked, but the wearer is usually locked, so J3 reads it narrowly: time,
  deadlines, assigned tasks and check-ins are not negotiable; new goals and commitments are. An agreed target stays frozen during an active lock: `adapt_goal` needs
  the wearer to be KNOWN to be unlocked, at the proposal and again at the button, so an unknown lock state refuses too.
- **Small models are uneven.** In the live runs `qwen3-vl` once acknowledged a counter-offer in words without revising the card, so the old card stood until
  declined. Both models sometimes ask a question instead of proposing. Nothing breaks; the user just gets no card that turn.
- **No model-written proactive messages.** A check-in question is fixed text plus plain code.
- **Multi-wearer tenants are not wired**: a tenant's invitation does not disclose the coach.
- **Coaching Memory on ordinary turns.** A user with an active Chaster connection always takes the context path, which has no JSON reply, so
  `CONVERSATION_STRUCTURED_OUTPUT` has no effect for them outside coach turns. This predates J3; coach turns do carry the side fields.

## 6. Decisions

### 6.1 Resolved

- **Button label (the original decision 4)** stays open as a cosmetic point in 6.2.
- **J2 consequences (3)**: section 4. **J3 model (5)**: `OLLAMA_MODEL=llama3.1`, check-in data may enter Coaching Memory. **Goal changes while locked (6)**: frozen.
  **Commitments (7)**: a minor Trust reduction only.

### 6.2 Carried forward, outside Phase J

Questions 1 to 3 are closed by `backlog_completion.md`, which also lists what stays open for the owner.

1. **Which text commands are retired?** Proposed: keep every consent/safety phrase exactly (`forget all`/`yes`, `mode ...`, `research ...`,
   `conditioning ...`, `vacation ...`, `halt`, `resume`, `leave confirm`, `accept`, `decline`, `chaster connect/disconnect`) but stop
   advertising them as "commands" (no command list); retire the read-only commands in favour of natural questions only after the natural
   paths cover them.
2. **Revert from Discord** (built in slice 2 on the strength of the I3 approval that a revocation reverts automatically): confirm that a
   Decline / Revoke button may switch the lock back to keyholder-only review from the bot. It is owner-only, audited, passes the halt and
   can only reduce exposure; the forward write stays CLI-only. If not wanted, the facade can record the decision and leave the revert to
   the CLI.
3. **J2 consequences.** Which Penalty Engine outcomes become which Chaster actions (freeze? add time? how much?), under which limits?
4. **Button label.** Rename "Confirm Penalty" to "Confirm Incident" until J2 exists?
5. **J3 model (resolved).** `OLLAMA_MODEL=llama3.1`; check-in data may enter Coaching Memory.
6. **Goal changes while locked (resolved).** Frozen (section 5.5).
7. **Commitments and tokens (resolved).** A minor Trust reduction only, no goal evidence (section 5.4).
