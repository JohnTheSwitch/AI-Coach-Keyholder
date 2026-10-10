# Phase 3 — Physical Chaster Execution: Design Proposal (v0.1)

> **Status: proposal for owner review. No code, no migration, no OAuth change
> has been made.** High-risk phase: this introduces the project's first Chaster
> *write* path. Everything below marked **VERIFY** must be confirmed by a direct
> download of the official OpenAPI document (needs your approval, see section 9)
> before any code is written.

## 1. What is verified, and how

Sources: the official docs (`docs.chaster.app`) and the official OpenAPI
document (`api.chaster.app/api-json`), read through a summarising fetch tool.
Because that tool is a summariser, the exact schemas must be re-checked against
the raw JSON (**VERIFY**) before implementation.

| Fact | Source | Confidence |
|---|---|---|
| OAuth scopes: `profile`, `locks` ("View and edit locks"), `shared_locks`, `keyholder` ("View and manage locked users"), `messaging` | docs scopes page | high |
| `POST /locks/{lockId}/update-time` — "Adds or removes duration when the caller has the matching `lock.time.add` or `lock.time.remove` permission". Security: oauth2 `locks`. 204 OK; 400 "Cannot change a non-locked lock"; 401; 403 "Missing the lock.time.add or lock.time.remove permission"; 404 | OpenAPI | high on path/permissions; **request body (`UpdateTimeDto`) fields NOT confirmed** (the fetch showed an empty schema) |
| `POST /locks/{lockId}/freeze` — "Keyholders can use this endpoint to freeze wearer's locks". Body `SetFreezeDto { isFrozen: boolean }` (so unfreeze = `isFrozen:false`). Security: oauth2 `locks`. 204 / 400 / 401 / 403 "Missing the lock.freeze or lock.unfreeze permission" / 404 | OpenAPI | high |
| Extensions API alternative: `POST /api/extensions/sessions/{sessionId}/action`, body `{"action": {"name": "add_time" \| "remove_time", "params": <seconds>}}`, `{"name":"freeze"}`, `{"name":"unfreeze"}`; security oauth2 scope `developer`; 201 / 401 / 404; each action writes a lock-history entry visible to wearer and keyholder | docs lock-actions page + OpenAPI | high on shape; the extension identity/token model is **not** described |
| `LockForWearer` fields exist: `status` (locked / unlocked / deserted), `isFrozen`, `canBeUnlocked` (**deprecated**), `keyholder` (nullable), `endDate` (nullable date-time) | OpenAPI schema | high. This **confirms the field names** that were previously unconfirmed (still want a real sanitised sample for value semantics) |
| No idempotency key is documented for any of these calls; no rate limits are documented | docs | medium (absence of evidence) |
| The API is "in beta"; developer access is granted through an application form | docs getting-started | high |

Consequences: `canBeUnlocked` must not drive any rule (deprecated). Time is
specified in **seconds** (the engine's whole-hour decisions convert to
`hours * 3600`; VERIFY the unit on `update-time`).

## 2. The blocking question: whose authority does the call use?

Today the app holds the **wearer's** OAuth token (scope `locks`). The calls
above only succeed when the *caller* holds the right lock permission
(`lock.time.add/remove`, `lock.freeze/unfreeze`). A wearer's token on a
keyholder lock normally lacks them (and a wearer adding or removing their own
time defeats the point). You said the lock is controlled by "a keyholder
extension/bot", so three possible identities exist:

| Route | Token | Pros | Cons / unknowns |
|---|---|---|---|
| A. Public API as the **keyholder account** | OAuth token of the keyholder user (that account authorises this app; scope `locks`, maybe `keyholder`) | documented endpoints and codes; same OAuth plumbing | needs a *second* connection (keyholder account) — today's tables are one connection per user; permission model of that keyholder session unverified |
| B. Public API as the **wearer** | existing wearer token | no new plumbing | almost certainly 403 on a keyholder lock; and it is the wrong authority |
| C. **Extensions API** `…/sessions/{id}/action` | extension (`developer` scope) credentials | purpose-built for bot-driven actions; every action is attributed to the extension in the lock history | extension must go through Chaster's integration/approval process; token model undocumented; needs a session id per lock |

**Recommendation:** decide A vs C with you before anything else; B is out.
Everything else in this document is route-independent through a small
`LockActionTransport` interface (add/remove time, freeze/unfreeze), so only the
adapter differs.

## 3. Architecture

New package `chaster_execution/` (an isolated execution plane, like the
emergency-unlock plane). Rules:

- It consumes only **persisted `lock_decisions` rows** produced by the Phase 2
  engine. It exposes no function that accepts free text, a Discord message,
  a model output or ad-hoc parameters. `application/`, `bot/` message handlers
  and `conversation_engine/` never import it (AST guard).
- It imports nothing from `chaster.emergency_unlock*`, and
  `application/service.py` still never mentions the word "emergency".
- Only one component can reach the transport: the `Executor`.

### 3.1 Switches
- `ENABLE_RULES_ENGINE` (exists) records decisions. `ENABLE_CHASTER_EXECUTION`
  (new, default `false`) is the master switch. Off ⇒ decisions stay `dry_run`,
  no executor is built, no token is touched.
- **Runtime halt:** a one-row `execution_control` table (`halted`, reason,
  timestamp) checked immediately before every call, settable from a local script
  only (never Discord, never the model). Lets you stop execution without a
  restart; the env flag alone cannot do that.
- Optional per-action allow-list (`ENABLE_CHASTER_EXECUTION_ACTIONS`, default
  time actions only; freeze/unfreeze stay off until separately enabled).

### 3.2 Decision status machine
`lock_decisions.status` today is only `dry_run` (immutable row). Phase 3 allows
**one** writer, the Executor, to advance it along:

```
dry_run ──(execution on)──> pending ──> executing ──> executed
                               │            └──────> failed
                               ├──────────────────> blocked   (pre-flight refused)
                               ├──────────────────> expired   (decision too old)
                               └ executing (crash) ─> needs_review
```
Every transition also appends a row to a new append-only
`lock_decision_executions` audit table (attempt id, decision id, from/to status,
timestamp, HTTP status and a *sanitised* error class, `end_date_before`,
`end_date_after` when known, transport route). Tokens, headers and bodies are
never stored or logged.

### 3.3 Idempotency / no double execution
Chaster documents no idempotency key, so the guarantee is **at-most-once on our
side**:
1. In one transaction: re-check the decision is `pending` and no execution row
   exists (`UNIQUE(decision_id)`), then set `executing`. This claim happens
   **before** any network call.
2. Make the call.
3. Record the outcome (`executed` / `failed`).

A crash between 1 and 3 leaves `executing`: the Executor **never retries it
automatically**. Startup reconciliation reads the lock (read-only), compares
`endDate` / `isFrozen` with the recorded "before" values and marks the decision
`executed` (change observed), `failed` (no change) or `needs_review` (ambiguous),
all visible to you. Automatic retry is permitted only for failures that provably
occurred before the request was sent (no connection, token refresh failed). Any
5xx or timeout is treated as *unknown outcome* ⇒ reconciliation, not retry.

### 3.4 Pre-flight (every execution, immediately before the call)
- master switch on, runtime halt off, action allowed;
- decision status `pending` and not older than 15 minutes (else `expired`);
- **granted scopes** of the connection include what the route needs (stored in
  `chaster_connections.granted_scopes_json`); otherwise `blocked` with a clear
  reason — never a silent failure, and a re-authorisation is requested;
- a fresh lock snapshot (≤ 15 min) and the engine safety gates re-run against it:
  `locked`, keyholder lock, frozen state compatible with the action, and the lock
  id equals the one observed (exactly one active lock; the existing observation
  service already refuses to record when several are active);
- the **hard limits are re-applied against executed history** (not dry-run
  history), so caps/cooldown/frequency hold in live mode. Phase 2's dry-run
  history counting is switched off for limit purposes once execution is on, to
  avoid dry-run rows consuming the real budget.
- global concurrency of one in-flight call; a conservative call-rate ceiling
  (no documented Chaster limit, so we set our own).

### 3.5 Lock snapshot hydration (your approved route)
- Migration 026 adds nullable columns to `chaster_provider_observations`:
  `is_frozen`, `has_keyholder`, `can_be_unlocked` (stored but unused),
  `end_date`. Old rows keep NULLs ⇒ the engine fails closed on them.
- `LockSnapshot` is built from the latest persisted observation
  (`observed_at = fetched_at`). The recorder/service already maps
  `status`; it will additionally extract the four fields **only when present with
  the expected type**, otherwise NULL (same discipline as `llm_lock_view`).
- **Freshness caveat:** nothing refreshes observations on a schedule (the
  project deliberately has no background infrastructure). If the latest
  observation is older than 15 minutes, the Executor performs **one** read-only
  refresh through the existing self-healing fetch (this is the only live read, and
  it is a read). If that fails ⇒ `blocked`, not executed. So a live read at
  execution time is unavoidable for correctness, even though evaluation uses
  persisted data.
- Evaluation itself (Phase 2) keeps using persisted observations only.

### 3.6 Failure handling
`failed` and `blocked` decisions stay visible and are never deleted or silently
re-queued; a human decision (or a later, separately approved policy) is needed
to try again, and then a **new** decision is created rather than reusing the old
one. Distinguish: 401 (token — one refresh via existing self-healing, then
`failed`), 403 (permission/scope — `blocked`, route problem), 400 (not locked —
`blocked`), 404 (lock gone — `blocked`, observation stale), 5xx/timeout
(`needs_review`).

### 3.7 Triggering
The project has no scheduler. Options: (1) run the Executor after
`evaluate_pending` at startup only (simple, but actions would wait for a
restart); (2) a small asyncio periodic task started by the bot, guarded by the
existing startup-lease style single-instance lock. I recommend (2) for
Phase 3 live use, still gated by the switches; needs your decision.

## 4. Safety and isolation summary
Master switch off by default; runtime halt; per-action allow-list; decisions
only (no free-form path); at-most-once claim; scope check; freshness check;
limits re-applied on real history; full append-only audit; no token or secret in
any log, row or test; TLS verification untouched; emergency-unlock plane
untouched; code/combination endpoints (`/locks/{id}/combination`, recovery) are
**never** wrapped — the transport interface has exactly four methods.

## 5. Migrations
- 026: observation columns (section 3.5), `lock_decision_executions`,
  `execution_control`; `lock_decisions.status` gains the new values (no CHECK
  exists, so no rebuild). No other schema change.

## 6. Test plan (no real network, ever)
Fake transport. Cases: flag off ⇒ nothing built; halt flips mid-run; every
status transition and the illegal ones; double-execution attempts (same decision
twice, concurrent claims); crash simulation between claim and record ⇒
`needs_review`/reconciliation outcomes; stale decision ⇒ `expired`; missing scope
⇒ `blocked`; stale/refresh-failed snapshot ⇒ `blocked`; limits against executed
history only; hours→seconds conversion; request body shapes per route; HTTP
code mapping; no token in any log/audit row; AST guards (no import from
application/bot/conversation_engine/emergency modules; nothing outside wiring
imports `chaster_execution`); the existing `emergency` and `datetime.now`
guards stay green.

## 7. Rollout
0. Migration + hydration only; Phase 2 runs dry with **real** snapshots.
1. Live dry-run monitoring: compare each would-be action with the lock's real
   history for a period you choose; tune the `BOOTSTRAP_DEFAULT` thresholds here.
2. Execution on a dedicated test lock, smallest step (1 hour), time actions only.
3. Normal operation; freeze/unfreeze only after their own sign-off.

## 8. Decisions needed from you
1. **Route (section 2): A (keyholder-account OAuth), C (extension API), or
   something else?** Which Chaster account actually holds the keyholder role on
   your lock, and does a bot/extension already exist for it?
2. If A: approve a second connection record for the keyholder account
   (migration 026 would add it) and the extra scope(s) needed (`keyholder`?).
   If C: confirm the extension credentials/approval status and how a session id
   is obtained per lock.
3. Triggering: startup only, or a periodic task (section 3.7)?
4. Approve the status machine, the at-most-once policy (no automatic retry of
   ambiguous outcomes) and the runtime halt table.
5. Staleness windows: 15 min for lock data (approved) and 15 min for a pending
   decision — confirm or change the latter.
6. Allow-list: confirm freeze/unfreeze remain disabled at execution even when
   time actions are enabled.
7. **Permission to download the raw OpenAPI JSON** (api.chaster.app/api-json,
   ~400 KB) into the scratchpad to confirm `UpdateTimeDto`, the permission
   names and the unit before coding.
8. Optional: a sanitised real `GET /locks` sample (no ids, no tokens) to confirm
   value semantics of `isFrozen` / `keyholder` / `status`.

## 10. Implementation status (addendum)

Implemented per this proposal: migration 026, the status machine, audit log,
at-most-once claim, runtime halt (seeded halted), pre-flight, hydration from
persisted observations, the periodic cycle and the composition layer; see
`chaster_execution/README.md`.

**Finding that changes section 1:** the raw OpenAPI document (read directly, not
via a summariser) shows `UpdateTimeDto` as an object with **no properties**, so the
Public API's request body for `POST /locks/{lockId}/update-time` is undocumented.
The earlier "VERIFY" item resolved to: not specified. No request body is guessed;
the real transport is gated off (`chaster_execution/transport.py`). The documented
time bodies exist only for the Extensions API (Route C, scope `developer`, seconds).
Also confirmed in the raw spec: `LockForWearer` / `LockForKeyholder` carry `status`
(locked / unlocked / deserted), `isFrozen`, `keyholder` (nullable), `endDate`
(nullable date-time) and `canBeUnlocked` (deprecated); `SetFreezeDto` is
`{isFrozen: boolean}`; permission names `lock.time.add`, `lock.time.remove`,
`lock.freeze`, `lock.unfreeze` exist. `tests/` and the schema treat the keyholder
connection as an ordinary `chaster_connections` row under its own internal user id.

### 10.1 Update (transport unblocked by the owner)

The owner supplied the `update-time` contract: `POST /locks/{lockId}/update-time`,
Bearer token of the keyholder connection, body `{"duration": <seconds>}`, positive
adds and negative removes. This is **not** in Chaster's OpenAPI and is unverified;
`ChasterHttpTransport` implements it with the conservative outcome mapping in
`chaster_execution/README.md`, and the first live use must be a single 1-hour change
on a test lock checked against the end date before and after. Execution remains off
by default (`ENABLE_CHASTER_EXECUTION=false`, plus the seeded runtime halt).
