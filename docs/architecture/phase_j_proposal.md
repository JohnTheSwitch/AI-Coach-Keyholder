# Phase J: Zero-Command Conversational UX, Live Consequences, Local Coach

Status: PROPOSAL. J1 slices 1 and 2 (sections 3.1, 3.2) and J2 (section 4) BUILT; the rest awaits the owner's decisions in section 6.

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

### 3.3 Next J1 slices (need decisions, section 6)

- **Zero-command migration.** Replace `help`'s command list with a short natural description of what the bot can do; accept natural
  phrasings for the read-only commands ("what's my task?", "am I on vacation?"); keep exact, deterministic phrases (or buttons) for every
  consent/safety action, because those must never be guessed.
- **Notification engine.** Generalise the I4 notice delivery (DM, retry, buttons, owner-only) into one outbound queue for every proactive
  message (incidents, poll alerts, consent prompts, J2 consequences), so all of them share retry, quiet hours and the Chaster-chat channel
  preference.

## 4. J2: Penalty Engine, Release Engine and the barrier (BUILT 2026-11-04, owner's aligned ruleset)

The owner's conflict review (six decisions) supersedes the earlier raw-hours design. Nothing in J2 adds or removes time on Chaster.

### 4.1 Ruleset

| Event | Consequence |
|---|---|
| Task or goal failure | Trust and token earning ONLY. Never a Penalty Window, a freeze or a Chaster change. The shipped `DEFAULT_RULESET` has no `ADD_TIME` rule. |
| 3 missed check-ins (consecutive, or within 48 h) | Trust drops through the per-miss evidence; a Warning is issued (event `check_in.warning_issued`, DM); the Incident stays UNCONFIRMED. Only the keyholder's confirmation (`python -m cli check-in-escalations confirm`) can lead further. |
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

- Nothing in production approves an exemption yet: `PenaltyEngine.freeze()` has no caller, so the exemption release is ready but has no trigger.
- `EPISTEMIC_MISREPRESENTATION` and `HYGIENE_MISUSE` have no detector; a confirmed photo rejection is the only working infraction today.
- `DEFAULT_RULESET` still removes time on goal progress and high Trust (`REMOVE_TIME`): the decision named rules that ADD hours, so these stayed.
- A missed check-in still vetoes a release (`ReleaseGates.last_check_in_missed`), so it can keep a barrier up for a while.
- The I3 consent notice does not mention that a confirmed rejection can freeze the lock.
- The Release Engine itself stays off (`ENABLE_RELEASE_ENGINE`, it needs `CHASTER_EXECUTION_WEARER_USER_ID`), so tokens are not earned or spent live yet.

## 5. J3: local Ollama coach (proposal)

- Daily check-ins and coaching dialogues on the local model (the check-in scaffolding, `supervision/`, and the Conversation Engine with
  Coaching Memory already exist; check-ins are off).
- Goal negotiation: the model proposes, the user decides; any goal or rule change goes through the existing consent process (2.5); the
  model never sees the Trust score.

## 6. Decisions needed

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
5. **J3 model.** Which local model for coaching (the current conversation model, or a separate one), and is check-in data allowed into
   Coaching Memory?
