# chaster_execution — Phase 3 (execution plane)

Canonical: `docs/architecture/chaster_execution_phase3_proposal.md` (approved).
Carries out barrier decisions on a Chaster lock. **Zero-Time-Modification (owner decision, 2026-11-04): the system never changes a lock's duration,
up or down.** No time method exists on the transport or the executor, and no action type or limit for one exists in `rules_engine`. Lock
progression is governed purely by the frozen state and the Release Engine. `ENABLE_CHASTER_EXECUTION` stays false until live testing.

**Live testing (owner decision, 2026-11-04):** physical lock destruction and unlock (the Chaster `/unlock` endpoint) stay banned, and no method for them exists
here. Unfreezing through the Release Engine (`frozen=False`) is allowed once the execution halt is lifted.

## The Chaster transport (`ChasterHttpTransport`, Route A)

`POST https://api.chaster.app/locks/{lockId}/freeze` with `{"isFrozen": <bool>}`, authenticated with the KEYHOLDER connection's access token (the
existing token manager: transparent refresh, one refresh-and-retry on a 401), and `set_extensions` for the guarded CLI.

- Outcomes: 204 OK; 400 not eligible; 403 forbidden; 404 not found; 401 refresh once then unauthorised. Any other status (including any other 2xx
  and every 5xx), a read timeout or a dropped connection is **unknown** and ends as `needs_review`, never retried. Only a connect timeout, a
  missing/unrefreshable connection or rejected input is "not sent".
- Defence in depth: the lock id must match `[A-Za-z0-9_-]{1,64}` (it is placed in the URL).
- The token lives only in the Authorization header of the single request; it is never logged, stored or returned. TLS verification is untouched.
  Tests inject a fake `send` or patch `requests.Session.send`; nothing touches the network.

## What is implemented

- **`LockActionTransport`**: exactly `set_frozen`. Never anything for time, combination, recovery or unlock.
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
    the state the barrier action expects; a lock id exists; the release limits are re-applied against executed
    history only (executed / executing / needs_review).
  - Only freeze / unfreeze execute, and only for barrier rule ids.
- **Switches**: `ENABLE_CHASTER_EXECUTION` (default true since 2026-11-10) and the runtime halt
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

`tests/chaster_execution/` (122 tests, fake transport, no network): switches and
halt (including mid-run), every status transition, claim uniqueness, crash
recovery, no automatic retry, staleness, each pre-flight refusal, limits on
executed history only, observation extraction, composition flags, an end-to-end
periodic cycle (missed goal -> executed decision), and import guards.

## v0.39: barrier management (freeze / unfreeze)

The Phase 3 ban on freeze/unfreeze is lifted **only** for barrier management:
- A `freeze`/`unfreeze` decision executes only when the executor was built with `allow_barrier_actions` (set only with
  `ENABLE_RELEASE_ENGINE`) **and** its rule id is in `BARRIER_RULE_IDS` (`barrier.release`, `barrier.freeze`). A freeze/unfreeze from any
  other rule (for example a custom Rules Engine ruleset) never executes.
- `ChasterHttpTransport.set_frozen()` uses the **documented** endpoint `POST /locks/{lockId}/freeze` with
  `{"isFrozen": <bool>}` (needs `lock.freeze` / `lock.unfreeze`); same outcome mapping and token handling as the time calls.
- **Separate limit pool** (`rules_engine/limits.py`): at most 2 RELEASES (unfreezes) per 24 h and a 4 h cooldown between them;
  a `FREEZE` (re-raising the barrier, rule `barrier.freeze`) is never blocked by the pool and does not use up the release
  budget. The time pool is unchanged and independent in both directions.
- Pre-flight for a barrier action: an `UNFREEZE` needs the barrier up (`isFrozen` exactly true) and, when a window check is
  supplied, a discretionary window at the moment of the call; a `FREEZE` needs it down. Locked + keyholder lock + fresh
  data + scope + halt clear as before.
- Verification of the freeze behaviour on a designated test lock (exhausted timer + `isFrozen: true` holds; unfreeze
  releases) must be done before `ENABLE_CHASTER_EXECUTION=true`.

## v0.39 Phase D: the guarded manual freeze (`manual_freeze.py`, migration 034)

`python -m cli freeze-lock <lock_id> --state true|false --owner-id <your internal user id> --yes` freezes or unfreezes ONE managed lock by hand, for live
verification. It is an operator tool: nothing in `bot/`, `application/`, `conversation_engine/` or `system/` imports it (guarded in tests), so no model, Discord or
Chaster message can reach it. Checks, in order, each refusal audited and nothing sent: owner (`OWNER_USER_ID`, exact constant-time match; unset refuses everyone),
`--yes`, plain lock id, runtime halt not set (an unreadable control counts as halted), a live keyholder search lists the lock as managed and locked with a known frozen
state (self-locks are refused), and the lock is not already in the requested state. Every invocation writes one `manual_lock_actions` row; a real attempt is written
first as `attempting` and then updated, so a crash cannot hide a physical action. Exit codes: 0 applied or nothing to do, 3 refused, 4 failed or ambiguous (`unknown`:
check the lock on Chaster; never retried automatically). It does not use the barrier limit pool and creates no Rules Engine decision.

## v0.39 Phase I2: the guarded extension-review update (`extension_review.py`, migration 042)

`python -m cli extension-review <lock_id> --owner-id <id>` is a DRY RUN: it reads the lock through the connected account, builds the COMPLETE body for
`POST /locks/{lockId}/extensions` (`chaster/extension_plan.py`: every extension sent back as read; only `peerVerification.enabled` = true, `visibility` =
`all_members` and `punishments` = [] change on `tasks` and `verification-picture`), prints each field change, every extension in the payload, a plan id and every
reason a real write would be refused. It sends and writes nothing and works while the runtime is halted. `--apply --plan <id> --yes` is the live write
(`ChasterHttpTransport.set_extensions`), with the same pattern as the manual freeze: owner, `--yes`, plain lock id and plan id, runtime halt not set, the lock is
readable, `locked`, and the account's role holds `lock.extensions.edit`, the body could be rebuilt faithfully, there is something to change, and the plan id of a
fresh read equals the dry run's (a lock that changed in between is refused). One `manual_extension_actions` row per `--apply` invocation (refusals included; the
attempt first as `attempting`); sent once, never retried; a fresh read afterwards decides `applied` or `applied_unverified`. Exit codes: 0 planned, nothing to do
or applied, 3 refused, 4 failed, ambiguous or unverified. Only the CLI imports it (guarded in tests).

## v0.39 Phase I3: the wearer's consent to community review (`review_consent.py`)

Community review (`all_members`) lets any logged-in Chaster member see a lock's verification photos and vote on them, so the forward write now
needs the wearer's consent (philosophy 2.5, 2.6). `review_consent.py` holds one fixed English notice (who sees the photos, that Chaster adds no
time on a rejection, that a rejection here is at most an UNCONFIRMED incident for the owner, that consent covers one lock and is revocable) and
`ReviewConsentLedger`: append-only rows in `consent_log` (`target_type` `integration`, `target_id` `community_photo_review:<lock id>`,
`target_version` = which notice text was shown, `explanation_shown` = the exact notice). The latest row decides: `approved`, `declined`, `revoked` or
`none`. Simplified (owner decision, 2026-11-04): the notice is informational, proceeding implies agreement with a link to the repository, and a
changed wording needs no re-acceptance.

- `extension-review --apply` refuses with `no_consent` on an explicit decline or revocation, and on a lock worn by ANOTHER account without that
  wearer's approval or the owner's attestation (that wearer may never have seen a notice). A lock the owner wears needs nothing else.
- `extension-review --revert` (dry run) and `--revert --apply --yes` set `visibility` back to `keyholder` on each review extension that has
  `all_members` (`plan_review_revert`; peer verification and punishments untouched). It needs no consent and no plan id (a given one must match)
  and is not stopped by the runtime halt (owner decision: it only reduces exposure); every other guard applies.
- `python -m cli review-consent <lock_id> --owner-id <id>` prints the notice and the state; `--accept|--decline|--revoke --yes` records the OWNER's
  decision, only for a lock the connected account wears. A lock worn by another account is refused (only that wearer can decide) unless the
  owner adds `--wearer-is-my-account`, attesting that the wearer account is the owner's own; the attestation is stored with the record. A
  decline or revocation of an exposed lock immediately runs the revert write, also while halted.
- `extension-config` shows the consent state per lock and warns when community review is on although the wearer refused it.

Only the CLI imports the service; the bot never records consent (guarded in tests). A Discord DM path for tenant wearers is not built yet.

Revocation (2026-11-03, `docs/architecture/phase_i_proposal.md` section 8): `--revert` and a revoked consent make Chaster stop asking for
photos: `verification-picture` is removed, `temporary-opening`'s picture requirements and the tasks' `verificationRequired` /
`codeRequired` and peer verification are switched off; every other extension and field is sent back as read. A decline runs it only when
community review is actually on.

Phase J2 (`penalty_barrier.py`, migration 044, `docs/architecture/phase_j_proposal.md` section 4): `PenaltyBarrierExecutor` carries out the barrier
intents of a Penalty Window on the one configured lock, only if a fresh read shows role `keyholder`. Freeze / unfreeze only, never time. Its queue is
separate from `lock_decisions`, so an intent survives a halt of any length (logged once, nothing read or sent) while the 15-minute decision expiry
stays as it is. At most once: the claim is committed before the call and an ambiguous outcome is `needs_review`. It acts on window events only, so
it never re-freezes a lock the 72-hour backstop released. On by default (`ENABLE_PENALTY_BARRIER`), but it acts only with `PENALTY_FREEZE_LOCK_ID` set.

## Extension guard

`extension_guard.py` keeps the settings in `chaster/extension_plan.py::REQUIRED_SETTINGS` true on Chaster by itself: it reads, corrects one field, and only a fresh read counts as done. It never acts past the runtime halt. See `docs/architecture/chaster_capability_map.md`, section 6.
