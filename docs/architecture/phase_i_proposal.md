# Phase I: Autonomous Extension Management

Status: I1 (role-agnostic discovery, read-only configuration fetcher) and I2 (the guarded configuration overwrite, section 5) built. I2 was applied live
on both locks on 2026-11-03 (section 6, live result). I3 (wearer consent, section 6) and I4 (community review results -> unconfirmed Incidents, section 7) built;
the forward write now also needs the wearer's recorded consent.

## 1. Goal (owner, 2026-11-03)

Photo validation is left to Chaster Peer Verification (Phase H was discontinued, `phase_h_proposal.md` section 12). The bot is to bring the review
settings of the `tasks` and `verification-picture` extensions to:

1. **Community review**: `peerVerification.enabled = true` and `peerVerification.visibility = all_members`.
2. **No Chaster-side punishments**: `peerVerification.punishments = []`, so a rejected check is handled only inside this engine (at most an
   unconfirmed Incident for the owner, philosophy 2.10 and 2.12), never also by Chaster ("double jeopardy").

The system must work whichever role the connected account has on a lock (wearer of a self-managed lock, or keyholder), as long as the lock's
permission contract allows it.

## 2. What the Chaster API offers (OpenAPI 0.35.0 and live reads, 2026-11-03)

- **There is no visibility value named `community`.** `PeerVerificationVisibilityEnum` is `all_members` | `keyholder`; community review is
  `all_members`. The extension `config` itself is an untyped `object` in the OpenAPI document; the `peerVerification` object seen live is
  {`enabled`, `punishments`, `visibility`} (on the wearer lock's verification picture `visibility` was absent while `enabled` was false).
- Punishments follow the `*LockActionModel` schemas, {`name`, `params`}: `add_time` / `remove_time` (seconds), `freeze`, `unfreeze`,
  `toggle_freeze`, `pillory` (an object), `set_display_remaining_time`.
- `GET /locks/{lockId}` returns, for the wearer and the keyholder alike: `role`, `permissions` (`preset`, `grants[]` with `resource` and
  per-subject verbs) and `extensions[]` (`_id`, `slug`, `mode`, `regularity`, `config`, `userData`, ...).
- **Write path (not used yet):** `POST /locks/{lockId}/extensions` (scope `locks`, body `{extensions: [{slug, config, mode, regularity,
  partnerConfigurationToken?}]}`), documented as "Requires the `lock.extensions.edit` permission on the lock's contract". The body is the
  COMPLETE list: a write must send every extension back unchanged except the edited fields.

## 3. I1 as built

| Piece | What it does |
|---|---|
| `LockDirectory.list_account_locks(user_id)` | One account, both roles: `GET /locks?status=active` (worn) and `POST /keyholder/locks/search` (held), each side with its own problem codes. |
| `cli.main.discovery_user_ids` | `list-locks` now asks the connected account (`CHASTER_EXECUTION_KEYHOLDER_USER_ID`) for worn locks too when no separate `CHASTER_EXECUTION_WEARER_USER_ID` is set. |
| `ChasterLockClient.get_lock(lock_id)` | `GET /locks/{lockId}`; the id is validated (`[A-Za-z0-9_-]{1,64}`) before any request; errors carry the HTTP status only. |
| `chaster/extension_config.py` | `parse_lock_configuration()` (role, who may edit extensions, every extension with a deep copy of its config, the parsed peer-verification settings), `review_gaps()` (difference to the target), `LockConfigurationReader` (one read per lock through the token manager, problem codes per lock, one INFO log line per review extension with enum values only). |
| `python -m cli extension-config` | READ-ONLY report of every lock of the account: role, edit permission, review settings and what would change. Locks that are not `locked` are out of scope. |

Never copied or logged: the lock combination, `offerToken`, `sharedLinkToken`, any extension's `userData` (it holds the current verification
code) and the task texts inside `config` (kept only in memory, excluded from `repr`).

**Live result (2026-11-03).** The account wears `6ae98ca…` (preset `full_trust`, both subjects may edit extensions) and an unlocked lock
`6aba61d1…` (out of scope), and holds the keys of `6aca95cd…` (preset `trusted_keyholder`, only the keyholder may edit). Gaps found:

| Lock | tasks | verification-picture |
|---|---|---|
| `6ae98ca…` (wearer) | visibility `keyholder` | peer verification off, visibility unset |
| `6aca95cd…` (keyholder) | visibility `keyholder`, punishments `add_time(3600)` | visibility `keyholder` |

## 4. Next steps (each needs the owner's approval)

| Step | Content | Live effect |
|---|---|---|
| I1 | discovery in both roles, read-only configuration fetcher, `extension-config` report | reads only |
| I2 | BUILT (section 5): the guarded write: read-modify-write of the COMPLETE extension list changing only `peerVerification` of the two review extensions; dry run showing the exact diff first; refuses when the lock is not `locked`, the account's role lacks `lock.extensions.edit`, the runtime is halted or the lock changed since the read; owner-only, `--yes`, audited; re-read and verify afterwards | live |
| I3 | BUILT (section 6): wearer notice and consent record; `--apply` requires approved consent; `--revert`; the owner's own decisions by CLI | engine; a revert is live |
| I4 | BUILT (section 7): community review results polled; a rejection becomes exactly one UNCONFIRMED Incident for the keyholder; transitions ledger; owner DM and CLI decision | reads only; local records |

## 5. I2 as built (2026-11-03)

- `chaster/extension_plan.py`: `plan_review_update(config)` builds the COMPLETE body (`{"extensions": [{slug, config, mode, regularity}, ...]}`, same order,
  configs deep-copied) and sets exactly `peerVerification.enabled = true`, `visibility = all_members`, `punishments = []` on `tasks` and
  `verification-picture`; any other key, also inside `peerVerification`, is sent back as read. Enabling is included because community review cannot
  happen while peer verification is off (the wearer lock's verification picture). It refuses when the body cannot be rebuilt faithfully (missing review
  extension, partner extension, invalid `mode` or `regularity`). `plan_id` = the first 16 hex characters of the SHA-256 of the canonical body.
- `ChasterHttpTransport.set_extensions(lock_id, body)`: `POST /locks/{lockId}/extensions`; 200/201/204 OK, 400 not eligible, 403 forbidden, 404 not
  found, 401 refresh once, anything else or a read timeout ambiguous; malformed input is never sent.
- `chaster_execution/extension_review.py`: `preview()` (dry run, works while halted, nothing audited) and `apply()` (owner, `--yes`, input, runtime halt,
  account, `locked`, `lock.extensions.edit` for the account's role, faithful plan, something to change, plan id equal to the dry run's; audited first as
  `attempting` in `manual_extension_actions`, migration 042; sent once; a fresh read decides `applied` or `applied_unverified`).
- `python -m cli extension-review <lock_id> --owner-id <id>` (dry run) and `... --apply --plan <id> --yes` (live).

**Dry run on the live locks (2026-11-03, runtime halted, nothing sent):**

| Lock | Role | Changes | Sent back unchanged |
|---|---|---|---|
| `6aca95cd…` | keyholder | tasks visibility keyholder → all_members; tasks punishments [add_time(3600)] → []; verification-picture visibility keyholder → all_members | pillory, link, temporary-opening |
| `6ae98ca…` | wearer | tasks visibility keyholder → all_members; verification-picture enabled off → on; verification-picture visibility unset → all_members | pillory, temporary-opening, link |

**Open until the first live write:** whether Chaster accepts the body as read (it may validate config fields more strictly than it returns them; a 400
changes nothing), and whether an edit by the WEARER of a lock with a keyholder is applied directly or handled differently by Chaster. Recommended
order: the keyholder lock first, then check it on Chaster, then the wearer lock.

**Halt gate verified live (2026-11-03):** `--apply --plan b92932793208fe65 --yes` on `6aca95cd…` while halted was refused with `outcome=halted` before any
read or send (audit `64b90b43…`). The owner authorized the live apply of both plans; clearing the halt from an assistant session was blocked by the
assistant's safety classifier, so the owner runs the clear / apply / halt sequence.

**State check (2026-11-03, read-only `extension-config` after the owner reported a manual run):** both locks still show the pre-I2 settings and the
halt reason is still `initial state: halted until the owner clears it`, so the manual clear / apply / halt did not reach this repository's database
(most likely run from another directory or with the Microsoft Store `python` alias). Nothing was written to Chaster.

## 6. I3 as built: wearer transparency and consent (2026-11-03)

**Why.** `all_members` means any logged-in Chaster member can see the verification photos (and task photos sent for verification) of the lock and vote on
them. That exposes the wearer to an audience they did not have before, so philosophy 2.5 (specific, revocable consent) and 2.6 (the wearer knows what
happens and why) apply. Removing the Chaster punishments is in the wearer's favour but is explained too: a rejected review no longer adds time on
Chaster; it becomes at most an UNCONFIRMED Incident for the owner (2.10, 2.12), never an automatic penalty.

The owner approved the proposed answers to decisions 2-4 ("approved 4-point design"). Decision 1: the wearer of `6aca95cd…` is the owner's own
second account; the owner records consent for it with an explicit attestation (`--wearer-is-my-account`). Interpretation 1 below was decided
the other way: a revert is NOT stopped by the runtime halt.

| Piece | As built |
|---|---|
| Notice (versioned) | `chaster_execution/review_consent.py` `NOTICE_TEXT`: who sees the photos, what they do, that Chaster adds no time on a rejection, that a rejection here is at most an unconfirmed incident the owner reviews, that the setting is visible on Chaster, one lock only, revocable, decline means no community review. `NOTICE_VERSION` = the first 12 hex characters of its SHA-256; a changed text makes earlier approvals `stale`. |
| Consent record | `ReviewConsentLedger`, append-only rows in `consent_log`: `target_type = integration`, `target_id = community_photo_review:<lock_id>`, `target_version = <notice version>`, `explanation_shown = <the exact notice>`, `approved` / `rejected` / `revoked`. The latest row decides (`approved`, `stale`, `declined`, `revoked`, `none`). |
| Gate on the forward write | `extension-review --apply` refuses with `no_consent` (audited, nothing sent) unless the state is `approved`; the dry run lists it as a blocker. This covers switching review ON as well (decision 4), because every forward plan ends at `all_members`. |
| Revert | `extension-review --revert` (dry run) / `--revert --apply --yes`: `plan_review_revert` sets `visibility = keyholder` on each review extension that has `all_members`; peer verification and punishments stay as they are, so it can only reduce exposure. No consent and no plan id needed (a given one must match); owner, `--yes`, the runtime halt, `locked`, the edit permission and the audit apply as before; verified by a fresh read. |
| Owner's own decisions | `python -m cli review-consent <lock> --owner-id <id>` shows the notice and state; `--accept`, `--decline` or `--revoke` with `--yes` records the decision when the connected account wears the lock. A lock worn by another account is refused (`wearer_is_another_account`) unless the owner adds `--wearer-is-my-account` (the wearer account is the owner's own); that attestation is stored in the record's comment. |
| Automatic revert (decisions 2 and 3) | A decline or revocation of a lock that currently has community review immediately runs the revert write, also while the runtime is halted (owner decision 2026-11-03: a revert only reduces exposure). |
| Status | `extension-config` prints the consent state and notice version per lock and `WARNING: community review is ON without the wearer's recorded consent` when they disagree. |

**Interpretations to confirm.**
1. **DECIDED: a revert passes the runtime halt.** The halt still stops every forward write and every other live action; only the switch back to
   `keyholder` visibility, which can only reduce who sees the photos, is exempt.
2. **Consent for a genuinely different wearer has no path yet** (a tenant DM command `community review accept|decline|revoke`, or the Chaster
   lock chat). The owner's own second account is covered by the attestation flag.
3. **No answer timeout yet.** Decision 2's "after how long" applies to remote wearers only; it comes with that channel. Without consent nothing is switched
   on, so silence already means "no".

**Live result (2026-11-03).** The owner recorded consent for both locks, ran the clear / apply / halt sequence and Chaster answered HTTP 201
for both writes. A read-only `extension-config` afterwards shows `6ae98ca…` and `6aca95cd…` with peer verification on, visibility
`all_members` and no punishments on `tasks` and `verification-picture` ("matches the target"), all other extensions still present, consent
`approved` (notice `b5e47d78b70f`) and the runtime halted again (`Post-write safety halt`). The open points of section 5 are answered: Chaster
accepts the body as read, and a wearer-side edit on a `full_trust` lock is applied directly.

**Live order (as run).** Consent for `6ae98ca…` (`--accept --yes`) and `6aca95cd…` (`--accept --yes --wearer-is-my-account`), a fresh dry run
(consent does not change the plan id), then the halt clear / apply / halt sequence.

## 7. I4 as built: community review results -> UNCONFIRMED Incidents (2026-11-03)

**Goal (owner).** Read the community votes on task photos and verification pictures; a rejection creates strictly an UNCONFIRMED Incident
for the keyholder's decision; no automatic token deduction or time added (no double jeopardy); every polled status transition recorded so
one rejected review can never create a second Incident.

**API used (OpenAPI 0.35.0, read-only).** `GET /extensions/tasks/{lockId}/history` (TaskHistoryEntry.peerVerification) and
`GET /locks/{lockId}/verification-pictures` (VerificationPictureHistoryEntry.peerVerification, or the deprecated `votes` with
`peerVerificationId`). Each review has an id and `status` ongoing / verified / rejected (PeerVerificationStatusEnum) plus vote counts.

| Piece | As built |
|---|---|
| Reads | `ChasterLockClient.get_task_history()` / `get_verification_pictures()`; `chaster/peer_reviews.py` keeps review id, status, vote counts, submission time; task texts, verification codes, image keys, voters and reason texts are never copied. |
| Ledger (migration 043) | `peer_reviews` (latest state per lock + review id, the claimed Incident id, registration, notice and decision stamps), `peer_review_transitions` (append-only, one row per status change), `peer_review_locks` (baseline marker). |
| Baseline | The first COMPLETE read of a lock records the reviews that already ended without creating Incidents; `ongoing` ones are followed. A partial first read postpones the baseline. |
| De-duplication | A review's first `rejected` status and its Incident-id claim are written in one transaction (`... SET incident_id = ? WHERE incident_id IS NULL`); the Incident is registered with that pre-chosen id (`TrustManager.register_incident_report(incident_id=...)`), so a retry hits the primary key instead of duplicating. Tested with repeated polls, two watchers on one database, a failed registration and a crash between registration and marking. |
| Incident | Rule group `peer_review_rejection`, Trust domain `PEER_REVIEW_TRUST_DOMAIN` (default `routine`), evidence impact LOW / confidence LOW / intent UNCLEAR, description with lock, source, review id and vote counts. Always UNCONFIRMED; no assessment. |
| No double jeopardy | `incident.reported` has no consumer; the Penalty Engine reacts only to `incident.confirmation_changed`; the daily evaluation and the plugin SDK count CONFIRMED Incidents only; the poll class has no path to any confirmation and the package imports nothing from execution, penalty, release or rules. Chaster's own punishments were removed in I2. |
| Owner notice | One Discord DM per Incident to the owner (id from the owner's identity row), retried each poll until Discord accepts it. |
| Decision | Discord buttons on the notice, **Confirm Penalty** / **Dismiss Incident** (persistent, owner's Discord account only, one click, buttons removed afterwards), or the CLI fallback `python -m cli peer-reviews list|confirm|dismiss` (owner, `--yes`): `confirm` = CONFIRMED with source `keyholder_review` (from then on it counts like any confirmed Incident), `dismiss` = stays UNCONFIRMED for good. |
| Failure alert | `PollHealth`: 3 consecutive failed polls (network, Chaster 4xx/5xx, rate limit, expired connection) -> one alert DM to the keyholder; one recovery note when polling works again. |
| Runtime | Its own bot task (`peer_review_loop`, first poll 30 s after start, then every `PEER_REVIEW_POLL_SECONDS`, default 600, minimum 120), independent of the Rules Engine cycle (off in this deployment). Behind `ENABLE_PEER_REVIEW_WATCH` (default false). Not stopped by the execution runtime halt: it only reads Chaster and writes local unconfirmed records. |

**Interpretations to confirm.**
1. **All Incidents go to the owner's database and Trust domain**, also for `6aca95cd…`, whose wearer is the owner's own second account. A genuinely
   different wearer would need the tenant path (their own database), which is not wired for this watch.
2. **A `rejected` review that later turns `verified`** (should Chaster ever reverse it) is recorded as a transition; the Incident stays for the
   keyholder to dismiss.
3. **Locks that are not `locked`** are not polled.

**Live (2026-11-03).** The owner's database had no Trust domain; the owner approved creating `routine` through the new guarded
`python -m cli trust-domain create` (consent record first, TI1; score 0.6, confidence 0.15). With `ENABLE_PEER_REVIEW_WATCH=true` the bot
applied migration 043, logged "Community review watch enabled: every 600 s", found the owner's Discord id for notices, and the first poll
recorded the baseline: `6aca95cd…` one task review already `verified` (no Incident), `6ae98ca…` one task review `ongoing` (followed).

## 8. Revocation stops photo requests (2026-11-03, owner directive)

A revocation (and `extension-review --revert`) no longer only hides the photos from the community; it makes Chaster stop asking for them.
Deep audit of the live configs first (read-only, task texts redacted): `tasks` items carry `verificationRequired` and `codeRequired`
(TaskItem, OpenAPI 0.35.0); `temporary-opening` has `requireVerificationPictureBefore` / `After`; `verification-picture` has its own
top-level `visibility` (picture history: `all` on `6ae98ca…`, `keyholder` on `6aca95cd…`) and NO on/off switch: its requests come from the
extension being on the lock. `pillory` and `link` have no photo settings.

`plan_review_revert` now:
- REMOVES `verification-picture` from the list (the only way to stop its requests);
- switches every other extension's own picture requirement off explicitly instead of trusting Chaster to default it after the removal
  (`temporary-opening`: `requireVerificationPictureBefore` / `After` -> false; owner remark: "any other extension using it should default to
  not requiring picture verification");
- on `tasks`: `peerVerification.enabled` -> false, `peerVerification.visibility` -> `keyholder` (punishments as read), every item's
  `verificationRequired` / `codeRequired` -> false; task text, points and duration as read;
- sends every other extension and field exactly as read, in read order, adds nothing; a lock without `verification-picture` is accepted.
The re-read must show nothing left to switch off and exactly the sent set of extensions (`applied`), otherwise `applied_unverified`.

Revoke vs decline: a REVOCATION switches everything off whenever anything is still on. A DECLINE (never agreed) runs the same switch-off only
if community review is actually on, so declining never strips the keyholder's own photo checks from a lock without community review.

Consequences: the I3 notice text describes the new effect, so its version changed (`97d89d263cde`) and earlier approvals count as `stale`
until accepted again (one button in the Discord consent overview). After a revocation the FORWARD plan refuses with
`missing_extension:verification-picture` until the keyholder adds the extension again on Chaster.

Live dry run (nothing sent): both locks would get `tasks peerVerification.enabled on -> off`, `visibility all_members -> keyholder` and
`verification-picture removed`; their task and temporary-opening photo flags are already off; `pillory`, `link`, `temporary-opening` are
sent back unchanged.
