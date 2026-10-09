# application — Phase 3.1: the first usable vertical slice

The first module in this system built for an *interface*, not a
domain concept. Ties every domain module built so far into one
channel-agnostic entry point, and gives Discord (or any future
channel) exactly one thing to call: `ApplicationService.handle_message()`.

## 1. The boundary between the Discord adapter and the application layer

```
bot/discord_bot.py                    application/
  (Discord-specific)                    (channel-agnostic)
──────────────────────                ─────────────────────
discord.Message                →      IncomingMessage
  .author.id, .content,                 channel, external_user_id,
  .channel, .id                          text, received_at

CoachKeyholderBot.on_message()  →     ApplicationService.handle_message()
  - filters to DMs only                 - resolves/creates the UserAccount
  - converts the message                - routes the text to a command
  - calls handle_message()              - calls domain modules' PUBLIC
  - sends the reply                       read APIs only
  - best-effort audit logging           - never raises

discord.Message reply           ←     OutgoingMessage.text
```

**`bot/discord_bot.py` never imports `trust_manager`, `penalty_engine`,
`recovery_plan`, or `goal_management`.** It imports exactly
`application.service.ApplicationService` and
`application.models.IncomingMessage`/`OutgoingMessage`. Every domain
read in this slice (the `status` command) happens inside
`application/service.py`, through a domain module's existing public
API (`PenaltyEngine.get_active_or_frozen_penalty_window()`) — never a
`_*_in_transaction` method, and never a raw table read.

**`application/` never imports `discord`.** Nothing in this package
knows a Discord message exists — `IncomingMessage`/`OutgoingMessage`
are plain dataclasses with no channel-specific fields. A second adapter
(a CLI, an HTTP endpoint, a different chat platform) would construct
`IncomingMessage(channel="cli", external_user_id=..., text=..., received_at=...)`
and call the exact same `handle_message()` — nothing in this layer
would need to change.

## 2. The supported flow of one message

```
Discord DM
  |
  v
CoachKeyholderBot.on_message()
  |  filters: ignore own messages, ignore anything not a DMChannel
  v
best-effort audit log (conversation_messages, Discord-specific columns
  -- an adapter-level concern, kept from Phase 0)
  |
  v
IncomingMessage(channel="discord", external_user_id=str(author.id), text, received_at)
  |
  v
ApplicationService.handle_message()
  |  UserService.get_or_create_user("discord", external_id, now)
  |      -- looks up (channel, external_id) in user_channel_identities;
  |         creates a UserAccount + identity row on first contact
  |
  |  OnboardingService.get_or_create_preferences(user.id, now)
  |      -- if onboarding_step != 'complete', this message is either the
  |         user's very first-ever contact (never treated as an answer --
  |         shown the current step's prompt directly) or an answer to
  |         whatever step they're on (docs/architecture/user_onboarding_technical_design.md);
  |         either way, CommandRouter is never reached for an incomplete user
  v
CommandRouter.route(text, RequestContext(user, now))
  |  exact-match, case-insensitive, against a fixed, explicit command set
  |  (only reached once onboarding_step == 'complete')
  v
a registered handler (e.g. _handle_status) calls a domain module's
  public read API and returns OutgoingMessage
  |
  v  (back up through handle_message(), never raises)
discord.Message reply sent to the same DM
  |
  v
best-effort audit log of the outgoing message
```

Both audit-log steps are deliberately isolated in their own
try/except (`_log_message_best_effort()`) — a logging failure must
never replace a real reply with the generic fallback, and must never
prevent the reply from being sent at all. Found as a real gap while
writing this adapter's own tests (the first draft coupled incoming-log
and reply generation in one try/except, and left the outgoing log
completely unprotected) — see `tests/bot/test_discord_bot.py`'s
`TestAdapterLevelErrorHandling` for the two distinct failure modes it
now separates: a logging failure that must NOT affect the reply, and a
failure reaching the application layer that MUST produce the safe
fallback.

## 3. What actually works today

- **Discord onboarding, persisted and resumable**
  (`docs/architecture/user_onboarding_technical_design.md`) —
  language → AI gender → personality, backed by `user_preferences`
  (migration 013). Runs before `CommandRouter` for any user who hasn't
  finished it; a process restart resumes from whatever step was last
  persisted, with no separate resume logic. `ai/identity_catalog.py`
  is the 15-identity reference catalog used for display/validation —
  a direct transcription of `ai_identity_technical_design.md` Sections
  3/10, not a second source of truth for that data.
- **A real, minimal command set** — `help` (lists commands), `status`
  (reports the current `PenaltyWindow`, if any, via
  `PenaltyEngine.get_active_or_frozen_penalty_window()` — a real read
  against real domain state, not a mock), `preferences` (read-only,
  shows a completed user's saved language/AI voice/personality),
  `mode`/`mode status`/`mode request advanced`/`mode request standard`/
  `mode cancel`/`mode confirm` (Advanced Mode's two-stage transition,
  now reachable from Discord — see below), `lock status`/
  `lock report locked`/`lock report unlocked`, `task request`/
  `task active`/`task complete`/`task cancel` (First Testable Keyholder
  Milestone, Slice C — see below).
- **First Testable Keyholder Milestone, Slice C.** `LockState`/
  `LockStateAdministration`/`TaskCatalog`/`TaskRuntime`/
  `TaskRuntimeAdministration` are constructed inside
  `ApplicationService.__init__` -- `LockStateAdministration`/
  `TaskRuntimeAdministration` always self-constructed there (never
  DI'd, never crossing into the Conversation Engine provider graph);
  `LockState`/`TaskRuntime`/`TaskCatalog` accept new, optional DI
  parameters as of Slice D -- see below -- with the same
  self-construction as a fallback for callers that don't pass them.
  `lock`/`task` are
  each their own command family (`register_family`, the same pattern
  `mode` already established) — **Command Family Fallback Precision**:
  the family reply fires only when the *entire* trimmed/lowercased
  input is exactly the bare family word (`"lock"`, `"task"`, `"mode"`,
  and, as of CHASTER-01A, `"chaster"`)
  alone, never merely because the input starts with it. A multi-word
  input starting with a family word that is not an exact registered
  command (a near-miss typo like `"task compelte"`, or ordinary
  conversation that happens to start with a common English word, e.g.
  `"task is really weighing on me today, can we talk?"`) now falls
  through as ordinary unmatched text, reaching Conversation Engine
  exactly like any other unmatched multi-word input — deliberately not
  fuzzy matching, an exact string-equality check only. Exact-command
  matching itself is unaffected (`"task request"` etc. are still
  matched by the router's own exact-handler lookup first, before the
  family lookup ever runs). `task request` reads the current
  `LockKnowledgeState`, filters eligible templates via
  `TaskRuntime.get_eligible_templates()`, and picks one
  deterministically (`task_runtime.selection.select_eligible_template()`
  — lowest `template_id`, alphabetically; neither `get_active_templates()`
  nor `get_eligible_templates()` carries an explicit `ORDER BY`, so this
  ordering is imposed in Python, not assumed from the query). Every
  `TaskAssignment*Error`/`TaskNotEligibleError` gets its own specific
  `except` clause mapped to a safe, generic reply — the same
  discipline `mode`'s own handlers already use, never a raw exception
  message reaching the user.

  **Deterministic Task Command Human-Readable Content** (extends
  Candidate B, `task_catalog`'s own `title`/`instructions` fields, to
  the deterministic command replies): `task active` reads the
  assignment's own exact pinned `(template_id, template_version)` via
  `self.task_catalog.get_template(...)` — never `current_version` —
  and replies `"Active: {title} ({template_id}, assigned {timestamp}).`;
  `task request`'s success reply is `"Assigned: {title}."`, reusing
  the already-selected `TaskTemplateVersion` directly rather than a
  second lookup; the "already have one" branch resolves the existing
  assignment's own pinned version the same way `task active` does. A
  legacy row with `title=None` (a pre-migration-021 row) renders as the
  literal string `"(not recorded)"` in all three replies — the exact
  same convention `conversation_engine/prompt_builder.py` already uses
  for the same case, never fabricated. `instructions` is deliberately
  **not** shown in any of these three replies (out of scope for this
  slice — a terse command reply is the wrong place for a full
  instructions paragraph); `task complete`/`task cancel` replies were
  left generic (`"Completed."`/`"Cancelled."`) in this slice —
  extended to the same title-aware convention by the follow-up
  **Title-aware `task complete`/`task cancel` confirmation** slice
  (`"Completed: {title}."`/`"Cancelled: {title}."`, same `"(not
  recorded)"`/pinned-historical-version invariants, `_resolve_task()`'s
  own shared helper now resolving the template after a successful
  transition rather than before it — `assignment` is read before
  `resolve()` either way, so its own pinned version is unaffected by
  when the lookup happens).
  **Verified directly, not just claimed:**
  known `lock`/`task` commands never invoke the model at all; ordinary
  conversational text mentioning lock/task-like intent (e.g. "I'm
  locked right now", "I finished it") never creates a `lock_reports`
  row or resolves a `task_assignments` row; a deterministic command's
  own reply text never enters Working Memory (`tests/application/
  test_lock_task_conversation_boundary.py`). `scripts/seed_development_tasks.py`
  is a separate, explicitly-invoked, idempotent maintenance script
  (`python3 -m scripts.seed_development_tasks`) — deliberately not
  wired into `bot/discord_bot.py`'s own startup (which never creates
  domain data today) and not a migration (schema only, per this
  project's own convention).
- **CHASTER-01A — `chaster connect`.** Registered exactly like every
  other command (`register("chaster connect", ..., handler)` +
  `register_family("chaster", ...)`) — never model-generated, never
  reachable before onboarding completes (the router itself is never
  reached until then). `ApplicationService.__init__` gained new,
  optional `chaster_connections`/`chaster_oauth_client` DI parameters
  (`self.chaster_states` is always self-constructed -- it needs no
  encryption or client credentials); if either optional parameter is
  `None`, the handler replies that Chaster is "not configured" rather
  than raising. See `chaster/README.md` for the full module boundary
  and its own honestly-flagged limitation (identity resolution).
- **Self-healing Chaster tokens + `chaster disconnect`.**
  `ApplicationService.__init__` gained an optional
  `chaster_token_manager` DI parameter (auto-built from
  `chaster_connections` + `chaster_oauth_client` when both are given).
  `chaster status` now calls the Chaster API through
  `ChasterTokenManager.call_with_token()` (pre-call refresh; one
  refresh-and-retry on HTTP 401) and answers a connection that can no
  longer be renewed with a red "Chaster Reconnection Required" embed.
  `chaster connect` issues a fresh link even for an already-connected
  user. New `chaster disconnect` is a deterministic, local-only removal
  with a green/blue embed. See `chaster/README.md` for the full detail.
- **Chaster LLM tool embeds.** When `ConversationResponse.tool_outputs`
  holds a `ChasterToolOutcome`, `ApplicationService` attaches the
  `EmbedSpec` built by the shared `_chaster_message()` (the same builder
  `chaster status` uses) to the model's conversational reply. The embed
  is built from the real tool output, never from model text.
- **First Testable Keyholder Milestone, Slice D.** `ApplicationService.__init__`
  gained new, optional `lock_state`/`task_runtime`/`task_catalog`
  parameters — see `conversation_engine/README.md`'s own "Slice D"
  section for the full detail (this is the DI half of that same
  change; the composition root half lives in `bot/discord_bot.py`).
  `tests/application/test_conversation_context_integration.py`
  verifies the whole path end-to-end through `ApplicationService`
  itself, not just the Conversation Engine internals.
- **Advanced Mode's transition process, wired end-to-end from Discord
  DM to `advanced_mode`'s own persisted state and back.** No new
  natural-language parsing — each `mode ...` command is registered as
  its own literal, multi-word string key in `CommandRouter` (which
  already matched on the full trimmed/lowercased text; no router
  change was needed at all). Two things specific to this integration,
  not part of `advanced_mode` itself:
  - **Explicit settle-before-act orchestration**
    (`ApplicationService._settle_mode_state()`), called at the start of
    every `mode ...` handler: `PenaltyEngine.ensure_current_state(now)`
    first, then `AdvancedModeAdministration.advance_transition_state(...)`.
    Both are real writes, both explicit here — `AdvancedMode`'s own
    read-only API is never the one applying them (see
    `advanced_mode/README.md` for why that separation matters).
  - **`IncomingMessage.external_message_id`** (a small, deliberately
    minimal extension, not a general consent module or table) —
    Discord's own `message.id`, threaded through
    `IncomingMessage` → `RequestContext` → `f"discord_message:{id}"`
    as the consent reference for `request_transition()`/
    `confirm_transition()`. Since a request and its later confirmation
    are necessarily two different incoming Discord messages, they
    always get two independently auditable consent references for
    free, with no separate mechanism needed.
- **`on_system_startup()` is now actually wired into the running
  process** (`bot/discord_bot.py main()`) — a real gap fixed while
  building this: `system_state_machine.md` Section 7 has always said
  this must run "before the Discord bot starts," but no adapter
  actually called it until this phase. Trust Manager/Penalty Engine/
  Recovery Plan/Goal Management recovery, plus the outbox publisher, now
  genuinely run at process startup, not only inside tests.
- **User identity across restarts** — the same Discord account always
  resolves to the same `UserAccount`, with `last_seen_at` tracked.
- **DM-only communication**, enforced by the adapter, tested directly
  (`TestDMFiltering`).
- **Errors are caught at two independent layers** — inside
  `ApplicationService.handle_message()` itself, and again in the
  adapter's own call into it — and always produce a safe, generic
  reply, never a leaked exception message or a crashed handler.
- **Everything above is tested without a live Discord connection** —
  `discord.Client.on_message()` is a plain coroutine; the adapter tests
  call it directly with a constructed fake message via `asyncio.run()`
  (no `pytest-asyncio` dependency added — stdlib only).

## 4. What is deliberately deferred

- **Any actual Coach/Keyholder reasoning, Behavior Learning, or LLM
  involvement** — `help`/`status`/`preferences` are hand-written, fixed
  responses against real data; nothing here calls an LLM or makes a
  judgment call. `ai/identity_catalog.py` exists (as static reference
  data for onboarding — see above), but the actual communication
  pipeline (`ai_identity_technical_design.md`'s own Sections 4–17,
  phrasing a `Decision` in an identity's voice) and
  `core/coach_engine.py` still do not.
- **A natural-language router** — `CommandRouter` matches exact,
  trimmed, lowercased command strings only. No intent parsing.
- **Trust Manager/Goal Management status commands** — both modules are
  designed around looking up a specific `domain_id`/`goal_group_id`;
  neither has a "list everything relevant" read API yet. `status` is
  scoped to Penalty Engine only in this slice rather than inventing a
  new domain-module method just to make the command feel complete.
- **Any write-capable command outside Advanced Mode's own transition**
  (starting/freezing/completing a Penalty Window directly, proposing or
  accepting a Goal change) — still out of scope. `mode request ...`/
  `mode cancel`/`mode confirm` are this project's *first* write-capable
  Discord commands (see above) — the consent/confirmation UX question
  this note used to defer is now answered for exactly this one case
  (Advanced Mode's own two-stage `critical_change` process, already
  designed with Discord-message-based consent references in mind), not
  generalized to every other domain module's own writes.
- **Multi-user support in the domain modules themselves** —
  `user_accounts`/`user_channel_identities` are purely this layer's own
  bookkeeping. No domain table anywhere has a `user_id` column; this
  system remains built for exactly one real person, as it always has
  been. See the schema comment in migration 011 for the explicit
  reasoning against reading this as a step toward multi-tenancy.
- **Any channel other than Discord** — the boundary above is designed
  to make a second adapter possible without touching `application/`,
  but no second adapter exists yet.


v0.28: `handle_message` runs `is_chaster_status_query` after the command router and before the engine; a match returns the `chaster status` reply (embed) without involving any model. `_handle_chaster_status` now returns a red "Chaster Not Configured" embed when no token manager is configured.


v0.29: `_answer_chaster_status_question` implements the hybrid path (read data -> `extra_system_context` -> engine text; deterministic embed for every non-success state and for generation failure). `_read_chaster_outcome` is shared with `chaster status`.


v0.30: the hybrid lock-status path passes the user's language to `build_chaster_status_context`; `cs` selects the Czech prompt and pre-formatted Czech time.


v0.35: general chat turns load Chaster context (`_chaster_chat_context`) for lock-related wording or an active connection; failures are swallowed so chat is never blocked.

v0.37: English only. The hybrid status path and the passive chat context no longer take a language (`build_chaster_*_context(locks, now=)`), the engine is always called with `en`, and onboarding offers English only (accounts that stored `cs` keep working and get English). 2110 tests.
