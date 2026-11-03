# Phase I: Autonomous Extension Management

Status: I1 (role-agnostic discovery, read-only configuration fetcher) and I2 (the guarded configuration overwrite, section 5) built. I2 has only been
run as a dry run and against the halt gate; the live write waits for the owner to clear the runtime halt. I3 is proposed (section 6).

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
| I3 | PROPOSED (section 6): wearer transparency and consent: the wearer is told that community members see the photos (2.5, 2.6); `--apply` requires a recorded consent | engine only |

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
assistant's safety classifier, so the owner runs the clear / apply / halt sequence himself.

## 6. I3 proposal: wearer transparency and consent (PROPOSAL, no code)

**Why.** `all_members` means any logged-in Chaster member can see the verification photos (and task photos sent for verification) of the lock and vote on
them. That exposes the wearer to an audience they did not have before; it is a change to how they are watched, so philosophy 2.5 (specific, revocable consent)
and 2.6 (the wearer knows what happens and why) apply. Removing the Chaster punishments is in the wearer's favour but must be explained too: a rejected
review no longer adds time on Chaster; it becomes at most an UNCONFIRMED Incident for the owner (2.10, 2.12), never an automatic penalty.

**Proposed pieces.**

| Piece | Content |
|---|---|
| Notice text (versioned) | One fixed English text: who can see the photos (any logged-in Chaster member), what they do (vote verified / rejected), what a rejection causes here (an unconfirmed Incident the owner reviews, no automatic penalty, no Chaster time), how to refuse or revoke, and that the setting is visible on the lock's Chaster page. Its SHA-256 prefix is the notice version. |
| Consent record | `consent_log` in the wearer's own database: `target_type = integration`, `target_id = community_photo_review:<lock_id>`, `target_version = <notice version>`, `explanation_shown = <the exact text>`, `approved` / `rejected` / `revoked`. Same pattern as the tenancy acceptance (`multi_wearer_keyholder`). |
| Delivery | Owner's own lock (tenant zero, the wearer is the owner): the CLI shows the notice and records consent with `--owner-id` + `--yes`. A managed wearer (tenant): a Discord DM with exact replies `community review accept` / `community review decline`, handled before any conversation and never by a model (like `accept` / `halt`). |
| Gate on I2 | `extension-review --apply` gains one more refusal, `no_consent`: the latest consent record for that lock and the CURRENT notice version must be `approved`. A changed notice text needs fresh consent. The dry run lists the missing consent as a blocker. |
| Revocation | `community review revoke` (wearer DM) or the owner CLI records `revoked` at once and plans the reverse change (`visibility = keyholder`; punishments stay empty). Applying the reverse write goes through the same guarded path. |
| Status | `extension-config` shows per lock: consent state, notice version, and whether the live setting and the consent agree (for example `all_members` without consent is flagged). |

**Decisions needed.**
1. **Order already taken.** The I2 writes come before I3. For `6ae98ca…` the wearer is the owner, so the owner's authorization counts as consent and can be recorded
   retroactively. For `6aca95cd…` the wearer is a DIFFERENT Chaster account: is that person a tenant (Discord DM possible), the owner's own second account, or
   neither? If neither, the notice can only go through the Chaster lock chat (Phase C channel) or by the owner personally; which one?
2. If that wearer declines or does not answer, should the lock be switched back to `keyholder` visibility (proposed: yes, through the guarded path), and after how long without an answer?
3. Should a revocation by the wearer reverse the setting automatically (it still passes the runtime halt and the audit), or only queue the reverse plan for the owner? Proposed: automatic
   is safer for the wearer (2.5 revocable "at any time"), and it can only reduce exposure.
4. Is the notice also shown when an extension is only switched ON (the `6ae98ca…` verification-picture case), or only when visibility changes? Proposed: both, since enabling review is
   what creates the audience.
