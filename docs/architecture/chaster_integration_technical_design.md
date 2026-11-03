# Chaster Integration — Technical Design (v0.17)

> **Update 2026-11-03 (owner decision): OAuth callbacks no longer use a tunnel.** Chaster redirects the user's browser, so the
> single canonical redirect URI is `http://localhost:8420/oauth/chaster/callback`, reached directly by a browser on the bot's own
> machine (the listener still binds `127.0.0.1:8420` only). The tunnel options and steps below (Option C, Milestone C) are
> historical for OAuth; the emergency listener (8421) stays local as before.

> **Status: Draft for review. HTTP callback architecture (Option C)
> is approved at the architecture level as of v0.4. The
> encryption/key-management architecture (Section 10) is recommended
> for approval as of v0.5, confirmed implementation-ready as of v0.6.
> Exact schema DDL (Section 21) is now specified. As of v0.7, the
> identity-resolution endpoint (`GET /auth/profile`) is confirmed via
> the official OpenAPI specification (Section 11) — one field-name
> detail remains before `unconfirmed_identity_resolver` can be
> replaced. As of v0.9, every retrieval avenue available in this
> environment (raw OpenAPI JSON at two content limits, the Swagger
> UI, and a targeted web search) has been tried and exhausted without
> reaching `CurrentUser`'s own field definitions from an authoritative
> source — this specific remaining detail now requires either a real
> developer application or a different information source not
> available here. CHASTER-01A itself (actual code, migration,
> dependency addition, and commands) remains implemented and locally
> validated (1273 tests) but is NOT real-Chaster-account-ready — see
> Section 26.**
>
> **v0.7 change note (identity-resolution research)**: Section 11
> gained a "Identity resolution mechanism — CONFIRMED" subsection
> recording `GET /auth/profile` (`operationId: AuthMeController_me`,
> response schema `CurrentUser`), confirmed directly from the
> official OpenAPI spec at `https://api.chaster.app/api-json`, plus
> the official `profile` scope description ("Access the username and
> email") from `docs.chaster.app/api/reference/scopes`. No code was
> changed — `unconfirmed_identity_resolver` remains in place pending
> confirmation of `CurrentUser`'s exact response field names.
>
> **v0.8 change note (narrow re-verification)**: a second fetch of
> the same OpenAPI document, requesting a substantially larger
> content limit, was attempted specifically to reach `CurrentUser`'s
> own field definitions. It truncated at the identical point as the
> first attempt. No code was changed. `unconfirmed_identity_resolver`
> remains in place — the blocker is explicitly **not** resolved.
>
> **v0.9 change note (Swagger UI + search verification)**: the
> official interactive Swagger UI (`https://api.chaster.app/api`) was
> attempted directly — its raw HTML is a client-side-rendered SPA
> shell with no schema content of its own (it fetches/renders the
> same `api-json` document via JavaScript, not executable here). A
> targeted web search for `CurrentUser`/`AuthMeController_me`
> surfaced no authoritative source, only an unofficial third-party
> PHP OAuth client, excluded as evidence per this project's own
> research discipline. No code was changed. The blocker remains
> explicitly open.
>
> **v0.10 change note (real-provider readiness audit)**: the existing
> CHASTER-01A code was audited against config handling, callback
> listener binding, token lifecycle, and the identity-resolution seam
> — **no correctness/architecture issue was found; no production code
> was changed.** Section 25a gained an explicit three-category
> "Real-provider prerequisites checklist" (can-test-now /
> requires-real-app / requires-tunnel). Section 25b gained an
> explicit, boxed deployment-gate statement. This turn's own
> instruction to stop re-attempting `CurrentUser` schema retrieval was
> followed — no further research was performed.
>
> **v0.11 change note (PC-local emergency-unlock safety plane)**: a
> new `chaster/emergency_unlock*.py` module set implements the
> safety-control plane as its own, separate process
> (`chaster/emergency_unlock_server.py`) — never imported by or
> importing `bot/discord_bot.py`. New Section 25c documents the exact
> architecture: a dedicated local secret
> (`CHASTER_EMERGENCY_UNLOCK_SECRET`), an audit trail reusing the
> existing `infrastructure.outbox` domain-events table (no new
> migration), and an explicit, unimplemented provider seam
> (originally named `NotConfirmedEmergencyUnlockProvider`, renamed to
> `UnwiredEmergencyUnlockProvider` in v0.12 once the operation itself
> was confirmed) — **the real Chaster
> remote-unlock operation remains unconfirmed and was not guessed.**
> The deployment gate (Section 25b) is updated to reflect that the
> safety plane is now implemented and locally tested, but explicitly
> remains open until a real, confirmed provider exists.
>
> **v0.12 change note (unlock operation confirmed — target-lock
> semantics now the open decision)**: `POST /locks/{lockId}/unlock`
> and `POST /locks/{lockId}/emergency-unlock` are now confirmed
> directly from the official OpenAPI spec, both `locks`-scoped — no
> OAuth scope change needed. `UnwiredEmergencyUnlockProvider`
> (renamed from `NotConfirmedEmergencyUnlockProvider` for accuracy —
> a mechanical rename only, no behavior change) remains the sole
> production wiring, since both confirmed endpoints require a
> `lockId` this project's interface does not yet accept and no target-
> lock decision has been made. Section 26 gained three concrete
> options (A/B/C) for this decision, deliberately without a
> recommendation. `CurrentUser`'s exact fields remain unresolved after
> one genuinely new, focused attempt (`docs.chaster.app/api/reference/action-logs`).
> No code behavior changed this turn beyond the provider rename.
>
> **v0.13 change note (Option A implemented)**: `chaster/lock_client.py`
> (new) and `RealChasterEmergencyUnlockProvider`
> (`chaster/emergency_unlock_provider.py`) implement the full,
> real, tested Option A control flow — discover active locks via the
> confirmed `GET /locks?status=active`, enforce exactly-one-candidate
> fail-closed, call the confirmed `POST /locks/{lockId}/emergency-unlock`
> only for the freshly-discovered lock. Now the production default in
> `chaster/emergency_unlock_server.py`. One genuine gap remains,
> isolated exactly like `unconfirmed_identity_resolver`:
> `LockForWearer`'s exact field schema is unconfirmed
> (`unconfirmed_lock_field_extractor` always raises) — a real request
> today discovers the lock for real and then fails safely at the
> eligibility check. 45 new tests (67 cumulative across both new
> modules and fixes). No migration; no OAuth scope change (`locks`
> already requested, already sufficient).
>
> **v0.14 change note (real-provider test readiness)**: new Section
> 25d, "First Real Chaster Provider Test Plan" — a controlled,
> phased, safe sequence for the eventual first real test, plus
> genuinely new developer-onboarding facts confirmed from official
> docs.chaster.app (approval-then-create-application sequence, a
> private `#developers` Discord channel, redirect-URI registration's
> exact location not confirmed). One small, justified addition:
> `ChasterOAuthClient.fetch_raw_profile(access_token)` — a
> development-only diagnostic mirroring `ChasterLockClient`'s own
> "return the raw dict" pattern, confirmed by a dedicated test to
> never be referenced by the real production callback flow. 8 new
> tests (75 cumulative). No migration; no change to
> `unconfirmed_identity_resolver`/`unconfirmed_lock_field_extractor`
> themselves — both remain exactly as they were.
>
> **v0.15 change note (pre-real-account sanity check)**: Section 25d
> corrected — a genuine gap, not cosmetic: the "Developer-application
> findings" paragraph already mentioned a developer-token path exists
> ("no OAuth needed"), but Phase 1's own numbered steps never
> connected to it, implicitly routing schema discovery through the
> full OAuth/callback/Cloudflare sequence. Confirmed via the same
> official Endpoints page already cited (developer token and OAuth
> access token both authenticate identically via the same `Bearer`
> header) that `fetch_raw_profile()`/`list_active_locks()` need
> neither OAuth nor Cloudflare to resolve the two schema blockers —
> only a developer token, called locally. Phase 1 now states this
> explicitly; the Cloudflare-prerequisites paragraph now states
> explicitly that it applies only to the separate goal of a real
> per-Discord-user connection, not to schema discovery. No code
> changed; no schema guessed; no real request made.
>
> **v0.16 change note (first real test — HTTP 400 diagnosis)**: new
> Section 25e records the actual result of the first real,
> developer-token test: both `GET /auth/profile` and
> `GET /locks?status=active` returned HTTP 400. Confirmed a genuine
> difference from Chaster's own official example request
> (`docs.chaster.app/api/public-api/endpoints/`), which includes a
> `Content-type: application/json` header our production clients
> never send — flagged explicitly as INFERENCE, not a confirmed root
> cause. `scripts/dev_chaster_schema_probe.py` gained a read-only
> diagnostic fallback (`_print_diagnostic_response()`) that captures
> and safely prints the real, redacted response body/headers/status
> when a primary call fails, and tests both the current header set
> and the official-example header set, so the next real run settles
> this empirically. No production client (`chaster/oauth_client.py`,
> `chaster/lock_client.py`) was modified. 11 new tests (33
> cumulative in `tests/scripts/`).
>
> **v0.17 change note (curl vs. Python request comparison)**: a real
> `curl.exe` request to `/locks?status=active` with the real developer
> token succeeded (HTTP 200), conclusively ruling out an invalid
> token, wrong endpoint/parameter, or general API unavailability —
> our Python client, same URL, same token, still returned 400. Direct
> inspection of the real `requests.PreparedRequest` (no network call)
> confirmed Python sends `User-Agent: python-requests/<version>`,
> `Accept-Encoding: gzip, deflate`, and `Connection: keep-alive` that
> curl's own shown request doesn't — flagged as INFERENCE (a real,
> confirmed difference, not yet a confirmed cause).
> `dev_chaster_schema_probe.py` gained a `curl_equivalent` header-style
> mode, verified against a real (unmocked) `PreparedRequest` to
> reproduce curl's exact header set, scoped to `/locks` only per
> instruction. **No production client was modified** — a fix to
> `chaster/lock_client.py` will only be made once this is confirmed by
> a real result. 26 tests in `tests/scripts/test_dev_chaster_schema_probe.py`
> (up from 21 — 2 obsolete tests removed, 7 added).
>
> **Dependency change policy**: `cryptography` is recommended as a
> future runtime dependency of `requirements.txt` — it has **not**
> been added. It will be added only once this recommendation is
> explicitly approved and an implementation turn actually begins.
>
> Nothing in this document is built. `core/config.py`'s existing
> `chaster_api_token` field remains exactly what it always was: an
> unread, scaffolded placeholder (`# Phase 7`), not an active
> integration. No migration, no production code, and no Discord
> command exist for anything described here.
>
> This document captures the research and architecture decisions made
> so far for a first, narrow slice (**CHASTER-01**: OAuth connection +
> read-only, on-demand provider state). It intentionally does not
> attempt to design the full eventual Chaster integration.
>
> **v0.2 change note**: Section 8 (HTTP callback architecture) and
> Section 9 (process/deployment) were revised against current,
> officially-verified Cloudflare Tunnel documentation and a deliberate
> attempt to disprove the v0.1 recommendation. The conclusion
> (Option C, one process) survived the challenge; the on-demand
> tunnel *lifecycle* did not — it is now "start once, run for the
> process's lifetime" rather than "start/stop per attempt," per the
> concurrency analysis in Section 9. A new Section 25 separates
> development and personal-deployment environments explicitly.
>
> **v0.3 change note (deployment-readiness audit)**: Section 9
> corrected — `main()` does **not** need restructuring into an
> `asyncio.run(main_async())` wrapper; `discord.Client` already
> exposes an official `setup_hook()`/`close()` lifecycle for exactly
> this purpose, verified directly against the installed library.
> `aiohttp.web` (the server, not just the client) is already an
> installed transitive dependency — no new package needed. A new
> fault-isolation requirement was surfaced: listener startup failure
> inside `setup_hook()` must never crash the whole bot. A new
> configuration-vs-secrets inventory was added to Section 9.
>
> **v0.4 change note (CHASTER-01A preparation audit)**: **Option C is
> now formally approved at the architecture level** (Section 9). A
> precise CHASTER-01A boundary was added (Section 26). OAuth `state`
> storage was re-examined against the actual deployment model and
> **changed from the earlier "in-process" assumption to a dedicated
> database table** (Section 21) — Section 21 explains why. Confirmed
> directly this turn: **no encryption/key-management facility exists
> anywhere in this repository's own dependencies** — a specific
> library choice is now an explicit open decision (Section 26), not
> something to assume from what happens to be installed in any given
> environment.
>
> **v0.5 change note (encryption/key-management architecture audit)**:
> Section 10 rewritten with a full, source-confirmed recommendation —
> `cryptography`'s `Fernet` (not raw AES-GCM), a single deployment
> master key via `.env` (`CHASTER_TOKEN_ENCRYPTION_KEY`, matching the
> existing `DISCORD_TOKEN` pattern), a reserved-but-unused
> `encryption_key_version` column for future rotation, and a narrow
> `TokenEncryptor` abstraction owned by the repository layer. Section
> 21's `chaster_connections` columns updated with confirmed
> column-level detail (TEXT not BLOB, no separate nonce/tag columns —
> Fernet embeds both). This is a **recommendation for approval**, not
> yet approved — `cryptography` has not been added to
> `requirements.txt`.
>
> **v0.6 change note (schema/prerequisite/safety audit)**: exact DDL
> specified for `chaster_oauth_states`/`chaster_connections` (Section
> 21), confirmed against `user_accounts`'/`user_preferences`'/
> `task_assignments`' own real schemas — both new tables use the
> one-row-per-user idiom, not the historical-rows idiom. New Section
> 18a resolves the connection lifecycle down to exactly two stored
> statuses. Section 7's concurrent-attempt behavior refined with the
> exact replace-on-reconnect mechanic. Section 10 gained a concrete
> `Fernet.decrypt(ttl=...)` misuse warning — encryption recommendation
> now marked **implementation-ready**, no material flaw found. New
> Section 25a (testability milestones) and Section 25b (a hard,
> not-yet-implemented **PC-local emergency-control** requirement,
> architecturally independent of Discord/Chaster/the callback
> listener). One new open question surfaced: whether
> `chaster_account_id` should be UNIQUE (left unconstrained for now).

## 1. Purpose

Let a Discord user optionally connect their own Chaster account to
this bot, so the bot can read that user's Chaster-reported lock status
on demand (`chaster status`) — as a second, clearly-separate,
provider-reported fact alongside the existing user-reported
`lock_state`, never a replacement for it.

## 2. Scope (CHASTER-01)

- Per-user OAuth 2.0 connection to a user's own Chaster account.
- Secure, encrypted-at-rest storage of that connection's credentials.
- An on-demand read of that user's current Chaster lock status
  (`locked` / `unlocked` / `deserted`), stored with its own provenance
  and fetch timestamp.
- A local-only disconnect.

## 3. Non-goals (explicitly out of scope for CHASTER-01)

- Any change to `lock_state`'s existing user-reported model or its
  `LockKnowledgeState` semantics.
- Any change to `task_runtime/eligibility.py` or task eligibility of
  any kind — provider state is read-only, informational, and inert
  with respect to every existing runtime decision in this slice.
- Background/scheduled polling of any kind.
- Webhooks (the Public API does not offer them — see Section 5).
- Automatic reconciliation of a conflict between user-reported and
  provider-reported state.
- `keyholder`/`shared_locks`/`messaging` OAuth scopes — only `locks`
  is requested.
- Any UI beyond three conceptual Discord commands
  (`connect`/`status`/`disconnect`).
- Chaster OAuth, an HTTP server, or any other production code — this
  document is design only.

## 4. Current architecture (as it actually exists today)

Confirmed directly from the repository, not assumed:

- **Process model**: `bot/discord_bot.py::main()` builds one
  composition root, then calls `bot.run(config.discord_token,
  log_handler=None)` — `discord.Client.run()` is a **blocking** call
  that owns the process's asyncio event loop until the bot
  disconnects or crashes. There is exactly one process, one event
  loop, today.
- **No HTTP server of any kind exists.** Confirmed by a direct search
  across all production code and dependencies (`requirements*.txt`)
  for Flask/FastAPI/aiohttp-server/`http.server`/uvicorn/starlette —
  none found. The application is a pure outbound Discord client; it
  has never needed to receive an inbound HTTP request.
- **No public network exposure of any kind is documented or
  configured.** `.env.example` has no `PORT`/`PUBLIC_URL`/`HOST`
  variable; `OLLAMA_HOST=http://localhost:11434` confirms even the
  model backend is local-only. No Dockerfile, no docker-compose, no
  reverse-proxy configuration, no TLS termination anywhere in the
  repository.
- **Deployment is `windows/README.md`'s own documented reality**: a
  Windows Scheduled Task (`AtLogOn` mode — deliberately **not**
  "whether user is logged on or not", to avoid storing a Windows
  credential), auto-restart on crash via Task Scheduler's own retry
  policy, logs to a local file. This is explicitly described as **"a
  personal bot on a personal Windows machine"** — not a dedicated,
  always-on server. No domain, no static/public IP, and no port
  forwarding are documented anywhere.
- **Conclusion**: this project today has zero inbound-network-facing
  infrastructure, runs only while its operator is logged into their
  own PC, and was explicitly designed around that assumption rather
  than around unattended, always-on server operation.

## 5. Chaster API findings (official sources, from the prior research turn)

- Two APIs: **Public API** (relevant here — read/manage a user's own
  locks) and **Extensions API** (build an in-Chaster game/feature,
  requires Chaster's own review process; this is the *only* API with
  webhooks). — *docs.chaster.app/api/basics/getting-started*
- Full OpenAPI 3.0 spec is public at `https://api.chaster.app/api-json`.
- `LockStatusEnum`: `["locked", "unlocked", "deserted"]` — Chaster's
  own official terminology. — *official OpenAPI spec*
- Relevant read endpoints: `GET /locks` (`LockForWearer[]`, active
  locks by default), `GET /locks/{lockId}` (`LockForPublic`).
- **No webhook exists for the Public API** — confirmed via the
  official Extensions API webhooks/changelog pages, which describe
  webhooks (`extension_session.created/updated/deleted`,
  `action_log.created`) as an Extensions-API-only feature. Polling is
  therefore the only option for CHASTER-01's use case.
- API access itself requires an approval request (a Google Form); the
  API is explicitly still in **beta**.
- No confirmed per-lock "state changed at" timestamp field was found
  in the (partially retrieved) `LockForPublic`/`LockForWearer`
  schemas — **NOT CONFIRMED BY OFFICIAL SOURCE**. No such field is
  assumed or designed around.
- **NOT CONFIRMED BY OFFICIAL SOURCE**: any OAuth token-revocation
  endpoint, or a device-code/callback-free OAuth variant (Section 8
  addresses the consequence of both).

## 6. Authentication

- **Developer token**: a single, all-scopes token tied to the
  developer's own Chaster account — usable only for accessing that
  one account, not suitable for a multi-user Discord integration, but
  directly useful for *testing the API contract itself* without OAuth
  infrastructure (see Section 13).
- **OAuth 2.0** (OpenID-Connect-compatible): the only mechanism that
  supports per-Discord-user Chaster connections. Authorization URL
  `https://sso.chaster.app/auth/realms/app/protocol/openid-connect/auth`,
  Token URL
  `https://sso.chaster.app/auth/realms/app/protocol/openid-connect/token`.
  Standard `authorization_code` grant; `state` param supported.
  **Authorization codes expire in 10 minutes and are single-use.**
  Access tokens are short-lived (`expires_in: 300`, i.e. 5 minutes, in
  the official example response); refresh tokens have their own,
  also-short, window (`refresh_expires_in: 1800`, 30 minutes).
- Requested scope for CHASTER-01: `locks` only.

## 7. OAuth flow

```text
Discord user
  → "chaster connect" (Discord command, not yet implemented)
  → Bot generates a single-use `state`, persists it in a dedicated
    database table (see Section 15 -- changed from an earlier
    "in-process" assumption), keyed by state -> user_id, TTL ~10 min
  → Bot replies (DM preferred) with the Chaster authorization URL
  → User's browser: Chaster login + consent screen
  → Chaster redirects browser to our callback URL with ?code&state
  → Callback handler (see Section 9 for where this runs):
      - look up `state`; missing/expired -> reject, consumed either way
      - the user_id bound to that state is who gets credited
        (never inferred from the browser session itself)
      - exchange `code` for tokens at the Token URL (server-to-server)
      - call an authenticated Chaster identity endpoint (e.g.
        /auth/profile) with the fresh token to get Chaster's own
        stable account _id -- never trust a client-supplied identity
      - only then, atomically, persist the connection row
  → Bot notifies the Discord user of success/failure
```

- `state`: a cryptographically random, single-use value; **deleted
  from storage immediately on first lookup** (consumed) — regardless
  of whether the rest of that callback's processing (token exchange,
  identity fetch) goes on to succeed. Deleting on lookup, not only on
  full success, was chosen deliberately: it keeps the replay window
  as small as possible; a rare downstream failure after a valid
  lookup just means the user re-runs `chaster connect`, which is a
  minor inconvenience, not a security concern. This is what defeats
  CSRF and authorization-code replay together with Chaster's own
  single-use code guarantee.
- CSRF/replay: covered by the above; an attacker cannot produce a
  valid `state` without it already existing in our own store, tied to
  a specific Discord user who actually issued `chaster connect`.
- Discord user A vs. B: prevented structurally — the state is bound
  to the user who *initiated* the flow, never to whichever browser
  session completes it.
- Callback failure (user denies, invalid/expired state, exchange
  fails, identity fetch fails): a clear failure message; **no partial
  credential row is ever persisted** — the write happens only after a
  fully successful identity-confirmed exchange.
- Concurrent attempts by the *same* user (**refined this turn with
  the exact schema in hand, Section 21**): the `chaster_oauth_states`
  table's own `UNIQUE(user_id)` constraint means a new `chaster
  connect` must **replace** (delete-then-insert, or an equivalent
  upsert) the existing pending row for that user — not merely
  "invalidate" it vaguely. The concrete, previously-unstated
  consequence: the *old* authorization link becomes immediately dead
  the moment a second `chaster connect` is issued — if the user goes
  back and clicks the first link, its `state` no longer matches any
  row and fails safely as "unknown state," the same as if it had
  simply expired. The `chaster connect` reply for this case should
  say as much (e.g. "this replaces any previous pending connection
  attempt") so the behavior isn't surprising.
- Concurrent attempts by *different* users: no conflict — independent
  state rows, looked up by their own distinct value; the shared
  listener handles concurrent requests natively (Section 9).
- **Duplicate callback for the same state** (two near-simultaneous
  requests, e.g. a user double-clicking): whichever request's lookup
  transaction commits first deletes the row; the second finds nothing
  and fails safely as "unknown state." This requires the actual
  implementation's lookup-and-delete to happen as one atomic
  operation (a single transaction, not a separate SELECT followed by
  a separate DELETE) — an explicit correctness requirement for
  whoever implements CHASTER-01A, not merely a description of the
  ideal outcome.

## 8. HTTP callback architecture — the central open question

**Confirmed finding**: OAuth's authorization-code flow structurally
requires a real, reachable HTTPS endpoint to receive Chaster's
redirect. This project has none today (Section 4), and its actual
production environment (a personal Windows PC, not a dedicated
server) makes permanently exposing one a real, non-trivial decision —
not a default to assume.

### Option A — Direct HTTP server on the current Windows host

- Would require: a stable public/static IP or dynamic DNS (neither
  exists today), router port-forwarding, a real TLS certificate
  (no HTTP-01 challenge path exists without already being publicly
  reachable, so DNS-01 would be needed), and ongoing Windows Firewall
  configuration.
- Directly contradicts this project's own documented deployment
  philosophy (`windows/README.md`'s explicit choice of `AtLogOn`
  specifically to avoid unattended/server-like operation).
- Uptime is tied to the operator being logged in — actually
  **acceptable** for this specific use case, since completing
  `chaster connect` is inherently something the operator does while
  present at their own PC.
- **Assessment**: the "public exposure" requirements (static IP/DNS,
  port-forwarding, TLS, firewall) are disproportionate new
  infrastructure and a real, permanent increase in attack surface for
  a personal machine, for a feature that's only actually needed for a
  few minutes per connection attempt.

### Option B — Separate, cloud-hosted callback relay service

- The relay would need to either complete the full token exchange
  itself (touching secrets) or hold the callback's `code`/`state`
  until the home-PC bot retrieves them — since the bot cannot receive
  an inbound call either way, the bot would need to **poll the relay**
  for pending completions. This is workable and keeps the home
  network fully closed to inbound traffic, but requires standing up,
  securing, and maintaining a second, always-on, real internet-facing
  service — its own hosting, its own secret handling, its own uptime
  — which is disproportionate ongoing maintenance for a single-user
  personal project today.
- **Assessment**: the architecturally "proper" long-term answer if
  this project ever needs multi-user scale or unattended reliability,
  but excessive for CHASTER-01 as currently scoped.

### Option C — Tunneling (a public HTTPS endpoint tunneled to a local listener)

**Verified against current official Cloudflare documentation
(`developers.cloudflare.com`) this turn:**

- Cloudflare Tunnel offers two distinct modes, confirmed directly
  from Cloudflare's own docs:
  - **Quick tunnels** (`cloudflared tunnel --url ...`) — zero-config,
    a random `*.trycloudflare.com` hostname, **no Cloudflare account,
    DNS record, or custom domain required** — but the URL changes on
    every restart. Dev/testing only; incompatible with a
    pre-registered fixed `redirect_uri`.
  - **Named tunnels** — a stable hostname (`<name>.<your-zone>`) that
    **survives restarts**. Cloudflare's own documentation explicitly
    names this the recommended option for **"production traffic,
    webhook receivers, OAuth callbacks, and any URL that needs to be
    bookmarked"** — a direct, on-point official recommendation for
    exactly our use case.
  - Named tunnels require a zone (domain) you control, but — also
    confirmed directly from Cloudflare's own Tunnels FAQ — **this
    does not require moving the domain's nameservers to Cloudflare.**
    "Partial Setup" (CNAME Setup) lets the domain keep its existing
    DNS provider; you add exactly one CNAME record for the specific
    subdomain used (e.g. `chaster-callback.yourdomain.com` →
    `<value>.cdn.cloudflare.net`). "Full Setup" (all nameservers on
    Cloudflare) is described as giving "the best experience" but is
    **not required**.
  - Cloudflare Tunnel itself carries no separate cost and works on
    Cloudflare's Free plan; the paid Zero Trust tiers gate
    seat-count/identity features irrelevant to a single-owner
    callback endpoint.
  - `cloudflared` officially supports Windows (official downloads
    page, official GitHub repo, and an official Cloudflare doc page
    showing a Windows PowerShell session running it directly) — can
    install via direct binary download or `winget install -e --id
    Cloudflare.cloudflared`, and can run as a Windows service.
  - HTTPS termination happens at Cloudflare's edge; the local
    `cloudflared` process talks plain HTTP to the local listener —
    matching what was assumed.

  **`CLOUDFLARE TUNNEL: CONFIRMED SUITABLE`** — subject to owning or
  being able to add one CNAME record to a domain (Part 2 below).

- **Critical constraint, unchanged**: Chaster's OAuth app registration
  requires one **fixed** `redirect_uri`, registered ahead of time —
  this is what rules out quick tunnels/rotating-URL free tunnels
  beyond initial manual testing.
- **NOT CONFIRMED BY OFFICIAL CHASTER SOURCE** (checked directly this
  turn, not merely assumed): whether Chaster enforces exact-string
  matching against a pre-registered `redirect_uri` list, whether
  `http://localhost` is permitted for a development/testing
  application, whether wildcard redirect URIs are supported, and
  whether one OAuth application can register multiple redirect URIs
  (e.g. one for development, one for personal deployment). Chaster's
  own "Create your application" page describes only naming the
  application; no redirect-URI-specific documentation was found at
  the depth searched. **This must be verified directly in the
  Chaster developer interface once API access is approved** — do not
  assume typical OAuth2 provider behavior applies here.

### Option D — An OAuth variant that avoids a public callback entirely

- Checked directly: nothing in the official Chaster API documentation
  describes a device-code / out-of-band grant (the pattern some
  providers use — "enter this code on your phone/TV") or any
  redirect-free OAuth variant. The only documented flow is the
  standard authorization-code-with-redirect flow.
- **NOT CONFIRMED BY OFFICIAL SOURCE that any callback-free option
  exists.** As anticipated, this option does not appear to be
  available.

### Recommendation (re-examined and refined — see Section 9 for the
concurrency/lifecycle refinement this challenge surfaced)

**RECOMMENDED: Option C — a Cloudflare named tunnel with a stable
hostname, sharing the bot's own asyncio process (Section 9).**

Why: it fits the actual project (personal bot, occasional use, no
existing domain/server infrastructure) far better than Option A
(permanent home-network exposure, contradicts this project's own
deployment philosophy) or Option B (a whole new always-on service,
disproportionate maintenance for one user). Option D does not exist.
Cloudflare's own documentation independently names this exact use
case (OAuth callbacks) as the intended one for named tunnels — this
is not merely a design choice made in isolation, it matches the
tool's own stated purpose.

- **Restart behavior**: the tunnel/listener starts with the bot
  process (Section 9's refined recommendation) — nothing
  attempt-specific to restart; a mid-flow tunnel failure simply means
  the user retries `chaster connect` once connectivity is restored.
- **Callback service unavailable**: if the tunnel isn't currently
  connected when `chaster connect` is issued, this is detected before
  the user is sent to Chaster's consent screen — no partial state.
- **Bot process unavailable**: irrelevant to this flow specifically —
  if the bot isn't running, `chaster connect` was never issued in the
  first place.

## 9. Process/deployment architecture

**Recommended: Option 1 — one process, one asyncio event loop**,
not two separate OS processes. Re-examined specifically against
`discord.Client.run()`'s actual lifecycle this turn, and the
recommendation is reinforced — with a **correction**, not just a
refinement, to how simple this actually is.

**Correction (deployment-readiness audit, this turn)**: the previous
revision of this document said adding an HTTP listener to the same
process "requires restructuring `main()`... into an async-driven
`asyncio.run(main_async())` wrapper." This overstated the work
involved. Verified directly against the installed `discord.py`
library's own source this turn: `discord.Client` already exposes
**`setup_hook()`** — an official, documented coroutine "called once,
in `login()`... before any events are dispatched," on the *same*
event loop `bot.run()` already owns — specifically intended for
exactly this kind of "start additional async resources when the bot
starts" case. `Client.close()` is equally overridable for teardown.
**`main()`'s existing `bot.run(config.discord_token,
log_handler=None)` call does not need to change at all.** Only
`CoachKeyholderBot` (in `bot/discord_bot.py`) would gain a
`setup_hook()` override (start the listener) and a `close()` override
(stop it, then `await super().close()`). This is a smaller, cleaner
integration point than previously described.

**Also confirmed this turn**: `aiohttp` — including `aiohttp.web`
(`Application`, `AppRunner`, `TCPSite`) — is **already an installed
dependency** (a transitive dependency of `discord.py` itself, not a
new one this project would need to add to `requirements.txt`). The
callback listener can be built entirely on infrastructure already
present, using the same non-blocking, event-loop-native
`AppRunner`/`TCPSite` startup pattern aiohttp's own documentation
recommends — nothing about this blocks or competes with Discord's own
websocket traffic on the shared loop.

**Fault-isolation requirement surfaced by this correction**: because
`setup_hook()` runs *before* the bot connects to Discord, an
**unhandled exception inside it would prevent the bot from starting
at all** — not just break the OAuth feature. This makes an explicit
requirement, not just a nice-to-have: listener/tunnel startup failure
inside `setup_hook()` must be caught and logged, never allowed to
propagate — the bot must come up and serve ordinary Discord traffic
even if the callback listener fails to bind or the tunnel is
unreachable. `chaster connect` itself would then need its own
runtime check ("is my local listener actually up?") to fail with a
clear message rather than silently handing out a broken link.

**Confirmed from the actual code**: `bot.run(...)` is literally
`discord.Client.run()`, which internally wraps `asyncio.run(...)` and
owns the process's event loop until the bot stops. Everything else in
the composition root (`LockState`/`TaskRuntime`/`TaskCatalog`/
`ConversationEngine` construction) is unaffected by any of this.

**Refinement found while challenging the original recommendation
(Part 7)**: the original phrasing — "start the tunnel/listener only
for the duration of one connect attempt, tear down immediately after"
— does not hold up cleanly for two Discord users connecting at
overlapping times (this section's own "Concurrency and edge cases"
subsection below): a strict
per-attempt start/stop would need reference-counting (don't tear down
while a second attempt is still pending), adding real complexity for
a small feature. **Refined recommendation: start the listener/tunnel
once, either at bot startup or lazily on the first `chaster connect`
of the process's lifetime, and simply leave it running for the rest
of that process's life** (it is idle and harmless when no connection
attempt is in progress). This is simpler, avoids the
teardown/reference-counting problem entirely, and is still
dramatically smaller exposure than a permanently-running dedicated
server, precisely because the whole process is itself already bounded
by `windows/README.md`'s own `AtLogOn` lifecycle — the endpoint is
only ever reachable while the operator is logged in and the bot is
running, not 24/7 regardless.

**One process vs. two, compared directly:**

| | One process (recommended) | Two processes |
|---|---|---|
| Startup | Single existing Scheduled Task, no new tooling | Needs a second Scheduled Task or a supervisor relationship — new deployment tooling `windows/README.md` doesn't have today |
| Shutdown | Both stop together | Two independent lifecycles to manage |
| Failure isolation | A well-isolated aiohttp request handler does not crash the shared loop on an unhandled exception in one request, but this must be coded defensively | Genuinely stronger isolation — a callback-server crash cannot affect the Discord bot |
| Windows Scheduled Task fit | Fits the existing, already-working setup exactly | Requires a second Task, mirroring `windows/README.md`'s existing three-script pattern a second time |
| Logging | Shares the existing log file/handler | Needs its own sink or a shared one to coordinate |
| Secrets | One place holds the Chaster client ID/secret | The same secret exists in two places, or the second process needs its own credential-decryption capability duplicated |
| Deployment complexity | None beyond what exists | New install/uninstall scripts |
| Testing complexity | Same pytest process, an async test client alongside existing bot tests | Cross-process coordination is harder to test deterministically |

The failure-isolation advantage of two processes is real but, in
practice, small (a defensively-written handler mitigates most of it);
the deployment/secrets/testing costs of two processes are concrete
and immediate. **One process remains the correct choice**, now with
better justification than before this turn's challenge.

### Concurrency and edge cases (analyzed while challenging the recommendation)

- **User starts OAuth, tunnel dies halfway through**: the pending
  `state` entry still exists server-side with its own TTL; the user's
  browser gets a connection error reaching the (now-unreachable)
  hostname; the `state` simply expires unused; **no connection row is
  ever written**, since persistence only happens after a fully
  successful, identity-confirmed exchange (Section 7). Safe — the
  user just retries.
- **Bot restarts mid-flow**: any in-memory pending `state` is lost —
  the user must re-run `chaster connect`. This never affects an
  already-completed connection (which lives in the database, not in
  memory) — only an interrupted, never-completed attempt.
- **Callback arrives after `state` expiration**: rejected at lookup —
  either already deleted or found-but-expired; the user sees a clear
  "expired, try again" message. Chaster's own 10-minute authorization
  code expiry would separately invalidate the flow regardless.
- **User clicks the authorization URL twice**: Chaster's own
  authorization codes are single-use; whichever browser tab completes
  first succeeds and consumes the shared `state`. The second callback
  attempt (if it arrives) finds no matching `state` and fails safely
  — a minor UX confusion, not a security issue.
- **User starts two `chaster connect` attempts**: the safest design is
  **one pending `state` per Discord user at a time** — a second
  `chaster connect` invalidates/replaces any earlier still-pending
  state for that same user, mirroring this project's own existing
  "at most one active thing at a time" pattern (`task_runtime`'s own
  single-active-assignment constraint).
- **Two different Discord users connect simultaneously**: no
  conflict — each gets an independently generated, uniquely-keyed
  `state`; the single shared listener/tunnel endpoint routes each
  incoming callback by its own distinct `state` value, not by any
  process-level exclusivity. This requires the listener to correctly
  handle concurrent/overlapping requests (which a proper async HTTP
  server does natively) rather than assuming only one flow is ever in
  progress — this is exactly the assumption the original "strict
  per-attempt start/stop" phrasing risked violating, and is why the
  refined "start once, run for the process's lifetime" design above
  is the safer choice.

### Configuration vs. secrets needed for real deployment (deployment-readiness audit, this turn)

Verified against `core/config.py`'s existing, established pattern
(environment variables override `.env`, `.env` never committed,
`ConfigError` raised only for genuinely required values) — any real
CHASTER-01 deployment would eventually need these values, clearly
separated into two categories. **None of this is being added now** —
this is an inventory for when implementation is actually approved.

**Configuration (not secret — could be documented with a placeholder,
safe to mention in setup instructions):**
- The callback hostname (e.g. `chaster-callback.<a domain Jiří
  controls>`) and the full `redirect_uri` built from it.
- The local bind host/port the aiohttp listener listens on internally
  (e.g. `127.0.0.1:<port>`) — never itself exposed directly; only
  reachable through the tunnel.
- The Cloudflare named tunnel's own name — lives in Cloudflare's
  dashboard / the local `cloudflared` config file, not necessarily in
  this project's own `.env` at all.
- `CHASTER_CLIENT_ID` — an OAuth client identifier; conventionally
  not treated as highly sensitive (it appears in the authorization
  URL itself, which is not secret), but still sourced from `.env`
  for consistency with everything else.

**Secrets (never logged, never committed, `.env`-only, following the
exact discipline `DISCORD_TOKEN` already uses):**
- `CHASTER_CLIENT_SECRET`.
- Cloudflare's own tunnel credentials file (`cert.pem`/tunnel token)
  — this is a **separate secret boundary from this project's own
  `.env`**, managed by `cloudflared` itself, not read by our Python
  code at all.
- Later, per-user encrypted OAuth tokens (already covered in
  Section 10).

**What must come from Jiří, without ever being a secret in this
list**: confirmation of which domain/subdomain he can add the one
required CNAME record to (Part B of this turn's audit) — this is
information, not a credential, and nothing here assumes an answer.

## 10. Credential security

- **Per-user OAuth tokens** (access + refresh) — never a shared
  developer token for multi-user use.
- Raw tokens: never in Working Memory (already structurally true —
  Working Memory only ever stores `user_content`/`assistant_content`
  strings), never in a Discord message, never in logs, never in
  exception messages, never in normal prompt/context construction.

### Dependency policy (audited directly this turn)

`requirements.txt`/`requirements-dev.txt` use minimum-version pins
(`>=X.Y.Z`), no upper bounds, no lockfile, no `pyproject.toml`, and no
existing optional-vs-required dependency split — everything listed is
unconditionally installed. There is no special process for
"security-sensitive" dependencies beyond ordinary code review; this
project has, however, demonstrated a deliberate preference for
avoiding a dependency where a small amount of its own code is
genuinely sufficient (`core/config.py`'s own comment explaining why
`python-dotenv` was skipped) — that reasoning does **not** extend to
cryptography, where hand-rolling primitives is a well-established
source of real vulnerabilities, not a case where a "small amount of
own code" is safely sufficient. Adding one well-justified dependency
here is consistent with, not a departure from, this project's own
judgment (it already depends on `discord.py`/`requests` where those
are the right tool). Mechanically, adding a new dependency is trivial
— one more `>=` line, no lockfile to regenerate, no CI to update.

### Cryptographic primitive (source-confirmed)

Confirmed directly against the official Python documentation
(`docs.python.org/3/library/crypto.html`, "Cryptographic Services"):
**the Python standard library has no general-purpose symmetric
cipher** — only hashing (`hashlib`), `hmac`, and `secrets`. A
third-party library is required; there is no stdlib-only path to
authenticated encryption.

Confirmed directly against the official `cryptography.io`
documentation (PyCA's own docs): the library's `Fernet` recipe
already provides exactly what this use case needs, as a single
high-level API:
- **Authenticated encryption** — AES-128 in CBC mode + HMAC-SHA256,
  combined so tampering is detected *before* any plaintext is ever
  returned (`cryptography.fernet.InvalidToken` is raised instead).
- **Nonce/IV handling is automatic** — a fresh random IV is generated
  internally on every `encrypt()` call and embedded in the token
  itself; the application never generates, stores, or manages a nonce
  separately. This removes an entire class of implementation mistakes
  a hand-rolled AES-GCM construction would otherwise require getting
  right (nonce uniqueness under a fixed key is exactly where most
  real-world AEAD misuse bugs come from).
- **Built-in key rotation** — `cryptography.fernet.MultiFernet` lets
  multiple keys be configured at once (a current key for new
  encryptions, and the current plus any number of older keys for
  decryption), with zero custom rotation logic to write.

**AES-GCM directly** (via the library's lower-level `hazmat` layer)
was evaluated and is **not recommended** for this use case: it offers
real advantages for streaming or very large payloads, neither of
which applies here (OAuth tokens are short strings, trivially held in
memory), and it would require the application to manage nonce
generation and storage itself — exactly the class of mistake Fernet's
higher-level API exists to prevent. **Recommendation: `cryptography`'s
`Fernet`, not raw AES-GCM.**

### Sanity-check finding (this turn) — one concrete implementation detail, no material flaw found

The v0.5 recommendation was deliberately challenged this turn (Fernet
suitability, `.env` key storage, the reserved key-version field,
repository-owned `TokenEncryptor`, independent access/refresh
encryption). **No material security flaw was found** — the
recommendation is confirmed **implementation-ready**. One concrete,
easy-to-get-wrong detail was surfaced and must be recorded so an
implementer doesn't misuse it: `Fernet.decrypt()` accepts an optional
`ttl=` parameter that rejects a token as expired based on the
timestamp *embedded inside it at encryption time* — this is meant for
short-lived messages, not long-lived stored credentials. **CHASTER-01A
must never pass `ttl=` to `decrypt()`** — a Chaster access/refresh
token may legitimately sit encrypted in the database far longer than
any sensible Fernet `ttl` window; using it would cause spurious
`InvalidToken` failures on perfectly valid, correctly-decrypting
stored credentials. Token freshness is already handled correctly and
separately, via this project's own `access_token_expires_at`/
`refresh_token_expires_at` columns (Section 21) — Fernet's own
internal timestamp is irrelevant to that and must not be conflated
with it.

### Encryption library (source-confirmed maturity/compatibility)

`cryptography` (PyCA) — its own official description: "our goal is
for it to be your 'cryptographic standard library.'" Confirmed via
its own GitHub repository: actively maintained by the Python
Cryptographic Authority, ships prebuilt wheels (including Windows),
and its current release line supports Python versions well past this
project's own verified 3.14.6 (README's own "User-verified working on
Windows with Python 3.14.6" claim). No concrete reason was found to
consider an alternative (e.g. `PyNaCl`) — `cryptography` is already
the de facto standard for exactly this use case in the Python
ecosystem, and introducing a second, less-common library for no
functional gain would be an unjustified addition.

### Key management (evaluated against this actual deployment)

| Option | Assessment |
|---|---|
| **A. Environment variable / `.env`** | Matches the exact existing `DISCORD_TOKEN` pattern already accepted for this project — zero new mechanism. Protects against source-control exposure (already gitignored) and against a stolen/copied database file alone. Does **not** protect against a full local-account/administrator compromise (whoever can read `.env` can typically also read the database file it decrypts) — this is an accepted, pre-existing limitation of this project's whole security model, not a new one introduced here. |
| **B. Windows-protected secret storage (DPAPI/Credential Manager)** | Genuinely stronger against a different-user-account reading the raw file, but this deployment is already single-user by design (`AtLogOn`); the marginal security gain does not justify a new platform-specific dependency and code path for a single-user personal project. |
| **C. External secret manager** | Introduces a network dependency and an ongoing service for something that should work fully offline — disproportionate, and inconsistent with this project's consistent avoidance of new hosted infrastructure. |
| **D. Manually-entered passphrase at each startup** | Incompatible with unattended `AtLogOn` + auto-restart-on-crash operation — a human would need to be present at every restart. |

**Recommended: A — `.env`**, with one **new, explicit operational
requirement** this audit surfaced: `Database.create_backup()` backs
up the database file only, not `.env`. If the master key lives solely
in `.env` and is ever lost independently of the database (or vice
versa — a database restored without its matching `.env`), every
stored Chaster token becomes **permanently** undecryptable. `.env`
(or at minimum the master key value) must be backed up with the same
care as the Discord token already receives — this is a documentation/
operational requirement, not a new mechanism.

- **Variable name**: `CHASTER_TOKEN_ENCRYPTION_KEY` (singular, for
  CHASTER-01A — see rotation, below).
- **Format**: exactly `Fernet.generate_key()`'s own output — a
  URL-safe base64-encoded 32-byte value, matching `cryptography`'s
  own documented format precisely; no custom encoding.
- **Never auto-generated by the bot itself** — generated once, by the
  operator, the same way `DISCORD_TOKEN` is obtained externally and
  pasted into `.env`. Auto-generation would risk a false sense that
  the key is safely "already there," skipping the operator's own
  deliberate backup step this key specifically requires. No key is
  generated or stored as part of this design audit.

### Key rotation (minimum viable model — not implemented)

CHASTER-01A needs **no rotation tooling** — a single active key is
sufficient to start. What it should still do, cheaply, is leave room
for rotation later: reserve a nullable `encryption_key_version`
column on `chaster_connections` (Section 21) now, even though unused
in CHASTER-01A, so a future rotation slice doesn't need a schema
migration just to add it retroactively. `MultiFernet` already makes
actual rotation nearly free when it's eventually built: configure the
new key first (for new encryptions) alongside the old key (kept, not
deleted, so it can still decrypt already-stored rows), and a future,
separate slice would perform a re-encryption sweep of existing rows
before the old key is ever safely removed — none of this is built
now.

### Application boundary — a narrow `TokenEncryptor`

- **Interface**: `encrypt(plaintext: str) -> str` /
  `decrypt(ciphertext: str) -> str` (raising a specific, named
  exception — e.g. `TokenDecryptionError` — on `InvalidToken`, never
  a bare/generic exception, matching this project's own convention of
  specific exception types elsewhere).
- **Knows nothing about the database.** A pure in-memory transform;
  the master key is loaded once, from `Config`, at construction —
  never re-read from `.env` per call.
- **Repositories own encryption, not callers.** A future
  `chaster/repository.py` calls `TokenEncryptor.encrypt()`
  immediately before an INSERT/UPDATE and `.decrypt()` immediately
  after a SELECT — application/service-level code (e.g. the OAuth
  callback handler) passes **plaintext** tokens into the repository's
  write method and never handles ciphertext directly, mirroring how
  `task_catalog/repository.py` already owns its own persistence-format
  concerns (JSON-encoding tuples/dicts) rather than pushing that onto
  callers.
- **Load-only-at-point-of-use**: a decrypted value should be a
  short-lived local variable, used immediately for one outbound call,
  never attached to a long-lived object or returned to a caller that
  doesn't immediately use it.

### Secret-lifetime / leakage invariants (traced this turn)

Token lifecycle: OAuth response → application memory → encryption →
database → later decryption (at point of use only) → refresh request
→ re-encryption. Explicit invariants at every leak-prone point:
- **Logs**: never log a raw token value; log only
  `user_id`/`connection_id`/`connection_status`/timestamps.
- **Exceptions**: a `TokenDecryptionError` (or any other) message may
  reference a `connection_id`, never the ciphertext or any decrypted
  fragment.
- **Discord responses**: `chaster connect`/`status`/`disconnect`
  replies never include a token value — already naturally true, since
  none of these replies were ever designed to display credentials.
- **Working Memory / model prompts**: no code path in CHASTER-01A (or
  the CHASTER-01B design) ever passes a token into
  `ResponseContextSnapshot`/prompt construction at all.
- **`repr()`/dataclass output**: any future connection dataclass must
  exclude token fields from its default `repr()` (Python dataclasses
  include all fields by default) — an explicit requirement for
  whoever implements it, since an innocuous `print()`/traceback could
  otherwise surface ciphertext (or, if a bug ever handed it a
  plaintext value by mistake, plaintext) in a log or test failure
  message.
- **Test failures**: the same `repr()` requirement protects pytest's
  own assertion-rewriting output; test fixtures must use obviously-
  fake key/token material, never anything resembling a real key
  format.
- **Database query logging**: this project has no SQL query logging
  today (confirmed absent); if one is ever added, it must exclude
  token-bearing tables or be disabled for them.

## 11. Chaster identity mapping

- The stable identity stored is Chaster's own account `_id`
  (`UserForPublic`/`CurrentUser`'s own field in the OpenAPI schema) —
  never `username` (mutable) and never anything Discord-side.
- One Discord identity (our own `UserAccount.id`, never a raw Discord
  ID) maps to **at most one active connection** at a time — a real
  uniqueness constraint, mirroring `task_runtime`'s own "one active
  assignment" pattern.
- If a new OAuth grant resolves to a *different* Chaster account than
  an existing connection: require explicit `chaster disconnect`
  first, rather than silently replacing one Chaster identity with
  another under the hood.
- Reconnecting to the *same* account (e.g. after a revoked/expired
  token) is treated as a refresh of the existing row, not a new one.

### Identity resolution mechanism — CONFIRMED (this turn)

**`GET /auth/profile`** — confirmed directly from the official
OpenAPI 3.0 specification (`https://api.chaster.app/api-json`,
`info.title: "Chaster"`, `servers: [{"url": "https://api.chaster.app"}]`
— the same specification `docs.chaster.app/api/public-api/endpoints`
itself names as authoritative). Exact facts, quoted from the spec
itself:

- **Path/method**: `GET /auth/profile`.
- **`operationId`**: `AuthMeController_me`.
- **`summary`**: `"Get logged user information"`.
- **Response schema**: `CurrentUser` (HTTP 200).
- **`security`**: `[{"oauth2": []}, {"bearer": []}]` — authenticated
  access required (a valid OAuth2 access token or bearer token), with
  an **empty** scope array for the `oauth2` requirement. In this
  spec's own convention (confirmed by comparing against every other
  endpoint, which list real scope names like `["locks"]` when a
  specific scope is enforced), an empty scope array denotes "any
  authenticated token, no specific scope beyond authentication
  itself" — i.e. this endpoint does not appear to require the
  `profile` scope specifically, though **this specific inference
  about empty-array semantics is not itself spelled out in prose
  anywhere in the docs** and should be verified empirically once a
  real developer application exists (Milestone C).
- **Tag**: `"Profile"`.
- A second, related endpoint also exists: **`GET /auth/profile/update`**
  (`operationId: AuthMeController_getUpdatedProfile`, summary
  `"Update profile from the authentication server"`, response schema
  `CurrentUserForProfileSettings`) — its exact relationship to
  `/auth/profile` (e.g. whether it forces a fresh fetch from the SSO
  server rather than a cached value) is not spelled out in the
  `summary` field alone and is not needed for CHASTER-01A's purpose.

**What is NOT yet confirmed**: the exact JSON field names inside the
`CurrentUser` response body. The OpenAPI document is very large, and
the fetch that retrieved it was truncated before reaching that
specific schema's definition. **A second, independent fetch attempt
(this time requesting a substantially larger content limit) was made
in a later verification turn and truncated at the exact same point**
— confirming this is a genuine document-size limitation in the
retrieval tooling available, not a one-off fetch failure or a gap in
the research effort. `CurrentUser` *is* referenced in the spec (as
`GET /auth/profile`'s own declared response schema) — it is not
absent from the API — its own field definitions were simply never
reached. A strongly-suggestive but **not identical** analogous
schema, `UserForPublic`, *is* fully confirmed in the same document
and includes (among many other fields): `_id: string ("The user
id")` and `username: string ("The username")` — Chaster's own
consistent naming convention for a user identifier and display name
everywhere else in this same specification. **Per explicit
instruction, these `UserForPublic` fields are recorded here for
context only and are NOT used to infer `CurrentUser`'s own fields —
implementing a resolver on this basis would still be a guess.** This
one remaining detail must be confirmed either by a follow-up
documentation fetch or empirically, once a real developer
application exists, before writing the field-mapping code.
**Every avenue available in this environment has now been tried and
exhausted**: the raw OpenAPI JSON (twice, both truncating at the
identical point before `CurrentUser`'s own definition), and the
interactive Swagger UI at `https://api.chaster.app/api` (its raw HTML
is a client-side-rendered single-page app shell with no schema
content of its own — it fetches and renders the same `api-json`
document via JavaScript, which is not executable by the tools
available in this environment). A targeted web search for
`CurrentUser`/`AuthMeController_me` surfaced no authoritative first-
or second-party source either — only an unofficial, third-party PHP
OAuth client library (`austomos/oauth2-chaster-app`), which is
explicitly excluded as evidence by this project's own research
discipline. **The identity-resolution blocker therefore remains
open** — `unconfirmed_identity_resolver` was not replaced this turn,
and resolving this specific remaining detail now requires either
direct access to a real, approved Chaster developer application
(to call `GET /auth/profile` empirically) or a different information
source not available in this environment.

**Scope reference** (`docs.chaster.app/api/reference/scopes`,
fetched directly, official): the `profile` scope is documented as
*"Access the username and email"* — confirming that requesting
`profile` (alongside `locks`) during the OAuth authorization request
is the officially documented way to be granted access to identity
data, independent of the exact endpoint's own scope-enforcement
behavior noted above.

**Suitability for CHASTER-01A**: `GET /auth/profile` is called with a
bearer token — exactly the access token CHASTER-01A already has in
hand immediately after a successful code exchange (Section 7). No
new OAuth round-trip, no new consent screen, no additional endpoint
type. This is appropriate to call immediately after token exchange,
before persisting the connection — precisely where
`unconfirmed_identity_resolver` is already wired in
(`chaster/callback_service.py::ChasterCallbackService.handle_callback()`).

**Does this change the implementation plan?** No architectural
change — the existing `resolve_identity` injection point in
`ChasterCallbackService` is exactly the right shape for this. The
**smallest change**, once the remaining field-name detail is
confirmed, would be: implement a new function (e.g.
`chaster/oauth_client.py::ChasterOAuthClient.get_current_user_identity(access_token)`
or a sibling module) that calls `GET /auth/profile` with the fresh
access token and maps the confirmed field(s) to `ChasterIdentity`,
then swap `unconfirmed_identity_resolver` for this real
implementation at the one call site in `bot/discord_bot.py`'s
composition root. No schema change, no new table, no change to
`ChasterCallbackService`'s own signature — `resolve_identity`'s
existing `Callable[[str], ChasterIdentity]` shape already fits a
`GET /auth/profile` call exactly (it takes an access token, returns
an identity). **Not implemented this turn**, per instruction — the
one remaining unconfirmed detail (exact response field names) should
be resolved first.

## 12. Provider-reported state

Conceptual fields (no schema is created by this document — see
Section 18):

- `provider` — a literal identifying which provider produced this row
  (e.g. `"chaster"`) — kept as a real, explicit column so the shape
  stays provider-agnostic even though only one provider exists today;
  a row is never ambiguous about its source.
- `status` — one of `locked` / `unlocked` / `deserted`, stored
  verbatim, Chaster's own three real values.
- `fetched_at` — **our own clock's timestamp of when we successfully
  received this response.** Explicitly documented, everywhere this
  value is used or displayed, as "when we asked and got an answer" —
  never presented as when the underlying lock event itself happened,
  since no such timestamp is confirmed to exist on Chaster's side.
- `source_connection_id` — always traceable to a specific stored
  connection; no anonymous/unattributed provider data.
- `chaster_lock_id` — the specific Chaster lock's own id (nullable in
  CHASTER-01, which may only ever track one/the primary lock, but the
  field exists so a later multi-lock extension needs no schema
  change).

## 13. `deserted`

Kept and stored as its own, distinct value — **never** coerced to or
displayed as `unlocked`. No policy exists yet (or is created by this
document) for what `deserted` should mean for any future eligibility
use; until one is explicitly approved, `deserted` is purely
informational, exactly like `locked`/`unlocked` are in CHASTER-01.

## 14. Freshness/staleness

- **Fresh observation**: shown with its real `fetched_at`; no
  numeric "freshness threshold" is defined in CHASTER-01, since
  nothing yet gates on staleness — the raw timestamp is always shown
  and the user judges for themselves.
- **Stale observation**: an older `fetched_at` still on file — shown
  with its real age, never silently treated as current.
- **No observation**: no provider-state row exists yet — shown as "no
  Chaster data yet," the same epistemic shape as `LockKnowledgeState.
  UNKNOWN`'s own "no trustworthy report exists" framing.
- **API failure / outage** (network error, 5xx, timeout): the live
  check failed; the last known observation (if any) is shown,
  explicitly labeled with its own age — never presented as current.
- **Expired access token**: a refresh is attempted transparently
  before failing.
- **Revoked/invalid grant** (refresh itself fails): connection status
  moves to `needs_reauthorization`; the user is told to reconnect;
  this is never treated as a lock-state signal of any kind.
- **Rate limit** (`429`/rate-limit headers): surfaced as "try again
  shortly," never silently retried in a way that could confuse
  timing, never treated as a state signal.

**Explicit invariant, restated because it matters most**: stale ≠
unlocked. API failure ≠ unlocked. No connection ≠ unlocked. Expired
credentials ≠ unlocked. `deserted` ≠ automatically unlocked. Every
failure/absence case fails closed — it produces an honest "we don't
know" or "here's the last thing we knew, and how old it is," never a
default assumption in either direction.

## 15. Conflict with user-reported state

Example: `LockKnowledgeState.LOCKED_USER_REPORTED` vs. a fresh
`PROVIDER_REPORTED: unlocked` (or the reverse).

- Both facts are stored and displayed **independently** — never
  merged into a new combined stored state.
- `chaster status` (conceptual) shows both side by side, neither
  framed as authoritative: "You've reported: locked" / "Chaster
  reports: unlocked (as of `fetched_at`)".
- `task request`/eligibility: **completely unaffected**.
  `task_runtime/eligibility.py` reads only `LockKnowledgeState`,
  exactly as today, conflict or not.
- Ordinary conversation: any future provider-state context fragment
  would render as its own separate DATA line, following the exact
  existing multi-fragment pattern (`lock_state` + `active_task`
  already coexist this way) — never silently resolved by the model.
- CHASTER-01 **reports only; it never reconciles or acts.**

## 16. Polling

**Recommendation: on-demand only** (a live call made when
`chaster status` is invoked), not background/scheduled polling.

- Freshness: as fresh as the moment of the command.
- Rate limits: negligible at this scale (100 req/min per IP is the
  documented global limit).
- Complexity: no scheduler/background-job infrastructure needed —
  this project has none today, for anything.
- Failure behavior: surfaced immediately, in the same interaction.
- Credential exposure: tokens touched only when actually needed —
  and naturally compatible with the ~30-minute refresh-token window
  (Section 6), since nothing needs to keep a session "warm" between
  uses.
- Background polling's main theoretical benefit (ambient freshness)
  requires proactive Discord messaging to be worth anything, and this
  project has none (confirmed in an earlier audit: nothing calls
  Conversation Engine on a timer). No interval is proposed, since
  none is needed.

## 17. Task Runtime boundary

`task_runtime/eligibility.py` is **not modified** by CHASTER-01. No
new implicit lock requirement is introduced. `REQUIRES_LOCKED`
eligibility continues to depend exclusively on `LockKnowledgeState`
(user-reported). Provider state never overwrites, gates, or overrides
anything in `task_runtime` in this slice. A future, separately
approved slice may define how provider state participates in
eligibility — CHASTER-01 makes no such decision and reserves no
implicit default.

## 18. Discord UX (conceptual only — no command implemented)

- **`chaster connect`**: replies with a one-time authorization link
  (DM preferred, to limit exposure of the state-bearing URL); on
  success, confirms connection (e.g. "Connected to Chaster as
  `<username>`"); on failure, a clear, specific-enough message
  (expired link, denied, or a generic retry prompt).
- **`chaster status`**: rejects cleanly if not connected; otherwise
  shows connection status, the fetched provider status verbatim
  (including `deserted`), `fetched_at`, and — if a user-reported lock
  state also exists — both values shown side by side, unedited.
- **`chaster disconnect`**: local-only (Section 19) — deletes stored
  credentials, states plainly that Chaster-side revocation is not
  guaranteed.

## 18a. Connection lifecycle (`chaster_connections` state machine)

Re-examined directly against the exact schema (Section 21) this
turn — the lifecycle turns out to need **fewer stored states** than a
naive first pass would suggest, because several of the states you
listed are actually *row existence*, not a status value inside a row:

- **Not connected** — no row exists in `chaster_connections` for this
  `user_id`. Not a stored status.
- **Authorization in progress** — a row exists in `chaster_oauth_states`
  for this `user_id`, but **no** row yet in `chaster_connections`.
  This confirms your own instinct: authorization-in-progress belongs
  **exclusively** to `chaster_oauth_states`, never to
  `chaster_connections` — the two tables are never both "about" the
  same in-progress attempt at once.
- **Connected** — a `chaster_connections` row exists with
  `connection_status = 'active'`.
- **Token refresh required** — **not a separate stored state.** A
  refresh is attempted transparently, automatically, at the moment a
  token is needed (Section 14) — this is runtime behavior, not
  something persisted. Only if the refresh itself *fails* does the
  row's status change.
- **Disconnected** — no row exists (Section 19: disconnect deletes
  the row entirely). Identical, at the schema level, to "not
  connected" — there is no persisted trace distinguishing "never
  connected" from "connected once, then disconnected."
- **Unusable/corrupt credentials** — maps to the *same*
  `needs_reauthorization` status already used for a failed refresh
  (Section 10's own reasoning: a decryption failure is treated the
  same way, reusing this status rather than inventing a new one).

**`connection_status` therefore needs exactly two values:
`active` and `needs_reauthorization`** — not five. This mirrors this
project's own repeatedly-demonstrated preference for the smallest
state model that's actually needed (`task_runtime`'s own "exactly
three states, exactly two transitions... deliberately not added 'for
the future'").

## 19. Disconnect semantics

**Local-only**, as decided: deletes locally stored Chaster
credentials, stops any future use of that connection, and tells the
user plainly that this cannot guarantee revocation on Chaster's side,
since no official revocation endpoint is confirmed to exist. The
system must never claim `"Chaster access has been revoked"` unless
that has actually been verified against a real, confirmed mechanism —
which does not exist today. If Chaster's documentation later confirms
a real revocation endpoint, extending disconnect to call it is a
separate, later slice.

## 20. Threat model

- **CSRF / OAuth code replay**: mitigated by the single-use,
  short-TTL, server-side `state` value (Section 7) plus Chaster's own
  single-use/10-minute-expiry authorization codes.
- **Open redirect**: the `redirect_uri` is fixed and pre-registered
  with Chaster; our own callback handler does not forward or redirect
  based on any user-supplied URL.
- **Forged callback / callback enumeration**: a forged callback
  without a valid, matching, not-yet-consumed `state` is rejected;
  `state` values are high-entropy and never predictable/enumerable.
- **Account-linking confusion**: prevented by binding `state` to the
  Discord user who initiated the flow (Section 7), never inferred
  from the browser session.
- **Credential leakage** (including via logs/exceptions): mitigated
  by encryption at rest, load-at-point-of-use only, and an explicit
  rule that no logging or exception-handling code may interpolate a
  raw token value — the same "reference by ID, never by raw secret"
  discipline already used for consent IDs elsewhere in this project.
- **SSRF**: all outbound Chaster calls target a fixed, known Chaster
  API host — no user-supplied URL is ever used to construct an
  outbound request.
- **Malicious/compromised provider response**: a Chaster response is
  external content and is read defensively — only the specific,
  expected enum-shaped fields are trusted; anything malformed or
  unexpected fails closed as an API failure rather than being
  accepted as-is.
- **Prompt injection via external data**: any Chaster-derived data
  that ever reaches a Conversation Engine prompt (not part of
  CHASTER-01 itself) must be treated exactly like task
  title/instructions already are — inert DATA, never an instruction —
  per the Plugin Architecture's own PLUG-8 principle ("content from
  an external source is always untrusted, labeled data").
- **Stale data / API outage / rate-limit exhaustion**: covered in
  Section 14; on-demand-only polling (Section 16) keeps request
  volume trivial regardless.
- **Unauthorized cross-user access**: every connection/observation
  row is keyed by our own internal `user_id` (never raw Discord ID),
  matching the access-isolation discipline already used for lock
  reports, task assignments, and preferences throughout this project.
- **Secrets in environment variables**: `CHASTER_CLIENT_ID`/
  `CHASTER_CLIENT_SECRET`/`CHASTER_REDIRECT_URI` would live in
  `.env`/`Config`, following the exact existing pattern
  `DISCORD_TOKEN`/`OLLAMA_HOST` already use — not a new pattern.
- **HTTPS termination**: handled entirely by the tunnel provider
  (Section 8's Option C) — the local listener itself only needs to
  speak plain HTTP to the tunnel's own local endpoint, which is the
  standard shape for this kind of tunneling tool.
- **Home-network exposure via a local HTTP server (specifically
  assessed, per instruction)**: the on-demand, short-lived nature of
  the tunnel/listener (Section 8's recommendation) keeps this
  materially smaller than a permanently-listening endpoint would be —
  the exposure window is minutes per connection attempt, not
  continuous. This is judged proportionate for this project's actual
  scale and use pattern; a permanently-open Option A deployment would
  not have been.

## 21. Database proposal (exact DDL for CHASTER-01A — migration NOT created)

Confirmed this turn directly from `database/migrations/011_application_layer.sql`
(the canonical identity tables) and `013_user_preferences.sql`/
`020_task_runtime.sql` (the two existing FK/PK idioms): every domain
table that needs "the current user's own row" references
`user_accounts.id` — never a raw Discord snowflake — via either
`user_id TEXT PRIMARY KEY REFERENCES user_accounts(id)` (a strict
one-row-per-user table, `user_preferences`' own pattern) or
`user_id TEXT NOT NULL REFERENCES user_accounts(id)` plus a separate
`id` PK (a table that legitimately accumulates multiple historical
rows per user, `task_assignments`' own pattern). **CHASTER-01A's two
new tables are both genuinely one-row-per-user** (a connection is
replaced or deleted, never accumulated; a pending OAuth state is
replaced, never queued) — so both use the `user_preferences` idiom,
not the `task_assignments` one. This is a real, evidence-based design
choice, not copied blindly from whichever table was read most
recently.

### `chaster_oauth_states`

```sql
CREATE TABLE IF NOT EXISTS chaster_oauth_states (
    state           TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL UNIQUE REFERENCES user_accounts(id),
    created_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL
);
```

- `state` (PK) — the random token itself, doubling as the lookup key;
  no separate `id` needed.
- `user_id` (**UNIQUE**) — this single constraint is what enforces
  "one pending state per user" at the database level, not merely in
  application logic (the same "database guarantee, not a
  repository-level check that could race" discipline migration 020's
  own comment already praises for `task_assignments`).
- `created_at`/`expires_at` — needed to enforce the ~10-minute window
  (Section 6) at lookup time.
- No index beyond the PK/UNIQUE (SQLite creates both implicitly).
- **Not encrypted** (Section 10) — a high-entropy random value with
  no other sensitive payload; its security property is
  unguessability, not database confidentiality.
- **Cleanup**: no scheduled job needed for CHASTER-01A (consistent
  with "no background jobs," already decided) — the `UNIQUE(user_id)`
  constraint itself bounds accumulation to at most one stale row per
  user who has ever abandoned an attempt; a genuine retention sweep is
  explicitly deferred, same as `chaster_provider_observations`'.

### `chaster_connections`

```sql
CREATE TABLE IF NOT EXISTS chaster_connections (
    user_id                     TEXT PRIMARY KEY REFERENCES user_accounts(id),
    chaster_account_id          TEXT NOT NULL,
    chaster_username            TEXT,
    encrypted_access_token      TEXT NOT NULL,
    encrypted_refresh_token     TEXT NOT NULL,
    encryption_key_version      TEXT,
    access_token_expires_at     TEXT NOT NULL,
    refresh_token_expires_at    TEXT NOT NULL,
    granted_scopes_json         TEXT NOT NULL,
    connection_status           TEXT NOT NULL,
    created_at                  TEXT NOT NULL,
    updated_at                  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chaster_connections_chaster_account
    ON chaster_connections(chaster_account_id);
```

Per-column justification (none included merely because it "might be
useful"):
- `user_id` (PK) — the canonical identity; also directly enforces "at
  most one connection per Discord identity" as the primary key
  itself, no separate constraint needed.
- `chaster_account_id` (indexed, **not** made UNIQUE — see Open
  Decisions) — needed to detect a genuinely *different* Chaster
  account on reconnect (Section 11's "require explicit disconnect
  first" rule cannot be enforced without storing this).
- `chaster_username` — a real, stated need: the `chaster status` UX
  ("Connected to Chaster as `<username>`," Section 18) requires it;
  not merely "might be useful."
- `encrypted_access_token`/`encrypted_refresh_token` (**TEXT, NOT
  NULL**) — the actual credential material this table exists to
  store. TEXT because a Fernet token is already URL-safe base64 ASCII
  — no BLOB needed. NOT NULL because a row is only ever created
  atomically with both real tokens (Section 7) — no valid
  intermediate state exists where a row has one but not the other, or
  neither.
- *(no separate nonce/IV or authentication-tag column — both are
  embedded inside the Fernet token itself, Section 10)*
- `encryption_key_version` (nullable, unused in CHASTER-01A) —
  reserved for a future rotation slice so it needs no migration of
  its own just to add this column.
- `access_token_expires_at`/`refresh_token_expires_at` — needed to
  decide whether a stored token is still usable or a refresh must be
  attempted first (Section 14's failure model depends on this
  directly).
- `granted_scopes_json` — what was *actually* granted may differ from
  what was requested (a user could modify the consent screen
  selection); needed to know what the connection can really be used
  for.
- `connection_status` — see the connection lifecycle below; exactly
  two values, not more.
- `created_at`/`updated_at` — the same audit-timestamp convention
  every other table already uses.

**Not included, deliberately**: a separate `id` column (unnecessary —
`user_id` already uniquely identifies the row and serves as PK); a
`chaster_lock_id` (belongs to `chaster_provider_observations`,
CHASTER-01B, not this table).

### `chaster_provider_observations` (CHASTER-01B — unchanged from the prior revision, included here only for completeness of the three-table picture)

```sql
CREATE TABLE IF NOT EXISTS chaster_provider_observations (
    id              TEXT PRIMARY KEY,
    connection_id   TEXT NOT NULL REFERENCES chaster_connections(user_id),
    provider        TEXT NOT NULL,
    chaster_lock_id TEXT,
    status          TEXT NOT NULL,
    fetched_at      TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
```

**Append-only**, matching `LockReport`/`TaskTemplateVersion`'s own
established discipline — "current provider state" is simply the most
recent row for a connection, read the same way `LockState.
get_current_knowledge_state()` already works for user reports. No
foreign key from any of the three new tables into
`lock_state`/`task_runtime` — kept structurally separate. Retention
policy for this log is explicitly deferred (Section 22). **This table
is CHASTER-01B's own scope, not created as part of CHASTER-01A.**

## 22. Failure modes


Covered in detail in Section 14 (staleness/failure) and Section 8
(callback-service unavailability). Summarized invariant: every
failure mode produces an honest, explicit "we don't know" or "here is
the last thing we knew and how old it is" — never a default
assumption toward `unlocked`, `locked`, or "verified."

## 23. Implementation sequence (proposed, not mandatory)

- **CHASTER-01A** — OAuth connection foundation: new `chaster/`
  package, config additions, one migration for `chaster_connections`,
  the on-demand tunnel/listener mechanics. Highest-risk piece,
  isolated first.
- **CHASTER-01B** — read-only provider state: the actual `GET /locks`
  call, `chaster_provider_observations` table, `chaster status`
  wiring. Depends on 01A.
- **CHASTER-01C** — `chaster disconnect`. Small, depends on 01A, no
  migration.

This split is a suggestion for reducing per-step risk, not a
requirement — CHASTER-01 as a whole is already a small slice.

## 24. Chaster API approval dependency

- **Buildable/testable without approved API access**: the OAuth
  `state` generation/storage logic, the on-demand tunnel start/stop
  mechanics, the encrypted-credential-storage code path, and the full
  callback handler logic *except* the two real network calls (token
  exchange, profile fetch) — those two can be tested against a local
  mock server standing in for Chaster's token/profile endpoints,
  using the exact contract already confirmed from the official
  OpenAPI spec. This matches this project's own existing precedent of
  testing external dependencies via fakes/mocks rather than live
  calls (e.g. Ollama itself is never called live in the existing test
  suite).
- **Needs real Chaster credentials**: only the final, one-time,
  manual check that a real `chaster connect` against Chaster's actual
  OAuth server and a real account succeeds end-to-end — this requires
  an approved developer application (client ID/secret) already
  registered, which itself requires the beta-access approval process
  to have completed.
- **What must happen before any real connection attempt**: (1) submit
  the Chaster developer-access request form — unknown turnaround
  time, so this should happen as early as possible, independent of
  when code is written; (2) once approved, register the application
  in Chaster's developer interface and obtain client ID/secret; (3)
  register the exact, fixed `redirect_uri` the chosen tunnel produces
  — reinforcing why Section 8's recommendation specifically requires
  a *stable* hostname, not a rotating one.
- CHASTER-01A/01B's own code and automated tests are not blocked by
  approval status; only the final live-account verification is.

### Domain/callback prerequisite — exact shape (this turn, no domain assumed or requested)

- **Example callback hostname shape**: `chaster-callback.<a domain
  the operator can add one CNAME record to>` — a suggested label, not
  a required exact string; fully configurable.
- **Exact callback URI structure**: `https://<hostname>/oauth/chaster/callback`
  — one single, fixed path. No wildcard, no per-user path segment;
  the user is identified by the `state` parameter (Section 7), never
  by anything in the URL path itself.
- **Does the application need the full public callback URL?** Yes,
  as a configuration value (`CHASTER_REDIRECT_URI` in `.env`) used
  both to build the authorization URL sent to Chaster and to compare
  against what Chaster's own developer dashboard has registered —
  these two must always match exactly (general OAuth practice;
  Chaster's own exact-match strictness remains unconfirmed, Section
  5, and must not be assumed either way).
- **Must the hostname be configurable?** Yes — via `.env`, never
  hardcoded, exactly like every other environment-specific value in
  this project.
- **Can the local callback port be fixed?** Yes, a sensible default,
  but still configurable via `.env` (e.g. `CHASTER_CALLBACK_BIND_PORT`)
  in case of a local port conflict on the operator's machine —
  configuration, not a secret (already classified this way in the
  deployment-readiness audit).
- **What stays external to the repository**: `cloudflared`'s own
  config file and tunnel credentials, and the CNAME DNS record itself
  — none of this lives in application source control; the repository
  only needs to know the *resulting* public hostname/URL as a config
  value, never how that hostname is technically achieved.

## 25. Development vs. personal-deployment environments

Two explicitly separate environments, with no secret shared between
them:

### Development

- **Goal**: exercise the OAuth state machine, callback handling, and
  failure modes without ever reaching the real Chaster API or
  exposing the operator's PC publicly.
- Chaster's token/profile endpoints are **mocked** locally (matching
  this project's own existing precedent — e.g. Ollama is never called
  live in the test suite either), using the exact request/response
  shapes already confirmed from the official OpenAPI spec.
- The local HTTP listener can be exercised directly on `localhost`
  (no tunnel needed at all for most of this) since nothing here
  requires a real, internet-reachable callback — only the
  request-handling *logic* is under test, not a real browser redirect
  from Chaster.
- A **separate Chaster developer application** (its own client
  ID/secret, if Chaster's developer interface allows more than one
  application per account — not confirmed either way) would be used
  for the rare cases where a real end-to-end check against Chaster's
  actual sandbox/beta environment is wanted, kept distinct from
  whatever application is eventually used for personal deployment.
- State validation, token exchange, and every documented failure mode
  (Section 22) are all testable this way, fully automated, with no
  dependency on network access or approval status.

### Personal deployment

- **Goal**: the real Discord bot, a real Chaster OAuth connection, a
  stable callback URL via the recommended named Cloudflare Tunnel
  (Section 8), running under the existing Windows Scheduled Task
  (Section 4).
- Uses the **real, approved** Chaster developer application's client
  ID/secret — never the development mock configuration.
- The tunnel's stable hostname is the one actually registered with
  Chaster as the app's `redirect_uri`.

### The explicit separation requirement

Development secrets (a mock/sandbox client ID/secret, if one is even
used) must never be reused as the personal-deployment application's
real credentials, and vice versa — the same discipline this project
already applies to `DISCORD_TOKEN`/`OLLAMA_HOST` never being
hardcoded, always sourced from `Config`/`.env`. Config values for
each environment would live in their own `.env` (development) vs. the
real, gitignored `.env` (personal deployment) — no new mechanism
needed beyond what `core/config.py` already does.

## 25a. Testability milestones

Defined so the operator does not have to wait for the entire
integration before testing anything real:

**Milestone A — pure local unit/integration tests, mocked Chaster
OAuth.** Prerequisites: `cryptography` approved and added, migration
022 created, `TokenEncryptor` implemented, `chaster_oauth_states`/
`chaster_connections` repository code implemented, a local mock
standing in for Chaster's token/profile endpoints. No domain, no
Cloudflare, no real Chaster application needed at all.

**Milestone B — local bot + callback listener, no Cloudflare.**
Everything in A, plus the real `aiohttp` listener wired into
`setup_hook()`/`close()`, exercised by hitting
`http://localhost:<port>/oauth/chaster/callback` directly in a test —
simulating exactly what Chaster's redirect *would* send, without
actually involving Chaster or a tunnel. Still no domain/Cloudflare/
real Chaster application needed.

**Milestone C — real OAuth through Cloudflare Tunnel + a real Chaster
application.** Requires: confirmed domain/CNAME access, an actual
configured Cloudflare named tunnel (external to this repository), and
a real, approved Chaster developer application (client ID/secret,
registered redirect URI matching the tunnel's hostname exactly). This
is the first point real end-user credentials and a live browser flow
are involved — and the first point the still-unconfirmed redirect-URI
matching behavior (Section 5) can actually be observed rather than
guessed.

**Milestone D — real Chaster lock-state integration (CHASTER-01B).**
Requires: Milestone C working end-to-end, plus CHASTER-01B's own
separate design and approval — entirely out of scope for CHASTER-01A.

### Real-provider prerequisites checklist (audited this turn, code found already correct)

**Can be tested now, without any Chaster credentials** (all already
exercised by the 1273-test suite):
- OAuth `state` lifecycle — creation, replace-on-reconnect, atomic
  single-use consumption (proven with real concurrent threads),
  expiry.
- `TokenEncryptor` encryption/decryption, including tamper/wrong-key/
  malformed-ciphertext rejection.
- The real `aiohttp` callback listener — startup, shutdown, a
  simulated bind failure never preventing Discord from starting,
  malformed/duplicate requests, a real HTTP round-trip against
  `localhost`.
- Token persistence and reconnect-replaces-not-accumulates behavior,
  using mocked Chaster token responses.
- Every secret-leakage invariant (no plaintext in the raw stored row,
  in logs, in exception text, in `repr()`).
- `chaster connect`'s deterministic routing, identity binding, and
  "already connected"/"not configured" replies.

**Requires a real, approved Chaster developer application**:
- `CHASTER_CLIENT_ID` / `CHASTER_CLIENT_SECRET` — only obtainable
  after Chaster approves the developer-access request and an
  application is created in their developer interface.
- The Chaster access-approval process itself (beta, unknown
  turnaround — Section 24).
- A registered redirect URI, exactly matching `CHASTER_REDIRECT_URI`.
- **The unresolved real `GET /auth/profile` → `CurrentUser` response**
  — this project's own research exhausted every retrieval avenue
  available in this environment (the raw OpenAPI JSON at two content
  limits, the Swagger UI, and a targeted web search) without reaching
  `CurrentUser`'s field definitions from an authoritative source; only
  an empirical call against a real account (or a different
  information source, e.g. Chaster's own `#developers` Discord
  channel) can resolve it.

**Requires external hostname/tunnel infrastructure**:
- A domain/subdomain the operator can add one CNAME record to
  (Section 8, Part B — a single record, not full nameserver
  migration).
- A configured Cloudflare named tunnel (external to this repository —
  `cloudflared`'s own config/credentials, never read by this
  project's Python code).
- **What the current local-only process genuinely cannot do without
  this**: receive Chaster's own redirect at all. `CHASTER_REDIRECT_URI`
  must be a real, internet-reachable HTTPS URL for Chaster to ever
  successfully deliver a callback — `http://127.0.0.1:8420` (the
  local bind address) is never itself usable as the registered
  redirect URI; the tunnel is what turns the local listener into
  something Chaster's own servers can actually reach.

**Not claimed as confirmed**: Chaster's exact redirect-URI matching
behavior (exact-match enforcement, `localhost` support, wildcards,
multiple registered URIs per application) remains explicitly
**NOT CONFIRMED BY OFFICIAL SOURCE** (Section 8) — this checklist
does not assert otherwise; it will only be observable once Milestone
C is actually attempted.

## 25b. Safety-override architectural requirement (design only — not implemented)

> **Deployment gate: no real personal Chaster deployment until the
> local emergency-unlock mechanism has been implemented,
> independently authenticated/authorized, audited, and thoroughly
> tested.** This is a hard precondition, not a nice-to-have — CHASTER-01A
> being complete and locally validated does not itself clear this
> gate. **Status as of this turn: the safety-control plane itself is
> now implemented and locally tested (Section 25c) — but the gate is
> still NOT cleared.** A passing local/fake-provider test proves the
> authentication, audit, and provider-boundary logic behaves
> correctly; it does not prove Chaster itself can be remotely
> unlocked, since the real Chaster unlock operation remains
> unconfirmed and unimplemented (Section 25c's own explicit
> statement). The gate closes only once a real, confirmed provider
> implementation exists and has itself been thoroughly tested.

A hard requirement, not optional: the future real Chaster integration
must be accompanied by a **PC-local emergency-control path**,
architecturally independent of everything else being built here.

- **Where it belongs**: its own, wholly separate mechanism — not
  inside the `chaster/` package, not a Discord command, and not
  dependent on this bot's own process being healthy. The entire
  reason this exists is for the case where Discord, the bot process,
  or the network is *not* working — a mechanism that only works
  through the same process it's meant to be a fallback for is not a
  real safety boundary.
- **Independent of Discord**: yes, explicitly. Discord's own
  availability, and this bot process's own health, must never be a
  single point of failure for the operator's ability to act.
- **Localhost-only**: yes, absolutely — and architecturally
  guaranteed, not merely configured that way. Given this project is,
  for the first time, adding a genuine public-facing surface (the
  Cloudflare Tunnel), the emergency path must never share that
  listener, port, or process — the strongest guarantee against
  accidental exposure (e.g. an overly broad tunnel ingress rule) is
  for it to not be reachable over any network socket the tunnel could
  ever be pointed at, ideally not a network listener at all.
- **Authentication/authorization**: OS-level access control — only
  usable by whoever is physically logged into the same Windows
  account the bot runs under — not a password or token scheme, which
  could itself be lost, forgotten, or phished. The same "physical
  presence is itself the authorization" principle this whole design
  already relies on for the interactive OAuth flow (Section 8).
- **Auditing**: a local log entry (timestamp, what was done) via this
  project's own existing `logging` conventions — never dependent on
  Chaster/Discord/network availability to actually record.
- **Inaccessible through Cloudflare/public HTTP**: guaranteed by
  never being part of the same `aiohttp` application the tunnel
  exposes — a genuinely separate mechanism, not a route that merely
  checks for a localhost caller.
- **Interaction with a future Chaster provider command/API**:
  **explicitly PENDING OFFICIAL API CONFIRMATION.** Nothing in the
  research so far (confirmed scopes: `locks`, `keyholder` — both
  read/manage-oriented) has confirmed whether Chaster's API offers
  any kind of remote release/unlock capability at all. This is not
  guessed here. If one is ever confirmed, the local emergency
  mechanism could potentially also *invoke* it as an additional,
  belt-and-suspenders measure — but the local mechanism's own value
  never depends on that, since its entire purpose is to work even
  when Chaster/Discord/the network is unavailable.
- **Explicit distinctions**:
  - *Emergency local control*: PC-local, offline-capable, physical-
    presence-authorized, independent of Chaster/Discord.
  - *Normal keyholder control*: whatever a keyholder does directly
    through Chaster's own interface — not something this bot manages
    at all.
  - *AI decisions*: Scarlett/the model has **no** authority here,
    exactly as everywhere else in this project — the emergency
    mechanism is not something the model can invoke or influence.
  - *Chaster provider state*: the read-only, `PROVIDER_REPORTED`
    observation this whole CHASTER-01/01B design carefully scopes —
    entirely separate from any control/override capability, which
    this project has never otherwise proposed building.

**Implemented as of this turn** (`chaster/emergency_unlock*.py`) —
see Section 25c for the exact architecture actually built. The real
Chaster remote-unlock operation itself remains unconfirmed and
unimplemented; only the safety-control plane around it exists.

## 25c. Emergency-unlock safety plane — implemented architecture

**A separate control plane, deliberately not part of the normal one:**

| | Normal control plane | Emergency safety plane |
|---|---|---|
| Process | `bot/discord_bot.py` (Discord bot) | `chaster/emergency_unlock_server.py` — a genuinely separate OS process (`python3 -m chaster.emergency_unlock_server`), never imported by or importing the bot process |
| Surface | Discord messages, `chaster connect` | One local HTTP route, `POST /emergency/unlock` |
| Authority | Governed by `_SYSTEM_BOUNDARIES`, personality, Conversation Engine | No AI/model involvement at all — confirmed structurally: neither `chaster/emergency_unlock.py` nor its listener/server import `discord`, `conversation_engine`, or `application` (this project's own test suite enforces this via AST inspection of actual import statements, not a docstring claim) |
| Authentication | Discord identity (`user_channel_identities`) | A dedicated, separate secret (`CHASTER_EMERGENCY_UNLOCK_SECRET`) — never the Chaster client secret, never the token-encryption key |
| Availability | Requires the bot process to be healthy | Deliberately independent of the bot process's health — this is the entire reason it is a separate process |

### Option A implementation (this turn) — active-lock discovery + real emergency-unlock call

**Implemented and tested** (67 new tests across `test_lock_client.py`,
`test_real_emergency_unlock_provider.py`, plus fixes to the existing
suite): `chaster/lock_client.py::ChasterLockClient` calls the two
confirmed endpoints directly —

- `GET /locks?status=active` (`operationId: LockController_findAll`)
  for discovery — the `status=active` parameter is itself officially
  documented (enum `active`/`archived`/`all`, default `active`),
  used explicitly here rather than relying only on the documented
  default.
- `POST /locks/{lockId}/emergency-unlock`
  (`operationId: LockController_emergencyUnlock`) for the actual
  unlock attempt, only ever called with a `lockId` the provider
  itself discovered moments earlier — never from any caller.

`chaster/emergency_unlock_provider.py::RealChasterEmergencyUnlockProvider`
implements the full Option A control flow, enforced exactly as
specified: `len(active_locks) == 0` → fail safely ("No active
Chaster lock found"); `len(active_locks) > 1` → fail safely with the
exact candidate count, never selecting one ("Found N active Chaster
locks — cannot determine an unambiguous target"); exactly one →
proceed to an eligibility check. Every HTTP outcome from the real
`emergency-unlock` call maps to a distinct, honest result — `204` is
the *only* success; `400`/`401`/`403`/`404`/anything else is failure,
each with its own accurate detail text (a `404` is explicitly
labeled as the target having "changed since discovery," never
silently retried against a different lock). A dedicated race/
stale-target test proves this directly: a lock present at discovery
that then 404s on the actual unlock call is reported as an honest
failure, with discovery called exactly once — no re-discovery, no
fallback to the general `/unlock` endpoint, no second guess.

**The one genuine, still-open research gap, isolated rather than
guessed**: this project has not confirmed `LockForWearer`'s exact
field-level JSON schema (id/type/bondage-config field names) from
any authoritative first-party source — a further, dedicated attempt
this turn (raw OpenAPI JSON, the Swagger UI, and a targeted search)
found only third-party SDKs, explicitly excluded as evidence.
`chaster/emergency_unlock_provider.py::unconfirmed_lock_field_extractor`
isolates this exact gap — it always raises
`LockFieldsUnconfirmedError`, and is the production default injected
into `RealChasterEmergencyUnlockProvider` (see
`chaster/emergency_unlock_server.py::build_listener()`). **This means
a real emergency-unlock request today will discover the active
lock(s) for real, enforce the exactly-one invariant for real, and
then fail safely at the eligibility-check step** — the same category
of gap, and the same honest-isolation treatment, as
`chaster/callback_service.py`'s own `unconfirmed_identity_resolver`.
Filling this in (replacing `unconfirmed_lock_field_extractor` with a
real implementation) is the smallest remaining step toward a fully
real emergency-unlock path, and requires either the exact schema
being confirmed from an authoritative source or empirical
confirmation once a real developer application and account exist.

**`UnwiredEmergencyUnlockProvider`** (renamed, unchanged in spirit)
now serves only as a test/fixture provider guaranteed to never touch
the network at all — no longer the production default.

**Authorization is exactly one operation, nothing broader**:
`EmergencyUnlockService.request_unlock()` is the only public method;
`EmergencyUnlockProvider.attempt_unlock(access_token)` is the only
method the provider seam exposes — no arbitrary lock ID, no arbitrary
provider API path, no general administrative capability of any kind.
"Higher priority than normal restrictions" is realized narrowly: this
plane bypasses the *normal control plane* (Discord/AI), not
arbitrary application state — it cannot touch `lock_state`,
`task_runtime`, preferences, or anything else in this codebase; its
only possible effect is the single confirmed-or-not unlock attempt.

### Authentication mechanism — decision and reasoning

**Chosen: a dedicated local secret** (`CHASTER_EMERGENCY_UNLOCK_SECRET`),
compared with `secrets.compare_digest` (constant-time). Evaluated
against the same three options considered for the master encryption
key (Section 10), with the same conclusion for the same reasons:

- **Windows-user/session binding** (DPAPI, named-pipe ACLs) — rejected
  again: a new platform-specific dependency and code path, for a
  marginal gain on a deployment that is already single-user by design
  (`windows/README.md`'s own `AtLogOn` model). The emergency plane's
  own process-separation already delivers the property this option
  would have added (availability independent of the bot process);
  adding OS-level binding on top would be genuine complexity for a
  security property this design does not need.
- **A separate credential file** (distinct from `.env`) — rejected:
  no real isolation gain over a `.env` variable given both live on
  the same machine, under the same OS user's file access, as every
  other secret this project already stores this way.
- **A dedicated `.env` secret, distinct from every other secret in
  this project** — **chosen**. Matches this project's own established
  `Config`/`.env` pattern exactly (no new mechanism to build, test, or
  document). Resists *accidental* invocation (nothing about it could
  be triggered by an ordinary typo or misclick). Deliberately kept
  fully separate from `chaster_client_secret`/
  `chaster_token_encryption_key` so that compromising one of those
  does not also compromise the emergency path — restated because it
  is the concrete security property that most directly serves this
  plane's own purpose (staying trustworthy even when something else
  has gone wrong).

### Local interface

`chaster/emergency_unlock_listener.py::EmergencyUnlockListener` —
its own, separate `aiohttp.web.Application` (never shared with
`chaster/callback_listener.py`'s own OAuth-callback application),
bound to `CHASTER_EMERGENCY_BIND_HOST` (default `127.0.0.1`) and
`CHASTER_EMERGENCY_BIND_PORT` (default `8421` — deliberately distinct
from the OAuth callback's own `8420`, so the two can run
simultaneously without conflict if ever desired, though they are
independent processes and need not run together at all). One route,
`POST /emergency/unlock`, accepting exactly `{"secret": ..., "user_id":
...}` — no other parameter is read or honored. Never routed through
Cloudflare Tunnel — the tunnel's own configuration (external to this
repository, Section 8) would need to deliberately target the OAuth
callback's own port to reach anything; nothing about the emergency
listener's port is ever mentioned to, or reachable by, the tunnel
unless the operator explicitly and separately misconfigured it to be
— a configuration this design does not produce or suggest.

### Emergency action semantics

Exactly the sequence Section 6 of the assignment for this turn
specified, implemented literally: authenticate → write an audit event
→ (if authenticated) look up the Chaster connection → write an audit
event → (if a connection exists) decrypt only the access token,
immediately before use → attempt the provider operation → write a
final audit event recording the real outcome → return a safe,
human-readable result. Distinct terminal statuses, matching the
assignment's own requested vocabulary: `AUTH_FAILED`, `NO_CONNECTION`,
`PROVIDER_SUCCEEDED`, `PROVIDER_FAILED` — `REQUEST_ACCEPTED`/
`PROVIDER_ATTEMPTED` are the two *intermediate* audit events written
en route to one of the four terminal ones, not additional terminal
states themselves.

### Provider abstraction — explicit statement on the real Chaster unlock API

**UPDATE (this turn) — the real Chaster remote-unlock operation IS
now confirmed to exist**, superseding the previous "not confirmed"
statement below (kept for its own historical accuracy). Confirmed
directly from the official OpenAPI specification
(`https://api.chaster.app/api-json`, re-fetched fresh this turn):

- **`POST /locks/{lockId}/unlock`** — `operationId:
  LockController_unlock`, summary "Unlock a lock", description
  *"Unlocks a lock. For wearers, the lock must respect certain
  constraints so that it can be unlocked."* Responses: `204` success,
  `400` *"Some extensions prevent the unlocking of the lock"*, `401`,
  `404`. **Required scope: `locks`** (`security:
  [{"oauth2":["locks"]},{"bearer":[]}]`) — already the exact scope
  this project's OAuth client already requests by default
  (`chaster/oauth_client.py::build_authorization_url()`'s own
  `scopes: tuple[str, ...] = ("locks",)`) — **no scope mismatch, no
  OAuth redesign needed.**
- **`POST /locks/{lockId}/emergency-unlock`** — `operationId:
  LockController_emergencyUnlock`, summary "Emergency-unlock a
  bondage lock", description *"Emergency-unlocks a bondage lock.
  Wearer-only safety release that bypasses the normal unlock
  constraints; requires at least one safety feature enabled in the
  bondage config."* Responses: `204`, `400` *"The lock is not a
  bondage lock, has no safety feature enabled, or is not locked"`,
  `401`, `403` *"Only the wearer can emergency-unlock"*, `404`. Same
  required scope: `locks`. This is, by name and description, exactly
  the officially-intended counterpart to this project's own PC-local
  safety plane — a wearer-initiated safety release, not a
  keyholder-initiated one.
- **Important, confirmed constraint**: `/emergency-unlock` only works
  for locks of type `bondage` (confirmed via `LockTypeEnum: [chastity,
  bondage]`) that have `emergencyReleaseEnabled: true` in their
  `BondageSessionConfig` (also confirmed schema — includes
  `emergencyReleaseCountdownSeconds`, `deadManSwitchEnabled`,
  `autoUnlockEnabled`). **It is not confirmed to apply to `chastity`-
  type locks at all** — for those, only the general `/unlock`
  endpoint (itself explicitly "constrained," i.e. not a guaranteed
  bypass) would apply.

**A genuine, new design decision this finding surfaces — NOT resolved
this turn**: both endpoints require a specific `lockId` in the URL
path. `EmergencyUnlockProvider.attempt_unlock(access_token)`'s
current seam accepts only an access token — **no lock identifier at
all**. `GET /locks` (already-confirmed, `locks`-scoped) can return
**multiple** locks for one account. Which lock the local safety plane
should target — the account's single active lock (when exactly one
exists), a specific lock the operator designates in advance (via a
new, not-yet-designed configuration/command), or some other rule — is
a genuine product/safety decision, not something to infer silently.
See Section 26's open decisions for the exact options presented.

**Historical statement (accurate as of last turn, now superseded by
the above)**: earlier research found no officially documented unlock
endpoint — that was correct at the time given what had actually been
checked (the `keyholder` scope's own description, "View and manage
locked users," does not itself confirm an unlock capability, and
nothing was guessed from it). This turn's dedicated, focused check of
the Locks section of the OpenAPI paths did find the two endpoints
above.

`chaster/emergency_unlock_provider.py::NotConfirmedEmergencyUnlockProvider`
**remains the only provider wired into production** — it was
deliberately not replaced this turn, because doing so would require
resolving the target-lock-semantics decision above, which this
research alone cannot settle. It never makes any HTTP call to
Chaster, and always returns `FAILED` with an explicit, honest reason.
**A passing fake-provider test does not constitute proof that
Chaster itself can be remotely unlocked** — the 39 tests from the
prior slice prove the safety-control plane behaves correctly around
whatever provider it's given; they prove nothing about Chaster's own
API on their own — though the API itself is now independently
confirmed to have a matching operation, per the above.

### Audit design — reusing existing infrastructure, no new migration

Every request writes to `domain_events` (`infrastructure/outbox.py`,
already existing since migration `002`) via the same `write_event()`/
`DomainEvent` this project's other modules already use for
cross-module events — `source_module="chaster_emergency"`,
`event_type` one of `chaster_emergency.{request_accepted,
auth_failed, no_connection, provider_attempted, provider_succeeded,
provider_failed}`. **No new migration was needed or created.**
`payload_json` carries only `user_id`/`chaster_account_id`/a short,
safe `detail` string — never a token, never a secret, never raw
provider response content (confirmed directly by tests reading the
raw stored payload). Append-only, matching every other audit-shaped
table in this project (`LockReport`, `TaskTemplateVersion`).

### Failure semantics

Every failure mode fails to a named, distinct, honest terminal
status — never silently reported as success:

- **Unauthenticated/unauthorized**: `AUTH_FAILED` — includes both a
  wrong secret and a genuinely unconfigured one (fails closed, never
  "anything is accepted because nothing is configured").
- **No connection exists**: `NO_CONNECTION`.
- **Provider unavailable/timeout/rejected**: all map to
  `PROVIDER_FAILED` (the provider seam models "could not reach it" and
  "it explicitly rejected the request" identically, distinguished only
  by the human-readable `detail` string — since the safety
  consequence for the operator is the same either way: the unlock did
  not happen).
- **Malformed local request**: HTTP `400`, rejected before
  authentication is even attempted.
- **Local database failure**: an unhandled exception during audit
  writing propagates rather than being swallowed — this deliberately
  does NOT catch-and-continue, since a database that cannot record
  what happened must not silently report a result the operator would
  have no way to later verify actually happened.
- **Application/bot process unavailable**: does not affect this
  plane at all — the entire point of the separate-process design.

## 25d. First Real Chaster Provider Test Plan

A controlled, safe, reproducible sequence for the eventual first real
test — **not to be run until an approved Chaster developer
application exists.** No step here asks for a secret to be pasted
into Discord, chat, or any AI tool; every credential stays entirely
within your own local `.env`/terminal.

### Developer-application findings (confirmed this turn, official docs.chaster.app)

- Developer **area** access requires a form-based approval request
  first (`docs.chaster.app/api/basics/getting-started`) — *"You will
  first need access to the developer area. You can request it by
  filling out this form. Describe what you want to create."* The API
  is explicitly still in beta.
- Only **after** that approval can you create an application
  (`docs.chaster.app/api/basics/create-application`) — confirmed
  steps: open the developer interface, click "Create an application,"
  enter a name, save. **Redirect-URI registration is not shown in
  this specific step's own description** — it is very likely a
  separate settings field on the created application (standard OAuth
  practice, and this project's own `chaster_integration_technical_design.md`
  Section 5 already flags redirect-URI behavior as otherwise
  unconfirmed) but this was **not directly confirmed** by what this
  search retrieved — check the actual developer interface once access
  is granted, rather than assuming.
- Once approved, you also get access to a **private `#developers`
  Discord channel** for API questions — a legitimate first-party
  support channel this project has no access to.
- Two authentication paths remain confirmed distinct: a **developer
  token** (tied to your own account only, no OAuth needed — useful
  for the manual research below) and **OAuth 2** (for other users,
  what CHASTER-01A's own `chaster connect` uses).

### Phase 1 — OAuth / profile

> **Faster path for schema discovery alone, confirmed via the same
> official Endpoints page already cited above** (*"you must obtain a
> developer token or an access token... pass your token in the HTTP
> Authorization header, prefixed with Bearer"*): `fetch_raw_profile()`
> and `list_active_locks()` both simply take a bearer token string —
> neither knows or cares whether it came from OAuth or from a
> developer token. **If your only goal right now is resolving the
> `CurrentUser`/`LockForWearer` schemas (Phase 1 step 5, Phase 2 steps
> 1–2), you do not need to complete steps 1–4 below, run the callback
> listener, or set up Cloudflare Tunnel at all** — generate a
> developer token directly from the Chaster developer interface (tied
> to your own account) and call `fetch_raw_profile(access_token=<dev_token>)`/
> `list_active_locks(access_token=<dev_token>)` straight from a local
> Python REPL. The full sequence below (steps 1–4, and Cloudflare) is
> only actually required for the separate goal of a real *per-Discord-
> user* connection via `chaster connect` — not for schema discovery
> itself.

1. Populate local `.env`: `CHASTER_CLIENT_ID`, `CHASTER_CLIENT_SECRET`,
   `CHASTER_REDIRECT_URI` (matching exactly what's registered with
   Chaster), `CHASTER_TOKEN_ENCRYPTION_KEY` (generated once, per
   `chaster/README.md`'s own instructions).
2. Start the bot process — `setup_hook()` starts the OAuth callback
   listener automatically once these are set (Section 9).
3. Send `chaster connect` in Discord; open the returned authorization
   URL in a browser; complete Chaster's own login/consent screen.
4. Observe the callback complete (state consumed, code exchanged for
   real tokens — both fully implemented and real today).
5. **To capture the real `/auth/profile` response safely**: from a
   local Python REPL or a throwaway, uncommitted script — never
   committed to the repository, never pasted into Discord/chat — call
   `ChasterOAuthClient.fetch_raw_profile(access_token=...)` (new this
   turn, `chaster/oauth_client.py`) using the real access token your
   own script obtained via `exchange_code_for_tokens()`. This method
   is a development-only diagnostic, confirmed never referenced by
   the real production callback flow (a dedicated test enforces
   this). It returns the raw, unparsed response dict — inspect its
   keys locally; do not print/log/commit the actual values if they
   include personally identifying data (email, etc.).
6. Use that real, observed shape to write the real `resolve_identity`
   implementation, replacing `unconfirmed_identity_resolver` at its
   one call site (`bot/discord_bot.py`'s composition root) — a
   separate, later implementation step, not part of this test plan
   itself.

### Phase 2 — lock schema

1. Once a real connection exists (Phase 1 complete and a real
   implementation wired in), make one authenticated `GET /locks`
   call — `ChasterLockClient.list_active_locks(access_token=...)`
   already returns the raw, unparsed list today, with **zero
   dependency on any unconfirmed field** (Section 25c's own
   "Option A implementation" subsection) — this call is already
   safe to make for real right now, independent of Phase 1.
2. Inspect the real response locally to determine the exact field
   names for: the lock's own identifier, its type
   (`LockTypeEnum: chastity | bondage`), and its bondage-safety
   configuration (`emergencyReleaseEnabled`, confirmed to exist as a
   *concept* in `BondageSessionConfig`, though its exact containing
   field path within a `LockForWearer` object is what remains
   unconfirmed).
3. Implement `unconfirmed_lock_field_extractor`'s real replacement
   using only those confirmed field names — no other field should be
   read speculatively.
4. Add fixtures/tests built from the **real, confirmed shape** — not
   real account data. Never commit an actual raw response, a real
   lock ID, or any other account-identifying value.

### Phase 3 — emergency-unlock provider

Only after Phases 1–2 are both complete:

1. Test active-lock discovery against the real account (already safe
   to do today, per Phase 2 point 1).
2. Test the exactly-one-candidate invariant with the real account's
   actual lock count.
3. Test eligibility using the now-real extractor.
4. **Only when explicitly intending to actually test the real
   emergency-unlock call** — this is the one step with a real,
   irreversible side effect — invoke it deliberately, on a test lock
   that actually satisfies the documented prerequisites (bondage type,
   emergency release enabled), and verify: the real Chaster response,
   the audit trail (`domain_events`, `source_module='chaster_emergency'`),
   and that failure paths (a lock lacking the safety feature, etc.)
   behave exactly as this project's own mocked tests already predict.

**Do not trigger a real emergency-unlock request merely as a side
effect of testing OAuth or discovery** — Phases 1–2 never call
`emergency_unlock()` at all; only Phase 3's own explicit, deliberate
step does.

### Cloudflare prerequisites (not implemented this turn)

**Not a prerequisite for the schema-discovery diagnostic work above**
(Phase 1's own callout box) — only for a real, per-Discord-user
`chaster connect` connection. Once a real Chaster application exists
and you're ready for that separate goal: a domain/subdomain you can
add one CNAME record to (Section 8, Part B), a configured Cloudflare
named tunnel routing **only** to the local OAuth callback listener's
own port (`CHASTER_CALLBACK_BIND_PORT`, default `8420`) — **the
emergency listener's own port (`CHASTER_EMERGENCY_BIND_PORT`, default
`8421`) must never be included in any tunnel ingress rule** — and
`CHASTER_REDIRECT_URI` set to the resulting stable public URL,
registered exactly in the Chaster developer interface. None of this
is implemented in this repository; all of it is external
configuration, by design (Section 8's own repository/Cloudflare
boundary).

## 25e. First real developer-token test — result and diagnosis in progress

**Actual result of the first real, read-only test against Chaster's
Public API** (developer token, entered only via local `getpass`,
never seen by this project's own tooling):

- `GET /auth/profile` → **HTTP 400**
- `GET /locks?status=active` → **HTTP 400**
- No write/unlock endpoint was called (confirmed structurally — the
  diagnostic script contains zero `requests.post/put/patch/delete`
  calls anywhere).

### CONFIRMED

- Both requests target the exact, officially-confirmed endpoints and
  HTTP methods (Section 11, Section 25c's own "Option A
  implementation" subsection) — no wrong URL/path/method.
- The token is sent as `Authorization: Bearer <token>` — matching
  Chaster's own official example on `docs.chaster.app/api/public-api/endpoints/`
  exactly for the *auth scheme* itself.
- **Chaster's own official example request on that same page includes
  a `Content-type: application/json` header on its GET example** —
  our current production clients (`chaster/oauth_client.py::fetch_raw_profile()`,
  `chaster/lock_client.py::list_active_locks()`) send **only** the
  `Authorization` header, no `Content-type` at all.
- HTTP 400 (Bad Request) is, by standard HTTP semantics, conventionally
  associated with a malformed/invalid *request* — as distinct from 401
  (unauthenticated) or 403 (forbidden), which are the more typical
  codes for an authentication/authorization problem specifically. Both
  endpoints failing identically with the same code is consistent with
  something common to both requests (the request construction itself,
  not an endpoint-specific parameter) being the issue.

### INFERENCE (not confirmed — the actual redacted response body, now
capturable via the diagnostic fallback below, is what would confirm
or refute this)

- The missing `Content-type: application/json` header — present in
  Chaster's own official example but absent from our current
  requests — is the most parsimonious candidate explanation, given
  it's a genuine difference from the documented example and would
  plausibly affect both endpoints identically. **This is a hypothesis
  to be tested empirically, not a confirmed root cause.**
- A malformed Authorization header value (e.g. stray whitespace/
  control characters from copy-paste) is a real, distinct possibility
  that would also affect both endpoints identically and would also
  plausibly produce a 400 rather than a 401 — cannot be distinguished
  from the header-based hypothesis without seeing the actual response
  body.

### UNKNOWN

- Whether Chaster's backend actually requires `Content-type` on a GET
  request, or whether its example simply always includes that header
  as a matter of style regardless of necessity.
- Whether a developer token has any usage nuance distinct from an
  OAuth access token beyond how it's obtained (nothing in the official
  docs already reviewed suggests a difference beyond issuance) — not
  contradicted by anything found, but not separately confirmed either.
- The exact Chaster-provided error message/body content — not yet
  seen, since the production clients discard it by design.

### Diagnostic fix made this turn (scoped entirely to the diagnostic script)

`scripts/dev_chaster_schema_probe.py` gained a read-only diagnostic
fallback, `_print_diagnostic_response()`, invoked only when a primary
(production-helper) call already failed: it makes one additional GET
to the same confirmed endpoint and prints the real, redacted status
code, headers, and JSON body — something the production clients
deliberately never expose (they intentionally discard the body by
design, Section 25c). It also tries **both** header variants — current
production headers, and current headers plus `Content-type:
application/json` — so the next real run settles the INFERENCE above
empirically rather than by further guessing. **No production client
in `chaster/oauth_client.py`/`chaster/lock_client.py` was modified** —
per instruction, if the real diagnosis shows the `Content-type` header
(or anything else) is genuinely required, that would be a separate,
explicitly-approved follow-up change to the production clients, not
made here.

### Update — real curl.exe request succeeded; User-Agent identified as the leading, still-unconfirmed candidate

**A real Windows `curl.exe` request to the identical endpoint
succeeded (HTTP 200) with a real developer token that our Python
`requests`-based client, calling the same URL with the same token,
still returns HTTP 400 for.** This conclusively rules out (per the
Chaster developer's own direct confirmation) an invalid token,
missing API access, wrong endpoint, wrong query parameter, or general
API unavailability. HTTP/2 vs HTTP/1.1 was separately ruled out by
the same developer (their curl works on both). The `Content-type`
hypothesis (previous subsection) is also independently ruled out —
already empirically disproven by your own prior test.

**CONFIRMED, via direct inspection of the real `requests.PreparedRequest`**
(`session.prepare_request()`, no network call made) — our Python
client sends several headers curl's own shown request does not:

| Header | curl (known working) | Python `requests` (default) |
|---|---|---|
| `User-Agent` | `curl/8.14.1` | `python-requests/<version>` |
| `Accept` | `*/*` | `*/*` (same) |
| `Accept-Encoding` | *(not shown/sent)* | `gzip, deflate` |
| `Connection` | *(not shown)* | `keep-alive` |
| `Authorization` | `Bearer <REDACTED>` | `Bearer <REDACTED>` (same) |

**INFERENCE, not yet confirmed as the root cause**: `User-Agent:
python-requests/<version>` is a very commonly targeted signature for
WAF/bot-mitigation rules specifically because it unambiguously
identifies the most common Python HTTP library's own unmodified
default, whereas `curl/<version>` is typically treated as a
legitimate developer tool. This is consistent with, and reinforces,
this section's own earlier structural finding (empty-body/no-
Content-Type 400s not matching how this API documents its own
application-level 400 errors anywhere else in its OpenAPI spec) —
both point toward an edge/infrastructure-level block rather than an
application-level validation failure. **Not yet proven**: no real
request with a modified User-Agent has yet been made.

`scripts/dev_chaster_schema_probe.py` gained a `curl_equivalent`
header-style mode, scoped to `/locks?status=active` only (per
instruction — `/auth/profile` is deliberately not touched by this
specific diagnostic pass, pending the `/locks` result first): it
reproduces curl's exact header set (`requests` fully removes a
default header when its value is explicitly set to `None` — verified
directly against a real, unmocked `PreparedRequest`) alongside the
current default-header variant, so the next real run settles the
User-Agent hypothesis empirically. **No production client was
modified** — `chaster/lock_client.py` still sends exactly what it
sent before; a fix will only be made once this hypothesis is actually
confirmed by a real result showing the curl-equivalent variant
succeeding where the default variant still fails.

## 26. CHASTER-01A boundary and open decisions

### CHASTER-01A boundary (approved scope for the first implementation slice)

CHASTER-01A is **"OAuth connection foundation"** only. It should
eventually provide:
1. initiating a per-user Chaster OAuth authorization flow,
2. receiving the OAuth callback,
3. exchanging the authorization code for tokens,
4. persisting the connection securely,
5. associating the connection with the Discord user,
6. exposing connection state internally (for a later CHASTER-01B to
   read).

It explicitly does **not** yet: fetch Chaster locks, interpret lock
status, create provider observations, touch `lock_state` or
`task_runtime` in any way, expose provider state to the model, or
implement anything beyond the local-only disconnect semantics already
decided (Section 19). `chaster_provider_observations` (Section 21)
belongs to CHASTER-01B, not this slice — CHASTER-01A only needs
`chaster_oauth_states` and `chaster_connections`.

### Command architecture fit (audited this turn, no change needed)

`chaster connect`/`chaster status`/`chaster disconnect` fit the
*existing*, unmodified command router exactly like `lock`/`task`/
`mode` already do: `router.register("chaster connect", ..., handler)`
for each exact command, plus `router.register_family("chaster",
invalid_handler=...)` for the bare word — already benefiting from the
Command Family Fallback Precision fix (bare `"chaster"` triggers the
family reply; anything else not an exact match falls through to
Conversation Engine, exactly as `task`/`lock`/`mode` do today). Since
`CommandRouter.route()` checks exact matches before any conversational
text ever reaches `ConversationEngine`, there is no code path by
which `chaster connect` could accidentally become model-generated
free-form behavior — this is already guaranteed by the existing,
unmodified router, not something CHASTER-01A needs to newly enforce.

### Open decisions (recommended for approval — see below for exact form)

1. **Whether `chaster_account_id` should be UNIQUE** on
   `chaster_connections` (i.e. can one Chaster account ever be linked
   to more than one Discord identity at once) — a genuinely new
   question surfaced by this turn's exact-schema audit, not
   previously decided. Left un-constrained (indexed, not unique) for
   now as the more conservative default; flagged rather than silently
   decided.
2. **Chaster developer-application registration timing** — when to
   actually submit the access-request form and register the
   application, given its unknown approval turnaround (Section 24).
3. **Confirming domain/CNAME access** (already surfaced in the
   deployment-readiness audit) — the one non-code prerequisite for
   Option C.
4. **The PC-local emergency-control mechanism itself** (Section 25b)
   — a hard requirement, design-only so far; its exact implementation
   is a separate, later piece of work, explicitly not part of
   CHASTER-01A's own scope but required before real deployment.
5. **`GET /auth/profile`'s exact `CurrentUser` response field names**
   (Section 11's new "Identity resolution mechanism" subsection) —
   the endpoint itself is confirmed via the official OpenAPI spec;
   only the precise JSON field names remain to be confirmed (a second
   documentation fetch, or empirically once a real developer
   application exists) before `unconfirmed_identity_resolver` can be
   replaced with a real implementation. **Re-checked this turn** via
   one genuinely new source (`docs.chaster.app/api/reference/action-logs`,
   not previously fetched) — did not resolve it; still unresolved.
   The already-exhausted avenues (raw `api-json` at two content
   limits, the Swagger UI shell, generic search) were deliberately
   not repeated, per instruction.
6. ~~**Target-lock semantics for the confirmed emergency-unlock
   operation** (new this turn — see the "Provider abstraction"
   subsection above for the confirmed endpoint evidence). Both
   `POST /locks/{lockId}/unlock` and `POST
   /locks/{lockId}/emergency-unlock` require a specific `lockId`;
   `EmergencyUnlockProvider.attempt_unlock(access_token)` currently
   accepts none, and `GET /locks` can return multiple locks per
   account. **Genuinely undecided — presented as three options below,
   not silently resolved:**

   **Option A — discover the target lock from provider state at
   request time.** Call `GET /locks?status=active` (already
   `locks`-scoped, no new OAuth grant needed) at the moment of an
   emergency-unlock request and target whichever lock is found.
     - *Safety*: reasonable when exactly one active lock exists;
       genuinely ambiguous with more than one — must fail safely
       (`NO_CONNECTION`-style rejection), never guess which of
       several to target.
     - *Stale-target risk*: none — always fresh, since it's looked up
       live, not cached.
     - *Accidental-unlock risk*: low for a single-lock account;
       meaningfully higher for a multi-lock account if the "pick one"
       policy is ever made permissive rather than fail-closed.
     - *Auth/OAuth interaction*: none beyond the existing `locks`
       scope.
     - *Provider-observation interaction*: this would be the very
       first place this project ever calls `GET /locks` for a live
       read — a small, real expansion of CHASTER-01A's current
       read-only footprint, though still short of CHASTER-01B's own
       scope (storing/observing state), and still entirely separate
       from `lock_state`.
     - *No target / multiple targets*: must both fail safely and
       distinctly (a new audit-worthy outcome, not silently mapped to
       an existing one).
     - *After a lock is deserted/unlocked*: `status=active` filtering
       already excludes these, so the discovery step itself handles
       this naturally.
     - *Auditability*: straightforward — log the discovered `lockId`
       (not sensitive) alongside the existing audit trail.
     - *Implementation complexity*: low-to-moderate — one new read
       call plus explicit multi-result handling in
       `EmergencyUnlockService`.

   **Option B — bind to one explicitly pre-configured lock.** The
   operator designates a specific lock ID in advance (via a new,
   not-yet-designed configuration or command), stored durably
   (a new column, likely on `chaster_connections` — a schema change,
   though not necessarily a new table).
     - *Safety*: deterministic and simple to reason about once set.
     - *Stale-target risk*: real and must be handled explicitly — if
       the designated lock is later deserted/archived/replaced, the
       stored target silently goes stale unless something actively
       revalidates it; this is the option's own main weakness.
     - *Accidental-unlock risk*: low, given a fixed, deliberately
       chosen target.
     - *Auth/OAuth interaction*: none beyond `locks` scope.
     - *Provider-observation interaction*: could be set once at
       `chaster connect` time or via a later, separate command — but
       designing that lifecycle is itself additional, not-yet-scoped
       work.
     - *No target*: a config value never set — same
       `NO_CONNECTION`-style safe rejection.
     - *Multiple targets*: not applicable — one is chosen upfront by
       design.
     - *After deserted/unlocked*: the real risk case above — needs an
       explicit staleness check before ever attempting to use a
       stored target, itself a new piece of logic to design.
     - *Auditability*: straightforward.
     - *Implementation complexity*: higher than A — needs a schema
       change, a designation UX, and staleness handling.

   **Option C — no clearly superior alternative was found** this
   turn. A hybrid (discover automatically, but fall back to or
   confirm against an explicitly configured target) is conceivable
   but was not designed here, since it would inherit complexity from
   both A and B without a clear net safety benefit established yet.

   **No recommendation is made here deliberately** — per instruction,
   this is presented as a decision, not resolved by this research
   turn. `EmergencyUnlockProvider`'s interface remains unchanged
   (still `attempt_unlock(access_token)` only) until this is decided;
   changing it now would itself be a silent design choice.~~
   **RESOLVED — Option A selected and implemented** (see Section
   25c's own "Option A implementation" subsection for the exact,
   tested control flow). `EmergencyUnlockProvider`'s interface was
   deliberately kept unchanged, exactly as anticipated above — target
   selection lives entirely inside
   `RealChasterEmergencyUnlockProvider`, never exposed to any caller.
   The remaining gap is narrower now: not *which* lock to target
   (decided), but `LockForWearer`'s exact field-level schema
   (unconfirmed, isolated in `unconfirmed_lock_field_extractor`).

**Recommended for approval** (this turn's audit resolved what was
previously the single remaining architectural unknown):
- `cryptography` as a required runtime dependency (added to
  `requirements.txt` only once this is approved — not yet).
- Authenticated encryption via `cryptography.fernet.Fernet` (AES-128-CBC
  + HMAC-SHA256, with built-in nonce handling and tamper detection),
  not hand-rolled AES-GCM.
- One deployment master key, `CHASTER_TOKEN_ENCRYPTION_KEY`, supplied
  through `.env`/environment variable — the same mechanism
  `DISCORD_TOKEN` already uses — with the new, explicit operational
  requirement that it be backed up separately from the database.
- A reserved (currently unused, nullable) `encryption_key_version`
  column stored alongside encrypted token payloads, for a future
  rotation slice.
- Rotation tooling itself (`MultiFernet`, a re-encryption sweep)
  explicitly deferred beyond CHASTER-01A.
- A narrow `TokenEncryptor` abstraction, owned by the repository
  layer — application/service code never handles ciphertext directly.
- No plaintext token persistence, logging, or prompt exposure at any
  point in the traced lifecycle (Section 10).

### Test matrix (for CHASTER-01A, once approved — not added yet)

Encrypt/decrypt round trip; two encryptions of the same plaintext
produce different ciphertext (nonce uniqueness, trivially true with
Fernet's own internal IV generation); tampered ciphertext raises
`InvalidToken`; wrong key fails to decrypt; malformed/non-Fernet
payload is rejected; a missing/absent key fails fast at
`TokenEncryptor` construction, not at first use; unusual Unicode in a
token round-trips correctly; an empty-string token is rejected at
encryption time (mirroring this project's own established
empty/whitespace-rejection convention elsewhere); a repository-level
test reads the *raw* SQLite row directly and asserts the plaintext
value never appears in the stored bytes; a captured-log test asserts
a known plaintext value never appears in log output across an
encrypt/decrypt/failure path. Unsupported key version is explicitly
deferred (no versioning behavior exists to test until rotation itself
is built).

### Already determined by previously approved architecture (not reopened)

- `PROVIDER_REPORTED` naming; `deserted` kept distinct; provider state
  is future-eligibility-only via separate approval; local-only
  disconnect; never `VERIFIED`; `lock_state`/`task_runtime` untouched
  in CHASTER-01.
- On-demand polling (no background jobs).
- Option C (Cloudflare named tunnel), one process, `setup_hook()`/
  `close()` lifecycle, listener/tunnel is process-lifetime
  infrastructure (not per-attempt).
- OAuth `state`: single-use, deleted on lookup, one pending state per
  user, persisted in `chaster_oauth_states` (Section 21) — not
  in-process memory.
- Encrypted tokens never in Working Memory, Discord messages, logs,
  or model prompts.

## 27. Explicit invariants (restated, load-bearing)

- Internal naming: `PROVIDER_REPORTED`. Never `VERIFIED`.
- `deserted` is its own stored, displayed value — never coerced to
  `unlocked`.
- Provider state never overwrites, gates, or is read by
  `task_runtime/eligibility.py` in CHASTER-01.
- `LockKnowledgeState`/`lock_state`'s existing user-reported model is
  never modified by this work.
- Disconnect is local-only; the system never claims a remote
  revocation it cannot verify.
- Stale/absent/failed provider state never defaults to `unlocked` (or
  any other assumption) in either direction.
- A Chaster API response is external, untrusted content — inert data,
  never an instruction, if it ever reaches a model prompt.
- `core/config.py::Config.chaster_api_token` is untouched, unread,
  and not part of this design — it remains a single-account developer
  token, not the per-user credential model described here.
