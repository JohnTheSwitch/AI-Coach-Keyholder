# Phase J: Zero-Command Conversational UX, Live Consequences, Local Coach

Status: PROPOSAL (2026-11-03). J1 slice 1 BUILT (section 3.1); everything else awaits the owner's decisions in section 6.

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

### 3.2 Next J1 slices (need decisions, section 6)

- **Zero-command migration.** Replace `help`'s command list with a short natural description of what the bot can do; accept natural
  phrasings for the read-only commands ("what's my task?", "am I on vacation?"); keep exact, deterministic phrases (or buttons) for every
  consent/safety action, because those must never be guessed.
- **Consent by conversation or buttons.** When the wearer asks about community review ("who can see my photos?", "stop community
  review"), the bot shows the I3 notice with **Accept / Decline / Revoke** buttons, recorded exactly like `review-consent`. A decline or
  revocation of an exposed lock must switch the lock back to keyholder-only review: that is a live Chaster write from a Discord path,
  which the I2 isolation currently forbids (decision 2).
- **Notification engine.** Generalise the I4 notice delivery (DM, retry, buttons, owner-only) into one outbound queue for every proactive
  message (incidents, poll alerts, consent prompts, J2 consequences), so all of them share retry, quiet hours and the Chaster-chat channel
  preference.

## 4. J2: execution layer and Penalty Engine (proposal)

- Today only `ENABLE_PEER_REVIEW_WATCH` is on: the periodic cycle does not run, the outbox is processed only at bot start, and a confirmed
  Incident lowers Trust at once but reaches the Penalty Engine only at the next restart, with no Chaster effect.
- J2 turns on the periodic cycle (`ENABLE_RULES_ENGINE=true`, outbox every cycle) and connects the Penalty Engine's decisions to the
  existing guarded executor (freeze, add time) under the runtime halt, the limit pool and the audit trail. The DM button label "Confirm
  Penalty" is only accurate once this exists; until then it should read "Confirm Incident" (decision 4).

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
2. **Revert from Discord.** May a wearer's "stop community review" (button or phrase) trigger the switch back to keyholder-only review
   directly from the bot (still owner-only, audited, it only reduces exposure)? This lifts the I2 rule that only the CLI can write
   extensions, for the revert direction only.
3. **J2 consequences.** Which Penalty Engine outcomes become which Chaster actions (freeze? add time? how much?), under which limits?
4. **Button label.** Rename "Confirm Penalty" to "Confirm Incident" until J2 exists?
5. **J3 model.** Which local model for coaching (the current conversation model, or a separate one), and is check-in data allowed into
   Coaching Memory?
