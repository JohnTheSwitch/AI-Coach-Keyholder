# Chaster

Canonical design: `docs/architecture/chaster_integration_technical_design.md`
(**`Draft for review`** — HTTP callback architecture and the
encryption/key-management architecture are approved at the
architecture level; this README describes exactly what has actually
been implemented: **CHASTER-01A** (OAuth Connection Foundation, plus
the real emergency-unlock safety plane) and **CHASTER-01B, Increments
1–3** (the provider observation log and its on-demand fetch service).
**CHASTER-01B Increment 4** (a conversation-engine context provider
reading these observations) is explicitly **not** built — see
"What is NOT implemented" below.

## Epistemic invariant — read this first

> **Chaster-reported state is `PROVIDER_REPORTED`/`ChasterProviderStatus`
> state — never `VERIFIED`, and never merged with `lock_state`'s own
> user-reported state.**

Chaster's API can confirm what its own system was told, never whether
a lock is *physically* secured. `ChasterProviderStatus` (`locked` /
`unlocked` / `deserted`) exists specifically so this distinction is
visible at the type level, not just in prose — no `VERIFIED` member
exists, or ever will. Provider observations and `lock_state`'s own
`lock_reports` are stored in structurally separate tables, with no
foreign key between them, and are never reconciled into one combined
value — see `docs/architecture/chaster_integration_technical_design.md`
Section 15 ("Conflict with user-reported state").

## What is implemented — CHASTER-01A (OAuth Connection Foundation)

- **`models.py`** — `ChasterOAuthState`, `ChasterConnection`,
  `ConnectionStatus` (exactly two values: `active` /
  `needs_reauthorization` — every other conceptual state is
  represented structurally: row absence = not connected/disconnected,
  a `chaster_oauth_states` row with no matching `chaster_connections`
  row = authorization in progress, a transparent refresh attempt =
  not a stored state at all). `ChasterConnection.__repr__` is
  overridden to redact both encrypted token fields.
- **`token_encryptor.py`** — `TokenEncryptor`, the ONLY place in this
  codebase that imports `cryptography.fernet`. Fails fast (at
  construction) on a missing/invalid master key — never generates a
  replacement. Deliberately never passes `ttl=` to `Fernet.decrypt()`
  — see the module's own docstring for why.
- **`repository.py`** — `ChasterOAuthStateRepository` (single-use
  `state` values, atomic consume via `Database.transaction()`'s own
  `BEGIN IMMEDIATE`, proven with real concurrent threads in this
  module's own tests) and `ChasterConnectionRepository` (owns the
  encryption boundary — callers only ever see plaintext at this
  layer's own edges, never ciphertext). Token refresh is NOT
  automatic here — `get_decrypted_access_token()` never checks
  expiry itself; `chaster/provider_observation_service.py` (below) is
  the one caller that performs the check-then-refresh-then-persist
  orchestration.
- **`_http.py`** — `send_with_ordered_headers()`, the single shared
  transport every real Chaster HTTP call in this project goes
  through. Exists because Chaster's own API/Cloudflare edge rejects
  `requests`' default header ordering (`Authorization` appended
  last) — confirmed by direct, reproducible, real testing against the
  live endpoint. Reorders `Authorization`/`User-Agent` to the front,
  preserves every other header `requests` itself needs (e.g.
  `Content-Type` for a form body) unchanged.
- **`oauth_client.py`** — `ChasterOAuthClient`, built only from the
  two officially confirmed endpoints (Authorization, Token) plus the
  confirmed `GET /auth/profile` (`fetch_raw_profile()` — used by both
  the real identity resolver below and a manual diagnostic script;
  see its own docstring). Never logs or echoes a code/token/secret in
  any exception message.
- **`callback_service.py`** — `ChasterCallbackService`, the
  framework-agnostic orchestration: consume state → exchange code →
  resolve identity → persist. **Identity resolution is now real**:
  `build_real_identity_resolver()`, evidence-backed by a real,
  complete, verbatim `GET /auth/profile` response (see the design
  doc's own full diagnostic history), reads exactly the two fields
  `ChasterIdentity` needs (`_id`, `username`) out of the ~90-field
  real response — nothing else. Fails closed on a missing/wrong-type
  `_id`. `unconfirmed_identity_resolver` (the old, always-raising
  placeholder) remains in the module as a test fixture only — the
  same pattern `UnwiredEmergencyUnlockProvider` already established.
- **`callback_listener.py`** — `ChasterCallbackListener`, the ONLY
  `aiohttp.web` server in this project. One route
  (`/oauth/chaster/callback`), localhost-only bind. Started/stopped
  by `bot/discord_bot.py::CoachKeyholderBot.setup_hook()`/`close()` —
  a startup failure here is caught and logged, never allowed to
  prevent Discord itself from starting.

## What is implemented — CHASTER-01B, Increments 1–3

- **Migration `023`** — `chaster_provider_observations`
  (`id`, `connection_id` REFERENCES `chaster_connections(user_id)`,
  `provider`, `chaster_lock_id` nullable, `status`, `fetched_at`,
  `created_at`), plus `idx_chaster_provider_observations_connection_fetched`
  on `(connection_id, fetched_at DESC)` for the one read pattern that
  matters ("the most recent observation for this connection").
  Append-only, structurally separate from `lock_reports` (migration
  019) — no foreign key between them, confirmed directly via
  `PRAGMA foreign_key_list`, not just by design intent.
- **`models.py`** — `ChasterProviderStatus` (exactly `locked` /
  `unlocked` / `deserted`, matching the confirmed official
  `LockStatusEnum` — see the epistemic invariant above) and
  `ChasterProviderObservation`.
- **`provider_observation_repository.py`** — `ChasterProviderObservations`
  (read-only, `get_latest()`) and `ChasterProviderObservationRecorder`
  (append-only write — always an INSERT, never an UPDATE/DELETE).
  Real concurrent-write safety proven with real threads, the same
  discipline `chaster_oauth_states`' own tests already established.
- **`provider_observation_service.py`** — `ChasterProviderObservationService.fetch_and_record()`,
  the on-demand fetch workflow (connection lookup → token-expiry
  check → transparent refresh + persist if needed →
  `ChasterLockClient.list_active_locks()` → parse → record). **No
  scheduler, no background thread, no asyncio task, no timer** — a
  single synchronous call, triggered by a future caller (a Discord
  command, not built in this increment). Returns
  `FetchObservationResult` (an outcome + the best available
  observation), not a bare `Observation | None` — Section 14's
  failure taxonomy needs to stay distinguishable at the type level:
  - `RECORDED` / `ZERO_ACTIVE_LOCKS` — a fresh observation.
    Zero active locks is recorded as `unlocked` — a real, positive
    report *from* the provider, structurally different from
    inferring "unlocked" from missing/failed data.
  - `NO_CONNECTION`, `NEEDS_REAUTHORIZATION`, `API_UNAVAILABLE` — the
    live fetch could not happen or the token could not be refreshed;
    `observation` carries the **last known** observation (its own
    real `fetched_at`, never rewritten to look fresh) if one exists,
    `None` otherwise. None of these ever imply `unlocked`.
  - `AMBIGUOUS_MULTIPLE_LOCKS` — more than one active lock; the
    model holds exactly one lock's status per row, so this fails
    closed rather than picking one arbitrarily (mirrors
    `emergency_unlock_provider.py`'s own established discipline for
    a structurally different reason).
  - `UNPARSEABLE_STATUS` — the real `status` field's value didn't
    match any of the three confirmed enum members. This project has
    directly confirmed the field's *type* (`str`) from a real
    response, but never directly confirmed every real *value* always
    matches the documented enum — an unexpected value is an honest
    parse failure, never guessed at or coerced to a default.

## What is implemented — `chaster status` command

A deterministic Discord command (never reaches `ConversationEngine`)
that shows the connected user's active locks. Read-only — makes
exactly one real Chaster call (`ChasterLockClient.list_active_locks()`),
never a write/unlock endpoint.

**Structurally independent from `ChasterProviderObservationService`
(CHASTER-01B Increment 3) and `chaster_provider_observations`** — this
command fetches live, on the spot, and does not read or write that
table at all. The two are separate, independent ways of reading live
Chaster data, not the same mechanism; neither calls the other.

- Not connected (no `chaster_connections` row): a red "Chaster Account
  Not Connected" reply pointing at `chaster connect`.
- Connection can no longer be renewed (Chaster rejected the refresh
  token, the stored ciphertext cannot be decrypted, or a freshly
  refreshed token was still rejected): a red "Chaster Reconnection
  Required" reply pointing at `chaster connect`. (Previously a
  decryption failure showed "Not Connected"; it is now this more
  accurate reply.)
- Token endpoint temporarily unreachable (network error, HTTP 429/5xx):
  a red "Chaster Temporarily Unavailable" reply; the connection is
  left intact.
- Connected, zero active locks: a blue "no active locks" reply.
- Connected, active locks found: a green reply, one field per lock
  (`title`, `status`, and a computed "time remaining" — `Xd Yh Zm`,
  `Expired`, or `Unlimited / Permanent` for a missing/unparseable
  `endDate`). `status`'s raw string is shown exactly as Chaster
  reports it — never interpreted into any kind of pass/fail decision,
  the same display-only discipline `ChasterProviderObservationService`
  already applies for a different, structural reason.
- A Chaster API failure (network error or any non-2xx response): a
  red "Chaster API Error" reply.

**First use of a Discord Embed anywhere in this project.** Rather than
making this command Discord-specific, `application/models.py` gained a
small, deliberately channel-agnostic `EmbedSpec`/`EmbedField` pair —
colors are semantic names (`"red"`/`"blue"`/`"green"`), never a raw
Discord color object — and `OutgoingMessage.embed` is optional,
defaulting to `None`. `application/` still imports no channel-specific
package at all, per its own header docstring; `bot/discord_bot.py::_build_discord_embed()`
is the only place a real `discord.Embed` is ever constructed, at the
adapter boundary, and every pre-existing command's plain-text reply is
completely unaffected by this addition.

## What is implemented — self-healing tokens, `chaster connect` re-link, `chaster disconnect`

**Goal: an expired token never requires manual database intervention.**

`chaster/token_manager.py::ChasterTokenManager` is the single place
that keeps a user's access token usable:

1. **Pre-call refresh.** If the stored access token is expired or
   within 30 seconds of expiring, the stored refresh token is exchanged
   at Chaster's token endpoint (`ChasterOAuthClient.refresh_tokens()`,
   `grant_type=refresh_token`; the endpoint is the existing confirmed
   Keycloak `TOKEN_URL`) and the new pair plus both expiry timestamps
   are persisted. No schema change was needed: migration 022 already
   stores `encrypted_refresh_token`, `access_token_expires_at` and
   `refresh_token_expires_at`.
2. **401 retry.** `call_with_token(user_id, now=, operation=)` runs the
   operation; if it fails with `ChasterLockApiError.status_code == 401`
   it refreshes once and retries once. A second 401 marks the
   connection `needs_reauthorization` — never a loop.
3. **Honest failure classification.**
   - `ChasterReauthorizationRequiredError` — Chaster definitively
     rejected the refresh token (`ChasterTokenExchangeError.transient`
     is `False`), the ciphertext cannot be decrypted, or a just-issued
     token was rejected. The connection is marked
     `needs_reauthorization`; later calls short-circuit with no
     network traffic until the user runs `chaster connect`.
   - `ChasterRefreshUnavailableError` — network error or HTTP 429/5xx
     (`transient=True`). The stored connection is **not** modified, so
     the next attempt can succeed on its own.
   - `ChasterNotConnectedError` — no connection row.

Design notes: the refresh token's own expiry is intentionally *not*
pre-checked locally (Keycloak offline tokens report
`refresh_expires_in` of 0); the token endpoint is the only authority.
A per-user `threading.Lock` serialises refreshes so a rotating refresh
token is never spent twice, and the connection is re-read inside the
lock so a caller that lost the race reuses the winner's token.
`ChasterProviderObservationService` previously contained its own copy
of this refresh logic; it now delegates to the same manager (its
`FetchObservationOutcome` values are unchanged, and a 401 is now also
refreshed-and-retried there). The emergency-unlock plane does **not**
use the manager and its behaviour is deliberately unchanged.

**`chaster connect`** no longer blocks an already-connected user: it
issues a fresh single-use authorization link (mentioning the current
connection status). The existing connection is left untouched until
the callback succeeds and replaces it atomically
(`create_or_replace` upserts, resetting the status to `active`), so an
abandoned link cannot destroy a working connection.

**`chaster disconnect`** is deterministic (never reaches
`ConversationEngine`) and **local-only**: no Chaster token-revocation
endpoint has been confirmed, so none is called and the reply says "This
does not notify Chaster". In one transaction
`ChasterConnectionRepository.delete()` removes the user's provider
observations (they `REFERENCE` the connection with foreign keys
enforced, so the connection row cannot be deleted while they exist —
this is the single deliberate exception to that table's append-only
rule), any pending OAuth state (an outstanding link stops working), and
the connection. It returns whether a connection existed. Replies: green
"Chaster Disconnected", or blue "Nothing to Disconnect". `lock_state`
is never touched. Note that after disconnecting, the bot holds no
token for that user, so nothing that needs one (status, observations,
the emergency-unlock plane's live discovery) can act for them until
they reconnect.

## What is implemented — LLM tool `get_chaster_status` (read-only)

`chaster/llm_tools.py` lets the conversation model answer natural
questions ("How much time is left on my lock?", "What's my status?") by
calling one **read-only** tool. This is the explicitly approved lifting
of CHASTER-01B Increment 4's gate **for this tool only**.

- **Tool:** `get_chaster_status`, no parameters, closed JSON schema.
  Any arguments the model sends are ignored (nothing is read from them,
  so they cannot steer the call). Exactly one Chaster request
  (`GET /locks?status=active`) per call. Never `emergency_unlock`, never
  a write, never `lock_state`, `task_runtime` or
  `chaster_provider_observations`. There is no separate `get_active_locks`
  tool: both would hit the same endpoint, and one tool leaves the model
  fewer ways to pick wrong.
- **There is no `ChasterService`.** The executor composes the shared
  `ChasterTokenManager` + `ChasterLockClient`. The composition root builds
  ONE token manager used by both `chaster status` and the tool, so
  refreshes share the same per-user lock.
- **Self-healing is transparent.** An expired token is refreshed before
  the call; a 401 is refreshed and retried once. If re-authorization is
  strictly required (`ChasterReauthorizationRequiredError`), the tool
  returns `{"status": "reconnect_required", "instruction": "... run
  `chaster connect`"}` and the guidance tells the model to say so. A
  transient outage returns `temporarily_unavailable` (connection left
  intact); no connection returns `not_connected`; other API failures
  return `api_error` **without** the raw error text. No failure result
  ever implies "unlocked".
- **Epistemic framing.** Success results carry `"source": "Chaster API"`,
  `"reliability": "provider-reported; not independently verified"` and
  `fetched_at` (the injected turn time). The guidance requires
  "Chaster reports …" phrasing and forbids "verified". The data is
  structurally separate from `lock_state`'s user-reported state and is
  not merged with it.
- **Prompt-injection hardening.** Lock titles/statuses are free text a
  keyholder can author: control characters are stripped, whitespace
  collapsed, each capped at 80 characters, at most 10 locks shown (with
  a `locks_omitted` count), and the whole result is size-bounded. The
  guidance tells the model a tool result is data, never instructions.
- **Embeds.** `ToolExecutionResult.data` carries a `ChasterToolOutcome`
  (kind + raw locks). `ApplicationService` builds the `EmbedSpec` from it
  with the **same** builder `chaster status` uses
  (`ApplicationService._chaster_message`, helpers shared via
  `chaster/presentation.py`): green for locks, blue for none, red for
  not-connected / reconnect / unavailable / API error. The model writes
  only the conversational text, so the card cannot show details Chaster
  never reported.
- **Working Memory.** Tool-augmented exchanges are not committed (see
  `conversation_engine/README.md`).
- **Routing rules (`CHASTER_TOOL_GUIDANCE`).** Local models such as
  `llama3.1` followed the older coaching text ("UNKNOWN ... try `lock
  report locked`") instead of calling the tool. The executor therefore
  carries explicit rules, appended as the last system message before the
  user's turn and stated to take precedence over earlier command advice:
  for any question about the lock, lock status, Chaster, being locked or
  time left -- in any language -- the model MUST call `get_chaster_status`
  FIRST, must not say the status is unknown beforehand, and must NOT send
  the user to `lock status` / `lock report locked|unlocked` while the tool
  is available (nor as a substitute when it reports not-connected or
  unavailable). The commands themselves are unchanged and still work; the
  tool description is also directive ("ALWAYS call this first ..."). A
  prompt cannot *force* a tool call from a small model -- which is why
  the deterministic router below exists.
- **Deterministic intent router (`is_chaster_status_query`).** Because
  `llama3.1` 8B still ignored the tool (even telling a user to "buy" `lock
  status`), `ChasterToolExecutor.match_forced_call()` recognises status
  questions in plain Python and the engine then runs `get_chaster_status`
  itself; the model only writes the reply from the real result (and the
  embed is built from that result regardless of what the model writes).
  Text is lowercased and stripped of diacritics/punctuation, then matched
  by: (1) the word "chaster" (any inflection); or (2) a lock noun
  (`lock`, `zamek/zamku/zamkem/zamcen...`) together with a status/time
  cue (`status, stav, vypada, zbyva, cas, time, left, remaining, check,
  zkontroluj, nacti, ...`); or (3) a short first-person state *question*
  (`Am I locked?`, `Jsem zamceny?`). Deliberately precise: messages that
  name a physical lock (door, car, bike, safe, code ...) and messages over
  200 characters never match; bare "Jaky je muj stav?" does not match
  either (could be mood). Anything unmatched still reaches the model,
  which may choose the tool itself. Plain `lock status` / `chaster status`
  are registered commands and never reach this path. Failure handling is
  unchanged: not-connected / reconnect / unavailable / API-error results
  flow to the model and the red embed exactly as before.
- **Opt-in.** `CHASTER_LLM_TOOLS_ENABLED=true` (default `false`),
  Chaster must be fully configured, and the Ollama model must support
  tool calling. If it does not, the bot falls back to ordinary replies.

**Open item (flagged, not resolved):** Chaster may let a keyholder hide
the remaining time from the wearer. This project has not confirmed such a
field in a real `LockForWearer`, so both `chaster status` and this tool
currently show `endDate`-based time remaining regardless. If a hide-time
field is confirmed, both should honour it (it is a one-place change in
`chaster/presentation.py`).

## What is NOT implemented

- **CHASTER-01B Increment 4** — a conversation-engine context
  provider reading `chaster_provider_observations`. Deliberately,
  explicitly deferred: wiring live/persisted Chaster data into
  `assemble_context()`'s prompt pipeline is its own decision, gated
  separately from the rest of CHASTER-01B, per this project's own
  review turn on the subject.
- Any change to `lock_state`'s user-reported model or `task_runtime`'s
  eligibility logic — both completely untouched, confirmed by direct
  inspection every time CHASTER-01B work has touched anything nearby.
- A command reading a *persisted* `chaster_provider_observations` row
  — that remains part of, or downstream of, Increment 4 specifically.
  (`chaster status`, below, is a separate, independent path — a live
  fetch, not a read of this table.)
- Key rotation tooling (`encryption_key_version` is reserved,
  unused, always `NULL`).
- **`LockForWearer`'s exact field-level JSON schema is now
  confirmed** for a real account's real active locks (43 top-level
  keys, including `_id`, `status`, `lockType`, `extensions` — see the
  design doc's own diagnostic history) — but
  `emergency_unlock_provider.py::unconfirmed_lock_field_extractor`,
  the function the **emergency-unlock path specifically** relies on,
  has **not** been updated to use this now-confirmed schema and still
  always raises. The confirmed schema is what
  `provider_observation_service.py` (above) is built against;
  emergency-unlock's own eligibility-check step remains a distinct,
  still-open piece of work.
- Real Chaster account identity resolution — **now resolved**, see
  `callback_service.py` above (kept here as a crossed-out item for
  changelog continuity — no longer a real gap).

## PC-local emergency-unlock safety plane

A **separate, standalone process** — `chaster/emergency_unlock_server.py`
(`python3 -m chaster.emergency_unlock_server`), never imported by or
importing `bot/discord_bot.py`, `application/`, or
`conversation_engine/` — confirmed by this project's own tests via
actual AST-parsed import statements, not a docstring claim. Exists
so the safety mechanism stays usable even if the Discord bot process
itself is hung, crashed, or otherwise malfunctioning.

- **`lock_client.py`** — `ChasterLockClient`: the two confirmed
  Chaster endpoints this safety plane needs (`GET /locks`,
  `POST /locks/{lockId}/emergency-unlock`), both now routed through
  `_http.py::send_with_ordered_headers()`. Returns each lock as a
  raw, unparsed `dict` — deliberately does not guess field names.
- **`emergency_unlock_provider.py`** — `RealChasterEmergencyUnlockProvider`:
  the real, tested Option A control flow (discover → enforce
  exactly-one → check eligibility → call the confirmed endpoint for
  that one lock only). `EmergencyUnlockProvider.attempt_unlock(access_token)`'s
  signature is unchanged — no caller can ever supply a lock ID.
  `UnwiredEmergencyUnlockProvider` remains as a test-only,
  network-inert fixture. The eligibility-check step still calls
  `unconfirmed_lock_field_extractor` (see above) and so still fails
  safely rather than guessing.
- **`emergency_unlock.py`** — `EmergencyUnlockService`: authenticates
  against a dedicated secret (`CHASTER_EMERGENCY_UNLOCK_SECRET`,
  never reused from any other Chaster secret), writes an audit event
  at every stage by reusing the existing `infrastructure.outbox`
  domain-events table (no new migration), decrypts the access token
  only immediately before a provider call.
- **`emergency_unlock_listener.py`** — its own separate
  `aiohttp.web.Application`, localhost-only, one route.
- **`emergency_unlock_server.py`** — the standalone entry point;
  refuses to start without both the emergency secret and the token
  encryption key configured.

**Deployment gate (unchanged): real personal Chaster deployment
remains blocked until a real, confirmed unlock operation exists and
has itself been thoroughly tested — this safety plane being
implemented and locally tested does not itself clear that gate.**

## Configuration

All new `.env` values are optional — the bot starts normally, and
`chaster connect` replies that Chaster is "not configured," if any
piece is missing:

- `CHASTER_CLIENT_ID`, `CHASTER_REDIRECT_URI` (non-secret)
- `CHASTER_CALLBACK_BIND_HOST` (default `127.0.0.1`), `CHASTER_CALLBACK_BIND_PORT` (default `8420`) (non-secret)
- `CHASTER_CLIENT_SECRET`, `CHASTER_TOKEN_ENCRYPTION_KEY` (secrets — generate the encryption key with
  `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`,
  then back it up **separately from the database** — losing one
  without the other makes every stored connection permanently
  undecryptable)

## Testability

- **Milestone A** (pure local, mocked Chaster HTTP): `tests/chaster/`
  — fully exercised, no domain/Cloudflare/real Chaster application
  needed. 405 tests across this package alone as of the deterministic
  tool-routing slice (1951 repository-wide).
- **Milestone B** (real local listener, no Cloudflare):
  `tests/chaster/test_callback_listener.py`,
  `tests/bot/test_chaster_listener_lifecycle.py` — a real `aiohttp`
  server, real HTTP requests, still entirely local.
- **Milestone C** (real OAuth through a real tunnel + a real,
  approved Chaster application): **still not achievable** — no
  longer blocked on identity resolution (now implemented), but
  Cloudflare Tunnel has never been set up, and `chaster_connections`
  has never been populated by a real, completed `chaster connect`
  flow. Every real request made against the live Chaster API so far
  (transport diagnostics, schema confirmation, this service's own
  manual smoke tests) has used a developer token entered by hand,
  never the production OAuth path. **All application-level code this
  milestone needs already exists and is already tested** — the
  `chaster connect` Discord command (`application/service.py::_handle_chaster_connect`),
  the callback receiver (`chaster/callback_listener.py` +
  `chaster/callback_service.py`, including real identity resolution
  and encrypted persistence) — none of it needed new code. What
  remains is entirely external: `docs/deployment/cloudflare_tunnel_config_template.yml`
  documents the exact `cloudflared` ingress configuration needed
  (one hostname → port 8420 only; port 8421, the emergency listener,
  must never appear in any tunnel ingress rule), and
  `scripts/dev_callback_listener_readiness_check.py` verifies the
  local listener itself is ready to receive tunnel-forwarded traffic
  — but neither substitutes for actually running `cloudflared`,
  registering the resulting public URL with a real, approved Chaster
  developer application, and completing one real `chaster connect`
  by hand.

## Current real-provider readiness (checkpoint status)

- **Option A is selected** for emergency-unlock target-lock semantics
  and is **implemented**: active-lock discovery happens live, at the
  moment of each emergency request, via the confirmed `GET
  /locks?status=active` — never from a cached or stale value.
- **Exactly one active lock is required.** Zero active locks fails
  safely (`"No active Chaster lock found"`); more than one fails
  safely with the exact candidate count, never picking one
  arbitrarily.
- **No caller can ever supply a `lockId`.** `EmergencyUnlockProvider.attempt_unlock(access_token)`'s
  signature has no lock parameter at all — selection happens entirely
  inside `RealChasterEmergencyUnlockProvider`.
- **The confirmed `POST /locks/{lockId}/emergency-unlock` endpoint is
  used**, with the documented bondage-lock + enabled-safety-feature
  eligibility requirement enforced before any unlock is attempted —
  though note the real, confirmed account used throughout this
  project's own diagnostics has `lockType: "chastity"`, not
  `"bondage"`, so this endpoint's documented prerequisite may not
  apply to that lock at all; this has not been reconciled.
- **`LockForWearer`'s top-level field schema is now confirmed** (see
  "What is NOT implemented" above) for real active locks on one real
  account — but the emergency-unlock eligibility-check step itself
  has not yet been updated to use it; `unconfirmed_lock_field_extractor`
  still always raises there.
- **`CurrentUser`'s exact field schema is now confirmed** — a real,
  complete, verbatim `GET /auth/profile` response was captured and
  used to build `callback_service.py::build_real_identity_resolver()`
  (above).
- **The real provider transport has been proven against a real
  Chaster account** — real `GET /locks?status=active` and real `GET
  /auth/profile` calls, through the actual production
  `ChasterLockClient`/`ChasterOAuthClient` (including the header-
  ordering fix), have both returned real HTTP 200 responses with
  real data, confirmed via a dev-only production-client smoke test.
  The real emergency-unlock POST itself has still never been
  exercised against a real account.
- **Cloudflare Tunnel is still not implemented** — Milestone C
  (real OAuth end-to-end) remains unreachable for that reason alone.
- **The local emergency safety-plane deployment gate remains open.**
  No real personal Chaster deployment should occur yet.


### Troubleshooting: lock questions reach the model with no embed

That means the tool is not active. Check the startup log: `Chaster LLM tool enabled` and `Conversation Engine tool calling ACTIVE` must both appear. A WARNING `CHASTER_LLM_TOOLS_ENABLED is true but the Chaster tool is NOT active` means Chaster is not fully configured (token encryption key, client id/secret, redirect URI). A per-message INFO `Tool call forced by deterministic match` confirms the router fired; `No forced tool match` means the matcher did not recognise the message. The matcher ignores diacritics, case and invisible format characters (1971 tests repository-wide).


### Deterministic DM guardrail (v0.28)

`ApplicationService.handle_message` answers natural-language lock/Chaster status questions (see `is_chaster_status_query`) with the `chaster status` path **before** any model, whether or not `CHASTER_LLM_TOOLS_ENABLED` is on. An unlinked account gets the red "Chaster Account Not Connected" embed, an unconfigured bot the red "Chaster Not Configured" embed. The engine-level forced routing remains a second layer. 1982 tests repository-wide.


### Hybrid lock-status answers (v0.29)

For a natural-language lock-status question the application fetches the locks deterministically and gives the model a verified `extra_system_context` (`chaster/status_context.py`: count, sanitised titles, raw status, time remaining, fetch time, provider-reported label; no other fields). The model only phrases the reply (tools off, text only). Not configured / not connected / reconnect / outage / API error, or any generation failure, produce the deterministic embed with no model text. 1994 tests repository-wide.


### English phrasing (v0.37; was Czech phrasing, v0.30)

`czech_text.format_time_czech(seconds)` inflects days/hours/minutes in code (1 -> singular, exactly 2-4 -> `dny/hodiny/minuty`, else `dní/hodin/minut`; zero units omitted; < 1 minute -> `méně než minuta`; negative -> ValueError). `llm_lock_view.build_lock_view(lock, now=, language=)` produces the dict the model sees (`lock_name`, `status`, `time_remaining_formatted`, `time_expired`, `has_end_time`, `can_be_unlocked`, `is_frozen`, `is_keyholder_lock`). Only `title`/`status`/`endDate` are confirmed Chaster fields; the three optional ones are `null` unless present with a boolean/object of the expected type. `status_context.build_chaster_status_context(..., language="cs")` appends `CZECH_STATUS_SYSTEM_PROMPT`. 2050 tests repository-wide.


### Live-test fixes (v0.31)

The lock view now carries `state` (frozen > expired > no_end_time > running). Frozen removes the countdown, sets `time_expired` false and `can_be_unlocked` null. The Czech prompt gained confidentiality, begging, no-commands (with negative examples) and numeral-verb-agreement rules (`Zbývající čas: **X**`). 2066 tests repository-wide.


v0.32: the Czech status prompt also forbids quoting the raw `status` field. 2069 tests repository-wide.


### Several locks (v0.33)

Each lock is turned into a view from its own dict only (`build_lock_views`); nothing is carried between iterations and inputs are not mutated (tested with a frozen keyholder lock plus a running self-lock, in both orders). The investigation found no Python path that drops a lock or moves a frozen flag, so if the live model still omits a lock, check the log line `Chaster status context built` (`lock_count`, `lock_states`, `chaster_statuses`, `lock_types`): `lock_count` 1 means Chaster's `GET /locks?status=active` itself returned one lock (a server-side filter -- not changeable in the view layer), while `lock_count` 2 with correct states means the model is mis-reading a correct context. 2082 tests repository-wide.


v0.34: the Czech status prompt carries the same direct-wording rule for explaining limits. 2084 tests repository-wide.


### Passive context in ordinary chat (v0.35)

`mentions_lock_topic()` (broad word-prefix check, diacritics-insensitive) or an active connection makes `ApplicationService` load the lock data for the normal conversation path via `build_chaster_chat_context`. Any failure yields no context (chat continues). With context present the model-chosen `get_chaster_status` tool is not offered (the data is already there); explicit status questions still use the status path. 2122 tests repository-wide. The Czech status prompt also requires the infinitive after helper verbs (`můžeš zkusit`, not `můžeš zkus`).


v0.37: the whole Chaster context is English. `english_text.format_time_english(seconds)` replaces `czech_text` (`2 days, 8 hours and 37 minutes`; under a minute -> `less than a minute`); the lock view, status context (`ENGLISH_STATUS_SYSTEM_PROMPT`) and chat context are single-language, summary lines read `Lock 1 of 2: "Name" [frozen], keyholder lock`. Earlier Czech-specific notes above are historical. 2110 tests repository-wide.
