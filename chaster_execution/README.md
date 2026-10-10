# chaster_execution — Phase 3 (execution plane)

Canonical: `docs/architecture/chaster_execution_phase3_proposal.md` (approved).
Applies Rules Engine decisions to a Chaster lock. **Built, tested and wired, but
the real Chaster transport is deliberately unavailable** (see "Blocker").

## Blocker: the update-time request body is undocumented

Read directly from Chaster's OpenAPI (`api.chaster.app/api-json`, v0.35.0):
`POST /locks/{lockId}/update-time` (scope `locks`; needs the caller to hold
`lock.time.add` / `lock.time.remove`; 204 / 400 / 401 / 403 / 404), but its
request schema `UpdateTimeDto` has **no properties**. Nothing documents how the
amount, direction or unit are sent. The only documented time bodies are the
Extensions API actions (`{"action":{"name":"add_time","params":<seconds>}}`,
scope `developer`), which is not the approved Route A. We do not guess a body
for an irreversible action, so `chaster_execution/transport.py` returns `None`
(`UPDATE_TIME_REQUEST_BODY_CONFIRMED = False`). With
`ENABLE_CHASTER_EXECUTION=true` the app logs why and keeps recording dry-run
decisions. To unblock: a documented schema, or a request captured from Chaster's
own client for a keyholder adding/removing time, then implement
`build_keyholder_transport()` and flip the constant.

## What is implemented

- **`LockActionTransport`**: exactly `add_time` and `remove_time`. No freeze
  method (disabled by decision), and never anything for combination / recovery /
  unlock.
- **`ChasterExecutor`** (`executor.py`): the only caller of the transport and the
  only writer of `lock_decisions.status`. Consumes persisted decisions only.
  - Status flow `dry_run -> pending -> executing -> executed | failed |
    needs_review`, plus `blocked` (pre-flight refused) and `expired` (older than
    15 minutes). Compare-and-set transitions, each appended to
    `lock_decision_executions` and emitted as `lock_decision.status_changed`.
  - **At-most-once**: the `executing` claim is committed before the call and is
    unique per decision. An interrupted run is reconciled to `needs_review`;
    ambiguous outcomes (timeout, 5xx, transport exception) are `needs_review`.
    **Nothing ambiguous is ever retried automatically**; a failed or reviewed
    decision needs a new decision.
  - Pre-flight, on fresh data: the keyholder connection's granted scopes contain
    `locks`; the observation is at most 15 minutes old (one read-only refresh if
    not); the lock is `locked`, a keyholder lock, and `isFrozen` is exactly
    `False`; a lock id exists; the hard limits are re-applied against executed
    history only (executed / executing / needs_review).
  - Only `add_time` / `remove_time` execute. Hours are sent as `hours * 3600`.
- **Switches**: `ENABLE_CHASTER_EXECUTION` (default false) and the runtime halt
  `execution_control` (migration 026), which is seeded **halted**, treats a missing
  or unreadable row as halted, and is changed only by
  `python -m scripts.execution_control status|halt|clear "reason"`. Both are
  checked per run and again immediately before every call.
- **Hydration** (`hydration.py`): `LockSnapshot` from the latest persisted
  observation. Migration 026 adds nullable `is_frozen`, `has_keyholder`,
  `end_date` to `chaster_provider_observations`; they are stored only when
  well-typed and old rows stay unknown. `canBeUnlocked` (deprecated) is never stored
  or read.
- **Periodic cycle** (`system/periodic.py`): every
  `CHASTER_EXECUTION_INTERVAL_SECONDS` (60), under the single-instance lease:
  drain outbox -> Rules Engine -> executor pass. Started by the bot only when the
  Rules Engine is enabled. `system/composition.py` builds everything.
- **Second connection (Route A)**: the keyholder's OAuth connection is an
  ordinary `chaster_connections` row under its own internal user id
  (`CHASTER_EXECUTION_KEYHOLDER_USER_ID`); the observed wearer is
  `CHASTER_EXECUTION_WEARER_USER_ID`. No schema change was needed. Creating the
  keyholder connection uses the existing `chaster connect` flow for that user and
  is not automated here.

## Isolation

Imports nothing from `conversation_engine`, `application`, `bot`, `discord` or any
`chaster.emergency_unlock*` module (AST-guarded); only `system/` wiring and
`scripts/` import it; `application/service.py` still never mentions the
emergency plane. No token, header or body is stored or logged. TLS is untouched.

## Tests

`tests/chaster_execution/` (71 tests, fake transport, no network): switches and
halt (including mid-run), every status transition, claim uniqueness, crash
recovery, no automatic retry, staleness, each pre-flight refusal, limits on
executed history only, observation extraction, composition flags, an end-to-end
periodic cycle (missed goal -> executed decision), and import guards.
