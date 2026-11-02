# Multi-wearer operator runbook

How to run the bot as keyholder for several wearers (v0.39 Phase E). Design and decisions: `docs/architecture/dual_role_and_messaging_proposal.md`, sections 10-16.
Code: `tenancy/` (registry, platform database, halt, archive), `system/tenant_*.py`, `system/managed_lock_source.py`, `system/lock_pairing.py`, `chaster/api_budget.py`.

## 1. What runs where

| Thing | Where it lives |
|---|---|
| The owner's own system ("tenant zero", `tenant_0`) | the existing main database (`DB_PATH`, default `data/app.sqlite`), unchanged |
| The tenant registry, the global halt, the lifecycle audit | `platform.sqlite` next to the main database (`PLATFORM_DB_PATH`) |
| Each wearer's data (goals, trust, check-ins, decisions, chat state, consent) | `data/tenants/tenant_<id>.sqlite`, one file per wearer |
| Removed wearers' databases | `data/tenants/archive/tenant_<id>.<UTC time>.sqlite`, read-only |
| The bot's Chaster keyholder connection | tenant zero's database, shared by every tenant through one token manager |

Every tenant has its own database, conversation memory, check-ins, release engine, execution halt and loop. Nothing mutable is shared between tenants.

## 2. One-time setup

1. In `.env`:
   - `OWNER_USER_ID=<your internal user id>` (the CLI refuses every owner command while it is empty).
   - `CHASTER_EXECUTION_KEYHOLDER_USER_ID=<internal user id of the bot's keyholder account>`.
   - `CHASTER_OAUTH_SCOPES=locks keyholder messaging` (`messaging` only if Chaster chat is wanted).
   - `ENABLE_MULTI_WEARER=true`, optionally `TENANT_CAP=5` (tenant zero counts), `CHASTER_API_BUDGET_PER_MINUTE=30`.
   - The usual flags (`ENABLE_RULES_ENGINE`, `ENABLE_SUPERVISION`, `ENABLE_CHECK_INS`, `ENABLE_RELEASE_ENGINE`, `ENABLE_CHASTER_EXECUTION`, `ENABLE_CHASTER_MESSAGING`) apply to every tenant.
2. Re-connect the bot's keyholder account so it holds the new scopes: send `chaster connect` from that account's Discord identity and complete the OAuth page.
3. Start the bot. The log says `Multi-wearer platform ready (tenant cap N); tenant_0 is the existing system.`
4. Check: `python -m cli tenants list` shows `tenant_0` and `Platform halt: clear`. `python -m cli list-locks` shows the bot's managed locks.

Before allowing any real lock action, do the live test-lock verification (exhausted timer + `isFrozen: true` holds; unfreeze releases) with
`python -m cli freeze-lock <lock id> --state true|false --owner-id <you> --yes` on a test lock.

## 3. Adding a wearer

| Step | Who | How |
|---|---|---|
| Invite | owner | DM the bot `invite <discord id> <name>`, or `python -m cli tenants invite --name <name> --discord-id <id> --owner-id <you>` |
| Invitation delivered | bot | a DM within about 30 s explaining what accepting means, `halt` and `leave` |
| Accept | wearer | DM `accept` (or `decline`). Acceptance writes a consent record into the wearer's own database |
| Onboarding | wearer | any message starts it (language, voice, personality) |
| Pair the lock | owner | `python -m cli tenants pair <tenant id> <lock id> --owner-id <you>`; the lock must be locked and held by the bot |
| Confirm the lock | wearer | DM `lock` (shows title and Chaster username), then `confirm lock` (or `reject lock`). Automatic if the wearer connected their own Chaster account and it is this lock's wearer |
| Allow lock actions | owner | `python -m cli tenants execution <tenant id> clear --reason "<why>" --owner-id <you>` (a new tenant starts halted) |
| Chaster chat (optional) | wearer | message the bot's Chaster account once, then DM `channel chaster`; `channel discord` switches back |

Nothing acts on a wearer's lock until all of these hold: the tenant is active, the pairing is confirmed, a fresh keyholder search shows the paired lock
locked with the bot as keyholder, and neither the tenant halt nor the platform halt is set.

## 4. Stopping things

| Want | Command | Who can undo |
|---|---|---|
| Stop every lock action for every tenant | `python -m cli tenants halt --reason "<why>" --owner-id <you>` | owner: `tenants unhalt` |
| Stop one tenant's lock actions | `python -m cli tenants execution <id> halt --reason "<why>" --owner-id <you>` | owner: `execution <id> clear` |
| Wearer stops their own lock actions | wearer DMs `halt` | only the wearer: `resume` (the owner's CLI refuses to clear it) |
| Pause a tenant (no loop, no chat, no actions; safety commands still work) | `python -m cli tenants suspend <id> --owner-id <you>` | owner: `tenants resume <id>` |
| Owner's own system | owner DMs `halt` / `resume`, or `python -m scripts.execution_control halt|clear` | owner |

A wearer can always use `halt` and `leave`, also while suspended. Chaster's own emergency unlock stays with the wearer on Chaster.

## 5. Ending a membership

- The wearer leaves: DM `leave`, then `leave confirm`. The tenant is halted, removed and its loop stopped at once.
- The owner removes: `python -m cli tenants remove <id> --yes --owner-id <you>`. Immediate as well.
- An invitee declines: `decline`.

After 5 minutes the bot moves the wearer's database to `data/tenants/archive/` (read-only) and records it; `python -m cli tenants archive --owner-id <you>`
does the same immediately (for example while the bot is stopped). An archived tenant is never processed or rebuilt; a re-invitation creates a brand-new tenant
and database. `python -m cli tenants list --all` shows removed and archived tenants.

Deleting an archived database is a deliberate manual step: stop the bot, check `tenants list --all` for the tenant id and archive file, then delete
`data/tenants/archive/tenant_<id>.<time>.sqlite` (and any `-wal`/`-shm`/`-journal` file next to it). Nothing deletes archives automatically.

## 6. The Chaster request budget

All tenants use the one keyholder account. The bot counts every request made with it in a sliding one-minute window (`CHASTER_API_BUDGET_PER_MINUTE`, default 30):
- Background requests (lock searches, observation refreshes, chat polling and sends) are refused when the window is full; they are retried on later cycles (observations simply go stale; a chat send reports not delivered).
- Lock actions that were already decided are never refused, only counted.
- A tenant cycle runs every `CHASTER_EXECUTION_INTERVAL_SECONDS` (60 s), staggered across tenants, and normally uses one or two background requests.
  With the default cap of 5 tenants that is well under 30 per minute. Raise the budget only if Chaster's limits allow it; lower it if you see HTTP 429 in the log.
- The CLI runs in its own process and is not counted.

## 7. Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| `tenants pair` says "keyholder search could not be done" | the bot connection lacks the `keyholder` scope, or Chaster is down | re-connect with `CHASTER_OAUTH_SCOPES` including `keyholder`; retry |
| `tenants pair` says "not a locked lock held by the bot" | the lock is unlocked, archived, or held by another keyholder | check on Chaster; the wearer may need to start a new lock with the bot as keyholder |
| Wearer's `confirm lock` says the lock is not held | the lock changed since pairing | re-pair after the lock is locked again |
| Nothing happens for a wearer | tenant halted (new tenants start halted), wearer halt, platform halt, unconfirmed pairing, or no fresh observation | `tenants list`; `execution <id> clear` if appropriate; ask the wearer whether they sent `halt` |
| Log: "Tenant lock check: not_found" | the paired lock is no longer among the bot's locked locks | nothing acts (by design); re-pair when there is a new lock |
| Log: "Tenant lock check: mismatch" | the lock's wearer or keyholder is no longer the paired one | investigate; unpair (`tenants unpair <id>`) |
| Log: "shared Chaster request budget is used up" | too much background traffic for the budget | it recovers on its own; check the number of active tenants and the budget value |
| `channel chaster` says "could not find a Chaster chat" | the wearer has never written to the bot's Chaster account | ask them to send one message there first |
| A removed tenant's file is still in `data/tenants/` | the 5-minute grace has not passed, or the file was in use | wait, or run `tenants archive`; check the log for "Archiving tenant ... failed" |
| A stranger writes to the bot | not invited | they get one fixed reply; nothing is stored |

## 8. Backups and safety notes

- Back up `platform.sqlite`, the main database and `data/tenants/` (including `archive/`) together; they are consistent with each other only as a set.
- Never copy a live tenant database into another tenant's file name, and never edit `platform.sqlite` by hand: the registry's uniqueness rules (one Discord id, one lock, one Chaster account per live tenant) are what keep wearers apart.
- The model never sees release times, and inbound Chaster chat is only conversation text in every tenant.
