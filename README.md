# AI Coach & Keyholder

A personal, local AI system combining a long-term coach with a
consistency/accountability system. See [`philosophy.md`](philosophy.md)
for the principles every decision and every future change must satisfy,
and [`docs/architecture/`](docs/architecture/) for the full
architecture baseline (nine technical designs + an integration audit +
implementation conventions -- all with status *Architecture baseline --
approved for implementation*).

## Architecture at a glance

The system is event-driven: domain modules never call each other's
internals directly. Each module owns its own state, publishes domain
events when that state changes, and reacts to other modules' events
through narrow, named consumer handlers -- never by reaching into
another module's tables or opening a second database transaction
mid-reaction. See `implementation_conventions.md` Section 3 and
`system/README.md` for the precise rules this follows, including a real
constraint (`NestedTransactionError`) discovered while wiring the first
cross-module consumer, and how it shaped every consumer written since.

The current, fully implemented lifecycle:

```
Incident (Trust Manager)
    │  confirmed
    ▼
Trust Score (recalculation pipeline)
    │  incident.confirmation_changed
    ▼
Penalty Window (Penalty Engine)
    │  should_extend() -- Extension
    ▼
Recovery Plan
    │  task completed
    ▼
Recovery Credit (back into Penalty Engine)
```

Three domain modules (Trust Manager, Penalty Engine, Recovery Plan) and
one composition layer (`system/`) exist today, fully wired together
through real, tested events -- not mocks. Each module's own README
documents exactly what it covers, what's deliberately deferred, and any
real architectural findings surfaced while building it:

- [`trust_manager/README.md`](trust_manager/README.md) -- Incident
  lifecycle, confirmation, severity assessment, the trust score
  recalculation pipeline.
- [`penalty_engine/README.md`](penalty_engine/README.md) -- the
  PenaltyWindow state machine, freeze-as-a-set-of-reasons, Extension
  (`should_extend()`), and Recovery Credit.
- [`recovery_plan/README.md`](recovery_plan/README.md) -- the Recovery
  Plan lifecycle as a pure reaction to Penalty Window events, plus
  Coach-facing task management.
- [`system/README.md`](system/README.md) -- the startup orchestrator,
  the consumer/dispatch framework, and the composition-layer wiring
  between all of the above.
- [`infrastructure/README.md`](infrastructure/README.md) -- the shared
  `Clock`, `Database`/`Transaction`, and transactional outbox that
  every module above is built on.

## Project status

**1744 passing tests** across the whole repository (`pytest`), including
two repository-wide guard tests: one that mechanically confirms no
production code outside `infrastructure/clock.py` calls
`datetime.now()`/`datetime.utcnow()` directly, and one that confirms
every `BOOTSTRAP_DEFAULT`-tagged constant uses the agreed structured
form (`tests/test_bootstrap_default_tags.py` — see "Bootstrap defaults"
in `trust_manager/README.md` and `penalty_engine/README.md`). This
figure has been corrected several times from stale counts once found
to be out of date: 1017 during the Task Catalog Human-Readable Task
Content slice below, 1081 (this section's own count, left un-updated
by that slice's deliberately narrow scope — its own instructions
touched only `application/README.md`, not this file) after the
Deterministic Task Command Human-Readable Content slice, item 32
below, 1091 after the Title-aware `task complete`/`task cancel`
confirmation slice, item 33 below, 1122 after the Scarlett/Hybrid
Personality Presentation slice, item 34 below (corrected here from an
earlier, wrong 1101 written into this same paragraph during that
slice — a transcription mistake in the documentation update itself,
not a re-run of the suite), 1143 after that same item's own identity-lifecycle acceptance-hardening pass (no new
roadmap item — this extended and verified item 34, not a new
capability; see `conversation_engine/README.md`'s own "Scarlett"
section for the added acceptance coverage), 1166 after Command Family
Fallback Precision, item 35 below, 1172 after the Task Catalog
Content slice, item 36 below, and 1273 after CHASTER-01A (OAuth
Connection Foundation), item 37 below, and 1625 after CHASTER-01B,
Increments 1-3 (Provider Observation Log + On-Demand Fetch Service),
item 38 below, 1632 after Milestone C local prerequisites (Cloudflare
Tunnel config template + local callback-listener readiness check),
item 39 below, 1660 after the `chaster status` command +
channel-agnostic Embed support, item 40 below, and 1744 after the
self-healing Chaster token handling + `chaster disconnect`, item 41
below. Noted here rather than
silently updated each time, since letting a stale count stand
unremarked is exactly the kind of documentation drift this project's
own "Roadmap" section already guards against.

Twenty-two sequential database migrations are applied so far
(`database/migrations/001` through `022`), covering the initial Phase 0
schema, the transactional outbox, Trust Manager, the trust
recalculation pipeline, Penalty Engine, the startup lease, Extension,
Recovery Plan, Recovery Credit, Goal Management, the application
layer's user identity bookkeeping, the plugin infrastructure's own
scoped migration tracking, Discord onboarding/user preferences, (014)
the Task Catalog reference layer, (015/016) its consent/timestamp
audit columns, (017) Advanced Mode's `OperatingMode` singleton and
two-stage transition process, (018) its `INVALIDATED` status
(a `source_mode` mismatch found under direct review), (019) Lock
State's append-only, user-reported `lock_reports` table, (020)
Task Runtime's `task_assignments` table (a database-level composite
foreign key to `task_template_versions` plus a partial unique index
enforcing at most one active assignment per user) together with Task
Catalog's own new `lock_requirement` column, and (021) Task Catalog's
own new `title`/`instructions` columns on `task_template_versions`
(nullable, no fabricated default -- see
`task_catalog/README.md`), and (022) CHASTER-01A's own
`chaster_oauth_states`/`chaster_connections` tables (both nullable-
column-free, no fabricated defaults, encrypted-token TEXT columns --
see `chaster/README.md`). See
[`database/migrations/README.md`](database/migrations/README.md) for
the hard rule migrations must follow (never destructive to user data).

**User-verified working on Windows with Python 3.14.6** (see the
"Python version" note above) — the full suite, a real Discord Gateway
connection, and an end-to-end DM session (including onboarding) have
all been confirmed there.

### Implementation order so far

1. ~~`infrastructure/clock.py` -- `Clock`, `SystemClock`, `FrozenClock`~~ **done**
2. ~~Database wrapper (the transactional `apply_transition` helper)~~ **done**
3. ~~`domain_events` schema + transactional outbox (claim/publish)~~ **done (Phase 1.4)**
4. ~~Consumer framework (dispatch by `event_type`)~~ **done (Phase 2.4)**
5. ~~Startup orchestrator (`on_system_startup()`, `system_startup_lease`)~~ **done (Phase 2.4)**
6. ~~Trust Manager Slice 1 (Domain Registry, Incident, Confirmation, Severity)~~ **done (Phase 2.1)**
7. ~~Trust Manager Slice 2 (score recalculation pipeline)~~ **done (Phase 2.2)**
8. ~~Penalty Engine Slice 1 (state machine, freeze-as-set-of-reasons, natural completion)~~ **done (Phase 2.3)**
9. ~~Extension (`should_extend()`, the unified consumption path)~~ **done (Phase 2.5)**
10. ~~Recovery Plan (lifecycle as a reaction to Penalty Window events)~~ **done (Phase 2.6)**
11. ~~Recovery Credit integration (Penalty Engine consumes `recovery_plan.task_completed`)~~ **done (Phase 2.7)**
12. ~~Focused architectural review + fixes (task-transition guards, `resume()` closing all matching freezes, Recovery Credit idempotency)~~ **done (Phase 2.8)**
13. ~~Goal Management (lifecycle, evidence, evaluation, the `GoalChangeProposal` confirmation mechanism)~~ **done (Phase 2.9)**
14. ~~First usable vertical slice: `application/` + a thin Discord adapter~~ **done (Phase 3.1)**
15. ~~Plugin Infrastructure, Step 1: `plugin_sdk.py` + `plugin_fault_boundary.py`, zero real plugins yet~~ **done**
16. ~~Plugin Infrastructure, Step 2: `plugin_registry.py` (discovery, manifest validation, wiring into `ConsumerRegistry`/`CommandRouter`), zero real plugins yet~~ **done**
17. ~~Plugin Infrastructure, Step 3: `plugins/goal_celebration`, the first real plugin, plus `plugin_migrations.py`~~ **done**
18. ~~Discord onboarding + persistent user preferences: language → AI gender → personality, the 15-identity catalog as approved reference data, `windows/` Task Scheduler deployment~~ **done** — see `docs/architecture/user_onboarding_technical_design.md`.
19. ~~Task Catalog, catalog-layer-only implementation slice: `TaskTemplateVersion`/`TaskTemplateCatalogEntry`, read-only `TaskCatalog` + governance-only `TaskCatalogAdministration`~~ **done** — see `task_catalog/README.md` for the exact boundary (no task instance, no Task Runtime, no role owners assigned).
20. ~~Advanced Mode, `OperatingMode`-only implementation slice: the global singleton, the two-stage `critical_change` transition process (`AdvancedMode` read-only + `AdvancedModeAdministration` write), the new `penalty_engine` transaction-scoped read this required~~ **done** — see `advanced_mode/README.md` for the exact boundary (no `DelegatedAuthorityPolicy`, no Penalty Window max, no tokens, no Hygiene values, no Task assignment).
21. ~~Advanced Mode's transition process wired into Discord DM: `mode`/`mode status`/`mode request advanced`/`mode request standard`/`mode cancel`/`mode confirm`, `IncomingMessage.external_message_id`, explicit settle-before-act orchestration~~ **done** — see `application/README.md` for the exact boundary (this project's first write-capable Discord commands).
22. ~~Conversation Engine, Slice 1 only: runtime types and a deterministic safety shell (`ResponseCategory`, `ConversationContextProvider`/`Fragment`, `ResponseContextSnapshot`, `ResponsePlan`, `ConversationResponse`, structural validation, deterministic fallback)~~ **done** — see `conversation_engine/README.md` for the exact boundary (no LLM, no ordinary conversation, no `ApplicationService` integration, today's Discord behavior completely unchanged).
23. ~~Conversation Engine, Slice 2: real Ollama-backed ordinary conversation for unmatched text only, ticket-based per-subject FIFO queue, transitional in-memory recent-history buffer, `Database`'s thread-local transaction guard fix~~ **done** — see `conversation_engine/README.md` for the exact boundary (no Memory System, no tool calling, no `GOVERNANCE_EXPLANATION`, no provider registry).
24. ~~Memory System, non-persistent Working Memory foundation slice only: `WorkingMemoryRole`/`WorkingMemoryTurn`/`WorkingMemorySnapshot`, `WorkingMemoryReader`/`WorkingMemoryWriter` protocols, `InMemoryWorkingMemory` (process-lifetime, per-subject, atomic whole-exchange commit, oldest-whole-exchange trimming, thread-safe, no FIFO of its own)~~ **done** — see `memory_system/README.md` for the exact boundary (no persistence, no migration, all four remaining memory layers still blocked on an unwritten privacy/consent design).
25. ~~Conversation Engine Slice 3: wired `ConversationEngine` to `memory_system`'s `InMemoryWorkingMemory` via `WorkingMemoryReader`/`WorkingMemoryWriter` (separate, injected dependencies), removed `TransitionalRecentMessageBuffer` entirely, explicit read/write failure policies with distinct log codes for expected vs. unexpected failures~~ **done** — see `conversation_engine/README.md`'s own "Slice 3" section for the exact boundary (same process-lifetime/no-persistence contract as before, mechanical cutover of the same 10-exchange/8000-character limits, no `ConversationContextProvider` integration despite the original design text).
26. ~~Preference & Limits Profile, Foundation Slice 1 only: pure process-independent domain model (`ProfileOwnerKey`, `ProfileTopicId`, `ProfileDisposition`, `ProfileEntry`, `PreferenceProfileSnapshot`, `TopicState`, `resolve_topic_state()`), at-most-one-active-entry-per-topic cardinality enforced constructionally~~ **done** — see `preference_profile/README.md` for the exact boundary (no repository, no persistence, no import/consent/eligibility integration, no age-gate Protocol, zero runtime wiring anywhere in the project).
27. ~~First Testable Keyholder Milestone, Slice A — Lock State: user-reported lock status only (`LockReportStatus`, `LockKnowledgeState` with an explicit `UNKNOWN` absence state, `LockReport`), append-only persistence (migration 019) with a deterministic `sequence_number` ordering tiebreaker, governed read/write split (`LockState`/`LockStateAdministration`)~~ **done** — see `lock_state/README.md` for the exact boundary (user-reported only, never a claim of verified physical reality; no Discord commands, no `ApplicationService`/Conversation Engine integration yet).
28. ~~First Testable Keyholder Milestone, Slice B — Task Runtime: `TaskAssignment` lifecycle (`ACTIVE`/`COMPLETED`/`CANCELLED`), authoritative eligibility enforcement re-derived inside `assign_task()` itself (never trusting a caller-supplied decision), database-level composite foreign key pinning an assignment to its exact immutable template version, database-level partial unique index enforcing at-most-one-active-assignment-per-user; `task_catalog`'s own new `LockRequirement`/`lock_requirement` field and `get_current_version()` read method~~ **done** — see `task_runtime/README.md` for the exact boundary (lock-state eligibility dimension only, no selection algorithm, no Discord/Conversation Engine integration yet). Error classification fixed after review — `TaskAssignmentReferentialIntegrityError` is now distinct from `TaskAssignmentConcurrencyError` (an invalid `user_id` FK is no longer mislabeled as an active-assignment race), and error messages no longer include raw identifiers.
29. ~~First Testable Keyholder Milestone, Slice C — Discord integration for both Slice A and B: `lock status`/`lock report locked`/`lock report unlocked`, `task request`/`task active`/`task complete`/`task cancel`, both as their own `register_family` command families (never falling through to Conversation Engine for the bare family word or an exact command — see item 35 below for a later refinement narrowing exactly which inputs the family fallback itself catches); `LockState`/`LockStateAdministration`/`TaskCatalog`/`TaskRuntime`/`TaskRuntimeAdministration` constructed directly inside `ApplicationService.__init__` (the same pattern `advanced_mode` uses, no DI from `bot/discord_bot.py`); `task_runtime.selection.select_eligible_template()` (deterministic, lowest `template_id`); `scripts/seed_development_tasks.py` (standalone, idempotent, explicitly-invoked maintenance script for two neutral development task templates)~~ **done** — see `application/README.md` for the exact boundary and its own directly-verified invariants (known commands never reach the model, ordinary conversational text never writes domain state, deterministic replies never enter Working Memory).
30. ~~First Testable Keyholder Milestone, Slice D — Authoritative Conversation Context Integration: `ConversationContextProvider.provide_context()` gained an explicit `subject_key` parameter (providers are static/stateless, shared across every subject, verified under real thread concurrency); `LockStateContextProvider`/`ActiveTaskContextProvider` (`conversation_engine/context_providers/`), read-only, always returning a real fragment on a successful read (including `UNKNOWN`/no-active-task — never conflating "successfully determined nothing" with "read failed"); a new deterministic `AUTHORITATIVE APPLICATION STATE` prompt section placed before Working Memory, with template metadata serialized as data, never a new instruction; `bot/discord_bot.py`'s composition root now shares one `LockState`/`TaskRuntime`/`TaskCatalog` instance between the provider graph and `ApplicationService`~~ **done** — see `conversation_engine/README.md`'s own "Slice D" section for the exact boundary (no Operating Mode provider — deferred, since a live-settled canonical read would require a write-side settlement step a read-only provider must never perform; no Chaster/personality/preference integration).
31. ~~Task Catalog Human-Readable Task Content — Candidate B: `title`/`instructions` added to `TaskTemplateVersion` (nullable at the schema level, migration 021; `str | None` on the dataclass, but required non-`None` `str` at the `TaskCatalogAdministration.create_template()`/`add_version()` write boundary — never accepting or writing `None` for a newly created version); `ActiveTaskContextProvider`'s disclosure whitelist and `prompt_builder.py`'s `AUTHORITATIVE APPLICATION STATE` rendering both extended, under the exact same structural trust-boundary guarantees already proven for `completion_requirements` (verified directly with adversarial `title`/`instructions` strings); a legacy row predating this migration renders as an explicit `"(not recorded)"` marker, never fabricated content; the two development seed task templates now carry real, human-readable content~~ **done** — see `task_catalog/README.md`'s own "title/instructions" note and `conversation_engine/README.md`'s own "Candidate B" section for the exact boundary (no richer task ranking/selection, no new eligibility dimensions, no Operating Mode provider, no Chaster, no preferences/limits, no persistent memory, no task-editing UI, no Discord command redesign, no automatic completion, no LLM-driven writes).
32. ~~Deterministic Task Command Human-Readable Content: `task active`/`task request` (both the success and already-active-task replies) now show the assignment's own pinned `TaskTemplateVersion.title` — resolved via `self.task_catalog.get_template(assignment.template_id, assignment.template_version)`, the exact same historical-version invariant `ActiveTaskContextProvider` already enforces, never `current_version`; a legacy `title=None` row renders as the same `"(not recorded)"` marker `prompt_builder.py` already uses, reused rather than reinvented; `instructions` deliberately not shown in any deterministic reply (a terse command reply is the wrong place for a full paragraph); `task complete`/`task cancel` replies (`"Completed."`/`"Cancelled."`) left generic in this slice (extended by item 33 below)~~ **done** — see `application/README.md`'s own note under Slice C for the exact boundary (no change to task selection/ranking/eligibility, task runtime, Task Catalog schema, conversation-engine/prompt behavior, write boundaries, or automatic completion).
33. ~~Title-aware `task complete`/`task cancel` confirmation: extends item 32's own convention to the two remaining `task ...` replies — `"Completed: {title}."`/`"Cancelled: {title}."`, resolving the assignment's own exact pinned `(template_id, template_version)` the same way, the same `"(not recorded)"` marker for a legacy `title=None` row, `instructions` still never shown. `_resolve_task()`'s shared helper (used by both `_handle_task_complete`/`_handle_task_cancel`) reads the active assignment before calling `resolve()`, then resolves the template after a successful transition — the assignment's own pinned version is unaffected by transition timing either way. The state transitions themselves (`ACTIVE → COMPLETED`/`ACTIVE → CANCELLED`) are unchanged — only the confirmation text changed~~ **done** — see `application/README.md`'s own note under Slice C for the exact boundary (same exclusions as item 32: no task selection/ranking/eligibility, task runtime, Task Catalog schema, conversation-engine/prompt behavior, write boundaries, or automatic completion changes).
34. ~~Scarlett — Hybrid Personality Presentation: the first real personality presentation layer. `ResponseContextSnapshot` gained a sixth core field, `identity_id: str`, threaded through `assemble_context()`/`build_response_context()`; `prompt_builder.py`'s system-message assembly was reordered to SYSTEM BOUNDARIES + category rules → **AUTHORITATIVE APPLICATION STATE** → **PERSONALITY / PRESENTATION** → language (personality used to render *before* domain state — the wrong precedence); the old bare "Tone guidance" line was replaced with an explicit, in-prompt-worded `PERSONALITY / PRESENTATION` block stating its own scope (tone/phrasing only, never authority, never overrides authoritative state or system boundaries, task/conversation content stays data regardless of tone) — `_SYSTEM_BOUNDARIES` itself untouched. Reuses the already-existing, already-working per-user onboarding identity-selection mechanism (`application/onboarding_service.py`) rather than inventing a new one — Scarlett is simply one of the 15 catalog identities, fully selectable and correctly wired, with no Scarlett-specific code in `conversation_engine`; deliberately does **not** add any mechanism that pre-selects/defaults a user to Scarlett without that existing choice, since nothing in the current architecture makes that strictly necessary. Uses Scarlett's real approved `CommunicationProfile` (`docs/architecture/ai_identity_technical_design.md` Section 10: warmth 0.5, humor 0.4, teasing 0.5, assertiveness 0.9, formality 0.3, verbosity 0.4), not a differing value set a handoff prompt suggested~~ **done** — see `conversation_engine/README.md`'s own "Scarlett — Hybrid Personality Presentation" section for the exact boundary (no Chaster, no preferences/limits, no persistent personality preferences, no onboarding-flow redesign, no advanced mode, no relationship/decision engine, no new task ranking/eligibility, no `_SYSTEM_BOUNDARIES` changes, no migration).
35. ~~Command Family Fallback Precision: `application/router.py`'s `register_family()`/`route()` family fallback now fires only when the entire trimmed/lowercased input equals the bare family word (`"lock"`, `"task"`, `"mode"`) exactly — never merely because the input starts with it. Fixes a real, concrete UX gap: an ordinary conversational sentence starting with a common English word (`"task is really weighing on me today, can we talk?"`) or a near-miss command typo (`"task compelte"`) used to be swallowed by the deterministic family-invalid reply instead of reaching Conversation Engine; both now fall through as ordinary unmatched text, exactly like any other unmatched multi-word input. Exact-command matching itself (`"task request"`, `"lock status"`, etc.) is completely unaffected — still checked first, before the family lookup ever runs. Deliberately not fuzzy/edit-distance matching, not a heuristic classifier, not model-based intent detection — an exact string-equality check only. `_SYSTEM_BOUNDARIES` untouched~~ **done** — see `application/README.md`'s own note for the exact boundary (no change to personality, Working Memory, lock/task semantics, or any exact command's behavior; the accepted trade-off is that multi-word near-miss typos are no longer auto-corrected by a family reply and instead reach the model as ordinary conversation).
36. ~~Task Catalog Content Proposal, Option A: the first real development task catalog content, added to `scripts/seed_development_tasks.py::_SEED_DEFINITIONS` alongside (not replacing) the two original `dev-seed-*` fixtures. Ten new templates spanning seven categories (`chore`, `organization`, `planning`, `routine`, `focus`, `movement`, `reflection`) — nine `LockRequirement.NONE`, one (`locked-reflection-entry`) `REQUIRES_LOCKED`, preserving the exact same user-reported-only epistemic wording `dev-seed-locked-chore` already established. Content was proposed by Claude, explicitly reviewed against a content-boundary checklist (no sexual/dangerous/self-harm/financial-legal-risk content, no physical lock verification claims, no external services), and approved item-by-item by Jiří before being written — including an explicit A/B decision on whether to keep the one physical-activity item (`short-walk`); Option A (keep) was chosen. Catalog size after seeding: 12 templates, idempotence re-verified~~ **done** — see `task_catalog/README.md`/`scripts/seed_development_tasks.py`'s own comments for the exact content; no change to `task_catalog`/`task_runtime` schema or selection logic, no migration, no new eligibility dimension.
37. ~~CHASTER-01A — OAuth Connection Foundation: a new `chaster/` package (`token_encryptor.py` using `cryptography.fernet.Fernet`, never raw AES-GCM or `ttl=`; `models.py`; `repository.py` for `chaster_oauth_states`/`chaster_connections`, atomic single-use state consumption proven with real concurrent threads; `oauth_client.py` built only from the two officially confirmed Chaster endpoints; `callback_service.py`; `callback_listener.py`, the project's only `aiohttp.web` server), migration 022, a `chaster connect` Discord command wired through the existing deterministic router exactly like `lock`/`task`/`mode`, and `CoachKeyholderBot.setup_hook()`/`close()` starting/stopping the localhost-only callback listener with startup failure explicitly unable to prevent Discord itself from starting. All Chaster HTTP traffic mocked in tests -- no real credentials needed for the 101 new tests. `core/config.py`/`.env.example` extended with clearly-separated non-secret and secret Chaster configuration, all optional. **Known, explicitly flagged limitation**: confirming which Chaster account authorized a connection needs an authenticated identity endpoint this project's research never confirmed and this slice's own instructions forbade guessing -- isolated behind an injectable `resolve_identity` dependency; production wiring always fails safely at this one step until a future slice resolves it. No lock fetching, no provider observations (CHASTER-01B), no change to `lock_state`/`task_runtime`, no `chaster status`/`disconnect`, no emergency-override mechanism (a separate, still-pending hard requirement)~~ **done** -- see `chaster/README.md` for the exact boundary and `docs/architecture/chaster_integration_technical_design.md` for the full design history.
38. ~~CHASTER-01B, Increments 1-3 -- Provider Observation Log + On-Demand Fetch Service: migration 023 (`chaster_provider_observations`, append-only, no foreign key to `lock_reports`, indexed on `(connection_id, fetched_at DESC)`); `chaster/models.py` gained `ChasterProviderStatus` (`locked`/`unlocked`/`deserted` -- never `VERIFIED`) and `ChasterProviderObservation`; new `chaster/provider_observation_repository.py` (read/write split, real concurrent-write safety proven with real threads) and `chaster/provider_observation_service.py` (`ChasterProviderObservationService.fetch_and_record()` -- on-demand only, no scheduler/background thread/asyncio task/timer anywhere; token-expiry check + transparent refresh + persist before the real `GET /locks?status=active` call; returns a `FetchObservationResult` outcome+observation pair, not a bare `Observation | None`, so every Section-14 failure mode -- no connection, needs reauthorization, API unavailable, ambiguous multiple active locks, unparseable status -- stays honestly distinguishable, never collapsed into an assumed `unlocked`). Separately, this same stretch of work fixed a real, previously-blocking transport bug (`chaster/_http.py::send_with_ordered_headers()` -- Chaster's own API/Cloudflare edge rejects `requests`' default header ordering, confirmed by direct real testing) and implemented real identity resolution (`callback_service.py::build_real_identity_resolver()`, evidence-backed by a real, complete `GET /auth/profile` response) -- both `chaster/README.md`'s "Current real-provider readiness" section and the design doc's own diagnostic history cover this in full. CHASTER-01B Increment 4 (a conversation-engine context provider reading these observations) remains explicitly deferred, gated on its own separate decision -- `lock_state`/`task_runtime`/`conversation_engine` untouched throughout~~ **done** -- see `chaster/README.md` for the exact boundary.
39. ~~Milestone C -- Local Prerequisites: confirmed the application-level code Milestone C needs (the `chaster connect` command, the OAuth callback receiver/`ChasterCallbackService`, real identity resolution) was already fully built and tested in earlier slices -- no new application code required. Added `docs/deployment/cloudflare_tunnel_config_template.yml` (a `cloudflared` ingress config template, not runnable as-is -- every real value is an external account-level prerequisite only the deployer can supply; the emergency-unlock listener's own port is structurally impossible to misconfigure into the tunnel, since the template routes exactly one hostname to exactly one port) and `scripts/dev_callback_listener_readiness_check.py` (a dev-only diagnostic that starts the REAL `ChasterCallbackListener` locally, confirms it binds and responds correctly, and shuts down cleanly -- explicitly scoped to what's verifiable without a real tunnel/DNS/Chaster application, never pretending to test those). Milestone C itself remains unreachable -- Cloudflare Tunnel has still never been set up~~ **done** -- see `chaster/README.md`'s own Milestone C section for the exact boundary.
40. ~~`chaster status` command + channel-agnostic Embed support: a new deterministic command (never reaches ConversationEngine) that fetches the connected user's active locks live via the existing, confirmed `ChasterLockClient.list_active_locks()` and displays them. Read-only -- makes exactly one real Chaster call, never a write/unlock endpoint; does not read or write `chaster_provider_observations` (CHASTER-01B's own on-demand fetch service, a structurally separate, not-yet-wired-to-any-command path) -- `chaster status` and `ChasterProviderObservationService` are two independent ways of reading live Chaster data, not the same mechanism. `status`'s raw value is shown exactly as Chaster reports it, never interpreted into a decision. Introduces this project's first-ever Discord Embed usage, via a new, deliberately channel-agnostic `EmbedSpec`/`EmbedField` pair in `application/models.py` (colors are semantic names -- `"red"`/`"blue"`/`"green"` -- never a raw Discord color object; `application/` still imports no channel-specific package, per its own header docstring) -- `bot/discord_bot.py::_build_discord_embed()` is the only place a real `discord.Embed` is constructed, at the adapter boundary. `OutgoingMessage.embed` is optional and defaults to `None`, so every pre-existing command's plain-text reply is completely unaffected~~ **done** -- see `chaster/README.md` for the exact boundary.
41. ~~Self-healing Chaster integration -- expired tokens never require manual database intervention: a new `chaster/token_manager.py` (`ChasterTokenManager`) is now the single place that keeps a user's Chaster access token usable. It refreshes an expired (or within-30-seconds-of-expiry) access token *before* a call using the stored refresh token (`ChasterOAuthClient.refresh_tokens()`, `grant_type=refresh_token`, against the existing confirmed Keycloak token endpoint), and on an HTTP 401 refreshes once and retries once (a second 401 is terminal, never a loop). Failures are classified honestly: a refresh token Chaster definitively rejects (or ciphertext that cannot be decrypted) marks the connection `needs_reauthorization` and produces a red "Chaster Reconnection Required" reply pointing at `chaster connect`; a transient failure (network error, HTTP 429/5xx) leaves the connection `active` and untouched, so an outage can never permanently log a user out. A per-user lock serialises refreshes so a rotating refresh token cannot be spent twice. The refresh logic that previously lived only inside `ChasterProviderObservationService` was *extracted* into this shared component (not duplicated); `chaster status` and the observation service both use it. `chaster connect` no longer refuses an already-connected user -- it issues a fresh authorization link, and the existing connection stays untouched until the callback atomically replaces it. New deterministic `chaster disconnect` command removes the user's stored tokens, provider observations and any pending authorization link in one transaction and answers with a green embed (blue if nothing was connected); it is a **local** removal only, because no Chaster token-revocation endpoint has been confirmed -- the reply says so rather than implying otherwise. To make this possible, `ChasterLockApiError` gained a backward-compatible `status_code` and `ChasterTokenExchangeError` gained `status_code`/`transient`. No schema change (migration 022 already stored both tokens and both expiry timestamps), no change to `lock_state`, `task_runtime`, `_SYSTEM_BOUNDARIES`, or the emergency-unlock plane (which deliberately does not use the token manager)~~ **done** -- see `chaster/README.md` for the exact boundary.

### Roadmap — three explicitly separate tiers

Reflecting a real correction: an earlier revision of this README listed
`memory_system_technical_design.md` under "approved" despite that
document's own header saying the opposite, and undercounted the
migration total by two. Both are fixed here, and this section now
keeps three tiers visibly distinct so that mistake doesn't recur
silently:

**1. Just implemented (this revision):** Advanced Mode's `OperatingMode`-only
slice — the global singleton and its two-stage `critical_change`
transition process (`docs/architecture/advanced_mode_technical_design.md`;
**the document as a whole remains draft, not approved for
implementation** — only the specific slice `advanced_mode/README.md`
itself describes has actually been built; item 20 above). Required one
small, confirmed-before-implementation change outside the new module:
`penalty_engine`'s own `get_active_or_frozen_penalty_window_in_transaction(tx)`.

**Previously implemented, same standard:** Task Catalog's catalog-layer
slice — versioned, append-only task templates and their governance
(`docs/architecture/task_catalog_technical_design.md`; **the document
as a whole remains draft, not approved for implementation** — only the
specific slice `task_catalog/README.md` itself describes has actually
been built; item 19 above).

**3. Drafts awaiting their own separate approval — not a queue of
what gets built next:**
- **Chaster integration — `planned / research` only, NOT implemented.**
  Full research findings, epistemic model, credential/security model,
  and the proposed first slice (**CHASTER-01**: OAuth connection +
  read-only, on-demand provider state) are written up in
  [`docs/architecture/chaster_integration_technical_design.md`](docs/architecture/chaster_integration_technical_design.md)
  — **draft, not approved for implementation**, same status
  convention as every other document in this section. `core/config.py`'s
  existing `chaster_api_token` field remains exactly what it always
  was: an unread, scaffolded placeholder (`# Phase 7`), not an active
  integration. CHASTER-01 remains **proposed / awaiting approval** —
  several explicit decisions (encryption library, exact schema DDL,
  Cloudflare Tunnel's exact free-tier domain requirement) are still
  open, listed in the document's own closing sections.
- [`docs/architecture/plugin_architecture_proposal.md`](docs/architecture/plugin_architecture_proposal.md)
  (v1.5) — **draft architectural proposal, NOT approved for
  implementation as a whole** (the document's own header: *"No code
  implemented or modified"* — a description of the document itself,
  not of what has since been built around it), *except* for Steps 1–3
  (registry, event bus, DI, permissions, config, fault isolation),
  implemented — see `infrastructure/README.md`. Section 26's own Open
  Question 6 (transaction-aware SDK read methods) has a **decided
  direction, not yet built** — explicit `_in_transaction`-suffixed
  read method variants, mirroring `publish_event`/
  `publish_event_in_transaction` — the document's own v1.5 note
  records that direction but explicitly changed no code doing so.
- [`docs/architecture/memory_system_technical_design.md`](docs/architecture/memory_system_technical_design.md)
  (v1.4) — **draft, NOT approved for implementation as a whole**,
  *except* for the non-persistent Working Memory foundation slice
  `memory_system/README.md` describes (item 24 above) — process-lifetime,
  per-subject, in-memory only, now wired into Conversation Engine
  Slice 3 (item 25 above). The five memory layers as a whole, a
  single-source-of-truth table per information category, an explicit
  ownership model, and any persistent memory of any kind remain fully
  open, unimplemented, and unapproved — persistent memory specifically
  blocked on a privacy/consent design that does not yet exist (see
  `memory_system/README.md`'s own audit).
- [`docs/architecture/relationship_decision_engine_technical_design.md`](docs/architecture/relationship_decision_engine_technical_design.md)
  (v1.1) — **draft, NOT approved for implementation.** How Domain
  State becomes one unified `Decision`, via a Relationship Engine
  (Coach/Keyholder as two interpretive perspectives, not two
  independent agents) and a Decision Engine (Entitlement Classes, the
  Hidden Token Economy).
- [`docs/architecture/ai_identity_technical_design.md`](docs/architecture/ai_identity_technical_design.md)
  (v1.0) — **draft, NOT approved for implementation as a whole**,
  *except* for Sections 3 and 10 (the 15-identity catalog and the six
  communication-profile values, cited by `user_onboarding_technical_design.md`
  as approved, stable reference data), and *except* for a separate,
  narrow, explicitly-approved slice — **Scarlett / Hybrid Personality
  Presentation** (item 34 below) — that renders a selected identity's
  `CommunicationProfile` as presentation-only tone/phrasing guidance
  for ordinary free-form coaching dialogue. This is **not** the full
  communication pipeline the document describes: `Decision` phrasing
  from a Relationship/Decision Engine, Explanation Fidelity, and
  Situational Constraint detection (Sections 5/6, and the document's
  own Open Questions 1–3) remain fully unapproved and unbuilt — no
  such engine exists yet to phrase anything from.
- [`docs/architecture/advanced_mode_technical_design.md`](docs/architecture/advanced_mode_technical_design.md)
  (v1.0) — **draft, NOT approved for implementation as a whole**,
  *except* for Section 2 (`OperatingMode` itself) and Section 11 (the
  transition state machine), implemented for exactly that slice, item
  20 above (with two refinements found during implementation review —
  see the document's own notes at the start of each section). Every
  other section — `DelegatedAuthorityPolicy`, the token transparency
  exception, `MAX_TARGET_ACTIVE_HOURS` as a function of mode, Hygiene
  values, Carry Bank, Task Runtime conditions — remains fully open,
  unimplemented, and unapproved.
- [`docs/architecture/task_catalog_technical_design.md`](docs/architecture/task_catalog_technical_design.md)
  (v1.0) — **draft, NOT approved for implementation as a whole**,
  *except* for the specific catalog-layer slice `task_catalog/README.md`
  describes (TC-1/TC-2/TC-4/TC-8's data shapes) — approved and
  implemented for exactly that slice, item 19 above. Since extended,
  as part of item 28 (First Testable Keyholder Milestone, Slice B):
  `TaskTemplateVersion`'s own new `LockRequirement`/`lock_requirement`
  field (owned here, not in `task_runtime`, per that slice's own
  ownership rationale) and `TaskCatalog.get_current_version()` — see
  `task_runtime/README.md` for why. Extended again as part of item 31
  (Task Catalog Human-Readable Task Content — Candidate B):
  `TaskTemplateVersion`'s own new `title`/`instructions` fields
  (migration 021) — see `task_catalog/README.md`'s own note for the
  exact write-boundary invariant (required `str` at the write API,
  `str | None` only at the dataclass level for legacy-row
  readability). Everything else in the document
  (which future domain owns each `TaskInstanceRole`, whether a Task
  Runtime beyond Slice B's own minimal lifecycle should ever exist,
  `SourceReference`, `binding_conditions_snapshot`) remains fully
  open, unimplemented, and unassigned.
- [`docs/architecture/conversation_engine_technical_design.md`](docs/architecture/conversation_engine_technical_design.md)
  (v1.0) — **draft, NOT approved for implementation as a whole**,
  *except* for Slices 1 through 3 specifically (runtime types and
  deterministic safety shell, Ollama-backed ordinary conversation for
  unmatched text only, Working Memory integration — items 22, 23, and
  25 above). Slices 4 through 6 (structured domain facts/governance
  explanations, provider registration maturity, a governed Tool
  Interface) remain fully open, unimplemented, and unapproved — see
  `conversation_engine/README.md` for the exact, currently-true
  boundary of what Slices 1–3 actually cover.
- [`docs/architecture/preference_limits_profile_technical_design.md`](docs/architecture/preference_limits_profile_technical_design.md)
  (v1.0) — **draft, NOT approved for implementation as a whole**,
  *except* for the pure, process-independent Foundation Slice 1
  `preference_profile/README.md` describes (item 26 above) — zero
  runtime wiring anywhere in the project, fail-closed blocked from any
  user-accessible integration until a separately approved
  age/eligibility design exists. Everything past Foundation Slice 1
  (an in-memory repository, import/consent/eligibility integration)
  remains fully open, unimplemented, and unapproved.
- [`docs/architecture/lock_state_technical_design.md`](docs/architecture/lock_state_technical_design.md)
  (v1.0) — **draft, NOT approved for implementation as a whole**,
  *except* for the Foundation Slice `lock_state/README.md` describes
  (user-reported lock status only, never a claim of verified physical
  reality — item 27 above), now wired into Discord as part of item 29.
  Any external/hardware verification or provider integration (e.g. a
  Chaster-style platform) remains fully open, unimplemented, and
  unapproved.
- [`docs/architecture/task_runtime_technical_design.md`](docs/architecture/task_runtime_technical_design.md)
  (v1.0) — **draft, NOT approved for implementation as a whole**,
  *except* for Slice B `task_runtime/README.md` describes (`TaskAssignment`
  lifecycle, authoritative eligibility enforcement, database-level
  composite FK and partial unique index — item 28 above), now wired
  into Discord as part of item 29. Selection/ranking beyond the
  current deterministic placeholder, the preference/limits eligibility
  dimension, Conversation Context integration, and any external
  provider integration remain fully open, unimplemented, and
  unapproved.

**Already approved and implemented, not a draft:**
[`docs/architecture/user_onboarding_technical_design.md`](docs/architecture/user_onboarding_technical_design.md)
(v1.0, done in full — see its own Section 9).

`philosophy.md` v1.16 already reflects one decision from this line of
work (Section 4.2: the Hidden Token Economy replaces an earlier,
never-implemented visible-token model) — see its own revision history
for the reasoning.

### Phase 0 foundation (unchanged since the original design)

- Directory structure (`core/`, `ai/`, `database/`, `bot/`,
  `integrations/`, `observations/`).
- `database/models.py` -- the original dataclass contracts
  (ContextSnapshot, CoachAssessment, KeyholderAssessment,
  DecisionResult, ObservationRecord, Rule, ConsentRecord, ...).
- `database/migrations/001_initial_schema.sql` -- the SQLite schema
  (hybrid: normalized fields + JSON, rule versioning, consent_log,
  observations).
- `database/database.py` -- the access layer (migrations + save/get for
  every entity), later rebuilt in Phase 1.2 to compose on top of
  `infrastructure.database.Database` rather than managing its own
  connections.
- `core/config.py` -- loading configuration and secrets from `.env`.
- `database/backup.py` -- backups via SQLite's online backup API, at
  most 1 automatic daily backup, always a backup before applying a
  migration (if the DB already had some schema version), simple
  rotation (default: the 14 most recent backups,
  `BACKUP_RETENTION_COUNT` in `.env`).
- `bot/discord_bot.py` -- a basic Discord bot: connects, logs messages
  to short-term memory, **no AI logic yet** (arrives in Phase 1+).

All of the above was tested end-to-end (round-trip save/load for every
entity, bot import and initialization, backup scenarios including the
daily limit, pre-migration backups, and rotation).

**Data and backup policy (verified):**
- User data (`data/coach_keyholder.db`, `data/backups/`) is outside git
  (`.gitignore`) -- updating the application (`git pull`) never touches
  it.
- Migrations are exclusively additive (see
  `database/migrations/README.md`).
- The runtime (bot, future core engines) never reads from
  `observations/` or from audit exports -- that's a write-only layer
  from the runtime's perspective.

## Installation

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows (cmd/PowerShell)
pip install -r requirements.txt
```

On Windows, if `python`/`pip` aren't the ones you want on `PATH` (or
you have multiple Python versions installed), the
[`py` launcher](https://docs.python.org/3/using/windows.html#launcher)
often works better and behaves identically for these commands:

```bat
py -m venv .venv
.venv\Scripts\activate
py -m pip install -r requirements.txt
```

**Neither `python` nor `py` is guaranteed to work, though** — found on
a real Windows install: Windows' own "App Execution Alias" feature can
intercept both names with a near-empty stub that opens a "choose an
app"/Microsoft Store prompt instead of running Python at all, if no
real interpreter has been installed through a path that takes priority
over that stub. If either command does this to you instead of running
Python, see [`windows/README.md`](windows/README.md)'s "Interpreter
detection" section — `windows/run_bot.ps1` finds a real interpreter
itself, robust against exactly this.

For development and running tests:

```bash
pip install -r requirements-dev.txt
pytest
```

```bat
:: Windows, via the py launcher (see the note above if this doesn't work)
py -m pip install -r requirements-dev.txt
py -m pytest
```

**Python version:** developed against 3.13 (the code uses
`enum.StrEnum` and modern typing — it would work on 3.11+, but 3.13
was the original agreed target). **User-verified working on Windows
with Python 3.14.6** (`discord.py==2.7.1`, full test suite —
435 passed — and a real end-to-end Discord DM session, all confirmed
by the person running this project; not independently re-verified in
this sandbox, which only has 3.12.3 available). No code change was
needed for 3.14 support — `enum.StrEnum`/modern typing are unaffected,
and `discord.py`'s own `audioop`-removal workaround (Python 3.13+
removed the `audioop` stdlib module entirely, PEP 594) is handled
internally by `discord.py>=2.7.1` itself via a conditional dependency
on `audioop-lts` — nothing this project's own `requirements.txt` needs
to add explicitly. If you hit an `audioop`-related `ModuleNotFoundError`
regardless, `pip install audioop-lts` resolves it (its `cp313-abi3`
wheel works on 3.13 and 3.14 both, via Python's stable ABI).

**No CI currently exists in this repository** (no `.github/workflows/`
or equivalent) — noted here rather than silently added, since setting
one up is a real, separate decision, not a side effect of a Python
version bump. If/when one is added, it should exercise both the
declared floor (3.11, where `enum.StrEnum` first exists) and the
current target (3.14) — not only whichever version a contributor
happens to have installed locally — so a change that accidentally
breaks compatibility with the floor is caught before it ships.

## Configuration

```bash
copy .env.example .env
```

Fill in at least `DISCORD_TOKEN` in `.env` (Discord Developer Portal ->
your application -> Bot -> Token). The bot needs to be invited to a
server with permission to read and send messages, and **Message
Content Intent** needs to be enabled in the Developer Portal (without
it the bot won't see message content -- `discord_bot.py` relies on it).

## Running

```bash
python -m bot.discord_bot
```

```bat
:: Windows, via the py launcher (see the "Neither python nor py is
:: guaranteed to work" note above -- if this doesn't run the bot,
:: use windows\run_bot.ps1 instead, which finds a real interpreter
:: itself)
py -m bot.discord_bot
```

```bat
:: Windows, robust alternative -- recommended if the above doesn't work
.\windows\run_bot.ps1
```

On first run, `data/coach_keyholder.db` is created automatically,
migrations are applied, and `on_system_startup()` runs (Trust Manager/
Penalty Engine/Recovery Plan/Goal Management recovery, then the outbox
publisher) before the bot connects to Discord.

Send the bot a direct message. **A brand-new user is walked through
onboarding first** (language → AI voice → personality — see
[`docs/architecture/user_onboarding_technical_design.md`](docs/architecture/user_onboarding_technical_design.md)),
resuming automatically from wherever they left off if the bot restarts
mid-onboarding. Once that's done, the supported commands are:
`help` (lists commands), `status` (reports the current Penalty
Window, if any — a real read against real domain state), and
`preferences` (shows your saved onboarding choices) — plus Advanced
Mode's own `mode`/`mode status`/`mode request advanced`/
`mode request standard`/`mode cancel`/`mode confirm` (see
`advanced_mode/README.md` for the underlying mechanism; this project's
*first* write-capable Discord commands — everything before this only
read). Anything else gets a polite "I don't recognize that yet." Only
DMs are processed; messages in a server channel are ignored. See
[`application/README.md`](application/README.md) for the full
boundary between the Discord adapter and the channel-agnostic
application layer underneath it, and what's deliberately not built
yet (no Coach/Keyholder reasoning, no LLM, no write-capable command
outside Advanced Mode's own transition).

**Manually testing the mode transition** (`py -m bot.discord_bot`,
then DM the bot):
1. `mode` — confirms you start in Standard, no pending request.
2. `mode request advanced` — creates the request; explains the
   24-hour wait and second confirmation.
3. `mode status` — shows the pending request's exact state.
4. `mode cancel` — cancels it; mode remains unchanged.

The full transition (24-hour wait → `AWAITING_CONFIRMATION` →
`mode confirm`) is verified by automated tests using a `FrozenClock` —
the production Discord flow has no time shortcut, so manually testing
the complete end-to-end transition means actually waiting 24 real
hours between `mode request advanced` and `mode confirm`.
**Want the bot to start automatically on Windows** instead of running
it manually every time? See [`windows/README.md`](windows/README.md)
for Task Scheduler setup (optional, not required for development).

## Structure

```
core/            # coach_engine, keyholder_engine, decision_engine, config (business logic -- Phase 1+)
ai/              # identity_catalog.py (the 15-identity reference catalog for onboarding --
                 # see docs/architecture/user_onboarding_technical_design.md); everything else
                 # here (ollama_client, the actual communication pipeline) remains Phase 1+
database/        # models.py, database.py, migrations/
infrastructure/  # shared cross-cutting layer (Clock, Database, Outbox,
                 # Consumer Registry, Startup Lease, Plugin SDK/Fault
                 # Boundary -- see infrastructure/README.md)
trust_manager/   # first domain module (Slice 1+2 -- see trust_manager/README.md)
penalty_engine/  # second domain module (Slice 1 + Extension + Recovery Credit -- see penalty_engine/README.md)
recovery_plan/   # third domain module (see recovery_plan/README.md)
goal_management/ # fourth domain module, first independent of the Trust
                 # Manager -> Penalty Engine -> Recovery Plan branch
                 # (see goal_management/README.md)
task_catalog/    # versioned task template reference layer -- catalog only,
                 # no task instances, no runtime owner assigned to most roles;
                 # now includes human-readable title/instructions content
                 # (see task_catalog/README.md)
advanced_mode/   # OperatingMode global singleton + two-stage critical_change
                 # transition process -- no DelegatedAuthorityPolicy, no other
                 # part of the wider Advanced Mode draft (see advanced_mode/README.md)
conversation_engine/  # Slices 1-3 -- runtime types, deterministic safety shell,
                 # real Ollama-backed conversation for unmatched text only, and
                 # Working Memory integration via memory_system (see conversation_engine/README.md)
memory_system/   # non-persistent Working Memory foundation slice, now wired into
                 # Conversation Engine Slice 3 -- process-lifetime, per-subject,
                 # in-memory; no persistence, no migration (see memory_system/README.md)
preference_profile/  # Foundation Slice 1 only -- pure, process-independent domain
                 # model (owner/topic/disposition/entry/snapshot, precedence
                 # policy); zero runtime wiring anywhere, fail-closed blocked
                 # until a separate age/eligibility design is approved
                 # (see preference_profile/README.md)
lock_state/      # First Testable Keyholder Milestone, Slices A+C -- user-reported
                 # lock status only, never a claim of verified physical reality;
                 # append-only persistence, now wired into Discord via
                 # application/service.py (see lock_state/README.md)
task_runtime/    # First Testable Keyholder Milestone, Slices B-C -- what was
                 # assigned to a specific user and its lifecycle; authoritative
                 # eligibility enforcement, database-level composite FK and
                 # partial unique index, deterministic (non-scoring) selection,
                 # now wired into Discord via application/service.py
                 # (see task_runtime/README.md)
application/     # channel-agnostic application layer: IncomingMessage/OutgoingMessage,
                 # UserService, OnboardingService, CommandRouter, ApplicationService
                 # (see application/README.md)
scripts/         # standalone, explicitly-invoked maintenance scripts -- never
                 # wired into bot/discord_bot.py's own startup or a migration;
                 # seed_development_tasks.py seeds two neutral dev/test task
                 # templates, idempotently
plugins/         # first-party plugins, loaded by infrastructure/plugin_registry.py
                 # (see plugin_architecture_proposal.md and infrastructure/README.md)
windows/         # Task Scheduler deployment for a personal Windows install
                 # (see windows/README.md) -- entirely optional, not needed to run the bot manually
system/          # composition layer: startup orchestrator, cross-module wiring (see system/README.md)
docs/architecture/  # architecture baseline: system_state_machine.md,
                     # seven domain technical designs, implementation_conventions.md,
                     # domain_events_catalog.md
bot/             # discord_bot.py, approval_flow.py (Phase 6)
integrations/    # chaster.py, apple_health.py (Phase 7)
observations/    # audit export (write-only from the runtime's perspective -- Phase 3+)
tests/           # pytest, structure mirrors the packages (tests/infrastructure/, tests/trust_manager/, ...)
philosophy.md    # the project's reference principles -- read this first
```

## Development history

Each phase below is summarized briefly; the module-level READMEs
linked above are the authoritative, detailed record of what each one
covers, what's deferred, and any real findings.

- **Phase 0** -- foundational schema, config, backups, a Discord bot
  skeleton with no AI logic yet.
- **Phase 1.1** -- `infrastructure/clock.py` (`Clock`/`SystemClock`/`FrozenClock`)
  and the repository-wide guard against direct `datetime.now()` calls.
- **Phase 1.2** -- the shared `Database`/`Transaction`/`apply_transition()`
  transactional core, with `database/database.py` rebuilt on top of it.
- **Phase 1.3** -- the architecture baseline (`philosophy.md` v1.12.1
  and the nine architecture documents) moved physically into the
  repository under `docs/architecture/`, plus a consolidated
  `domain_events_catalog.md` with five real cross-document
  inconsistencies found and documented (not yet resolved at the time).
- **Phase 1.4** -- the transactional outbox
  (`infrastructure/outbox.py`, `domain_events` schema): write, claim,
  publish, and consumer-side dedup, all built on `apply_transition()`.
- **Phase 2.1-2.2** -- Trust Manager: Incident registration and
  confirmation, deterministic severity assessment, and the trust score
  recalculation pipeline (evidence -> score, with a bounded
  per-recalculation delta and diminishing-returns confidence).
- **Phase 2.3** -- Penalty Engine: the PenaltyWindow state machine
  (start/freeze/resume/complete), freeze modeled as a set of
  concurrently active reasons rather than a single flag.
- **Phase 2.4** -- the Consumer Framework and Startup Orchestrator
  (`infrastructure/consumer_registry.py`, `system/startup.py`), and the
  first real, working cross-module event subscription (Trust Manager ->
  Penalty Engine). This is where a genuine architectural constraint
  (`NestedTransactionError`, arising when two modules share one
  database core's single-open-transaction guard) was discovered and
  resolved by making event payloads carry everything a consumer needs,
  rather than letting a handler call back into another module's public
  API mid-transaction. See `system/README.md` for the full account --
  this became a standing rule applied to every event added since.
- **Phase 2.5** -- Extension (`should_extend()`), unifying
  window-starting and window-extending into one consumption path, with
  every TBD parameter from the architecture document flagged explicitly
  as this implementation's own default rather than an architectural
  decision.
- **Phase 2.6** -- Recovery Plan, reacting purely to Penalty Engine's
  own events. Wiring it as a second downstream consumer surfaced a
  second real finding: `process_pending_events()` originally processed
  only one batch per call, so an event cascade (one handler's side
  effect triggering another) wouldn't fully propagate within a single
  `on_system_startup()` call. Fixed by draining the cascade in a loop,
  bounded by `max_cascade_rounds` as a safety limit.
- **Phase 2.7** -- Recovery Credit integration: Penalty Engine consumes
  `recovery_plan.task_completed` and decides, independently, how many
  hours a completed task actually earns against the window's capacity.
  This closes the full Incident -> Assessment -> Penalty -> Recovery ->
  Credit -> Penalty Adjustment lifecycle described in
  `penalty_window_technical_design.md` Section 3.4.
- **Phase 2.8** -- a focused architectural review after the milestone
  above (deliberately scoped to logical contradictions, invalid-state
  risks, module coupling, and expensive-to-reverse decisions -- not a
  full style/naming pass) found and fixed three real bugs: task
  transitions with no status guard (`recovery_plan/README.md`), `resume()`
  closing only the most recently opened freeze period instead of every
  matching one, and an idempotency asymmetry in Recovery Credit's
  direct-call path (both `penalty_engine/README.md`). A fourth finding
  -- composition-layer coupling via underscore-prefixed method calls in
  `system/startup.py` -- was deliberately left as documented, not
  restructured (`system/README.md`), consistent with this project's
  standing rule against designing an abstraction before the pattern
  has repeated enough times to reveal its actual shape.
- **Phase 2.9** -- Goal Management: the fourth domain module, and the
  first independent of the Trust Manager -> Penalty Engine -> Recovery
  Plan -> Recovery Credit branch (it reads nothing from the Trust
  Manager for any decision of its own). Covers the Goal lifecycle
  (create/pause/resume/complete/archive), append-only GoalEvidence and
  GoalEvaluation, and the GoalChangeProposal confirmation mechanism that
  gates adaptation/replacement/abandonment behind an explicit,
  content-bound user decision. Two real gaps were found in the
  architecture document while implementing it -- `GoalInterventionType`
  has no value for proposing goal completion, despite the document's
  own prose requiring one, and the lifecycle diagram leaves adaptation
  from a PAUSED goal ambiguous -- both resolved with the
  least-presumptuous reading available and flagged, not silently
  decided (`goal_management/README.md`). GoalAccountabilityAssessment,
  GoalNegotiation, and the actual Trust Manager integration are
  deferred -- they depend on Coach/Keyholder reasoning this system does
  not have built yet.
- **Phase 3.1** -- the first usable vertical slice: a real, minimal path
  from a Discord direct message all the way to a domain module's data
  and back. Introduced `application/`, a channel-agnostic layer
  (`IncomingMessage`/`OutgoingMessage`, `UserService`, `CommandRouter`,
  `ApplicationService`) that `bot/discord_bot.py` -- rewritten as a thin
  adapter -- is the only thing that knows about Discord specifically.
  Two explicit, read-only commands (`help`, `status`); `status` reads
  `PenaltyEngine.get_active_or_frozen_penalty_window()` directly,
  proving the whole pipe against real data. Two real gaps were found
  and fixed along the way: `on_system_startup()` had never actually
  been wired into any running process before this phase, despite
  `system_state_machine.md` requiring it since Section 7 was written;
  and the adapter's first draft coupled audit logging with reply
  generation, so a logging failure silently replaced a real reply with
  the generic error message. See `application/README.md` for the full
  adapter/application-layer boundary, the supported message flow, and
  what's deliberately not built yet (Coach/Keyholder reasoning, an LLM,
  any write-capable command outside Advanced Mode's own transition,
  multi-user support in the domain modules).
