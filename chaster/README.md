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
  needed. 326 tests across this package alone as of the self-healing
  tokens / `chaster disconnect` slice (1744 repository-wide).
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
