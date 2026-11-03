# Phase I: Autonomous Extension Management

Status: I1 (role-agnostic discovery and the read-only configuration fetcher) built. Nothing writes to Chaster yet.

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
| I2 | the guarded write: read-modify-write of the COMPLETE extension list changing only `peerVerification` of the two review extensions; dry run showing the exact diff first; refuses when the lock is not `locked`, the account's role lacks `lock.extensions.edit`, the runtime is halted or the lock changed since the read; owner-only, `--yes`, audited; re-read and verify afterwards | live |
| I3 | wearer transparency and consent: the wearer is told that community members see the photos (2.5, 2.6) before a lock is switched to `all_members` | engine only |
