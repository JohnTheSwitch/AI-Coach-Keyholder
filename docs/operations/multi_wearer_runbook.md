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

## 9. Phase F features (Coaching Memory, idea box, Progressive Conditioning)

All of them are off by default and apply to every tenant (and to the owner's own system) when switched on.

| Feature | Flags | Notes |
|---|---|---|
| Structured replies | `CONVERSATION_STRUCTURED_OUTPUT=true` | Required by everything below. Check the model first: `python -m scripts.eval_structured_output` |
| Coaching Memory | `ENABLE_COACHING_MEMORY=true` | Notes about the user's own patterns, goals and quotes; never health, trauma, self-harm or distress |
| Idea box | `ENABLE_AI_SUGGESTIONS=true` | Private suggestions for the owner; never shown to the model again |
| Progressive Conditioning | `ENABLE_SLAVE_TRAINING=true` (with the release engine) + per-database owner switch | Hidden factor on the token payout threshold |

Model settings for Qwen 2.5 32B: `OLLAMA_MODEL=<your qwen2.5 32b tag>`, `OLLAMA_READ_TIMEOUT_SECONDS=120`, `OLLAMA_KEEP_ALIVE=30m`.

**Wearer commands** (in their DM, always available, never sent to the model): `memory`, `forget <id>`, `forget all`, `conditioning status|on|off`, `telemetry`, `research status|on|off`.
`conditioning off` freezes the factor at its current value; it never resets.

**Owner commands:**
- `python -m cli ai-ideas list [--status new|accepted|rejected|archived|all] [--tenant <id>] --owner-id <you>`: suggestions from every live tenant, labelled `<tenant>:<id>`.
- `python -m cli ai-ideas accept|reject|archive <tenant>:<id> [--note "..."] --owner-id <you>`: a verdict on one suggestion. It changes nothing by itself; adopting an idea is a separate
  change that needs the wearer's consent.
- `python -m cli tenants slave-training <tenant> --enable|--disable|--status --owner-id <you>` (`tenant_0` = your own database): the owner switch; `--status` shows the factor.

**How the factor moves:** +0.025 (cap 1.500) only when Chaster confirms a release that a token paid for, with the global flag on, the owner switch on and the wearer's `conditioning` on.
The 72-hour backstop, failed or ambiguous releases, `freeze-lock` and emergency unlocks never raise it. The payout threshold is base x factor (base 1.0 Standard, 2.0 Advanced).
Turning `ENABLE_SLAVE_TRAINING` off returns every threshold to 1.0 (the stored factor is kept, unused).

**Troubleshooting:**

| Symptom | Likely cause | What to do |
|---|---|---|
| Replies are plain, never JSON | `CONVERSATION_STRUCTURED_OUTPUT` off, or the model's JSON is unusable (log `structured_unparseable`) | run the evaluation script; check the model tag |
| `memory` stays empty | the flags are off, the notes were filtered (log `Coaching memory note not stored` with an outcome code), or the daily limit (5) was reached | expected for sensitive topics |
| No suggestions in `ai-ideas list` | `ENABLE_AI_SUGGESTIONS` off, filtered content, or the daily limit | check the flags |
| The factor never rises | global flag, owner switch, wearer `conditioning off`, or no Chaster-confirmed token release yet | `tenants slave-training <id> --status`; the audit table `training_increments` records why each release did or did not count |
| First reply after a pause takes a minute | the 32B model is loading | expected; keep `OLLAMA_KEEP_ALIVE=30m` |

## 10. Phase G1: local Behavioral Telemetry

Always on, no flag. The last step of every periodic cycle derives numeric records (check-in response delays, token spends, conditioning increments, daily
summaries) from tables the system already writes, in each tenant's own database. Nothing is sent anywhere, and nothing reaches the model.

- **Consent:** a tenant is recorded only after accepting an invitation that discloses it. Wearers who accepted an older invitation are not recorded (their
  `telemetry` says so). There is no re-consent command yet.
- **Wearer:** `telemetry` shows the categories, counts and oldest record (never the conditioning factor); `forget all` erases it together with the Coaching Memory.
- **Runtime samples (G2):** each model reply adds an `llm_generation` record (latency, structured-output status) and, from the second reply on,
  a `conversation_gap`. They are held in memory (at most 2000 per database) until the next periodic cycle; a restart loses the undrained ones.
  Many `unparseable` statuses mean the model's JSON is unreliable: run `python -m scripts.eval_structured_output`.
- **Retention:** 180 days, pruned once a day.
- **Owner analysis (G3):** `python -m cli research analyze <tenant> [--days 30] [--output report.txt] --owner-id <you>` (`tenant_0` = your own database).
  Stop the bot first (or wait 10 minutes after it last ran a cycle), or pass `--allow-while-live` and accept slower replies. It uses the local Ollama
  only (`RESEARCH_OLLAMA_MODEL`, default `OLLAMA_MODEL`; `--timeout` default 600 s). The report ends with the consent warning: any parameter change it
  suggests is a `critical_change` that needs the wearer's explicit consent first. Nothing is stored; keep the file private.
- **If records stop appearing:** check the log for `Behavioral telemetry collection failed`; the periodic cycle only runs while at least one periodic feature
  (rules engine, supervision, release engine, check-ins) is enabled. The table is `behavioral_telemetry`; the read positions are in `telemetry_watermarks`.
- **Research export (G4, opt-in):** set `ENABLE_RESEARCH_TELEMETRY=true`, `TELEMETRY_ENDPOINT=https://...` (the receiver), `TELEMETRY_TOKEN` (issued by the
  receiver) and `TELEMETRY_HMAC_KEY` (32+ random characters, keep it forever: a new key makes earlier exports unerasable). The startup notes say when one is
  missing. Each wearer decides with `research on|off`; only records made after `research on` leave, pseudonymized. `research off` / `forget all` send an
  erase request (even with the flag off). An outage logs one warning (`Research telemetry link is down`) and one recovery line; nothing is lost meanwhile.
  Pending items: `research_erase_requests WHERE sent_at IS NULL`, `behavioral_telemetry WHERE synced_at IS NULL`.
- **Running the receiver (G5):** see `hive_mind_receiver/README.md`: its own virtual environment outside the repository, one token per sending
  instance in `TELEMETRY_TOKENS` (remove a token to revoke that sender), `python -m hive_mind_receiver`, and a Cloudflare tunnel to
  `http://127.0.0.1:8787`. A 401 or 422 from the receiver shows in the bot's log as `Research telemetry link is down (HTTP 401)`.
