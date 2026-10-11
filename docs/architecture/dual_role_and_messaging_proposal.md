# Dual-role locks (wearer + keyholder) and a unified messaging layer: proposal (draft, for review)

Status: PROPOSAL. No runtime code, migration or CLI has been written. This document records what the repository actually
contains, what the request assumes that is not true here, the risks, and a phased plan with the decisions needed.

## 1. The request versus the repository

| The request assumes | What exists |
|---|---|
| `src/` or `app/`, `chaster_client.py`, `bot.db`, `app.cli` | Flat packages; `chaster/lock_client.py` (only `GET /locks`), `chaster/oauth_client.py`, `chaster_execution/transport.py` (`POST /locks/{id}/add-time`, `remove-time`, `freeze`); SQLite `app.sqlite` with numbered migrations (031 applied); no CLI package (only `scripts/execution_control.py`). |
| A bot that may keyhold many wearers | One wearer everywhere: `CHASTER_EXECUTION_WEARER_USER_ID`, one release user, one check-in user, one Trust domain per wearer, one periodic cycle. The keyholder connection is a single internal user (Route A). |
| Self-lock "wearer" calls to preserve | `GET /locks?status=active` with the wearer's own OAuth connection (read-only scopes), used by the status path, the LLM tool and the provider observation. This stays untouched. |
| Messaging by Discord only | `ApplicationService` is channel-agnostic in principle, but delivery is Discord-specific (`bot/discord_bot.py`, `bot/checkin_sender.py`); the check-in sender already is a narrow port (`CheckInSender`). |

## 2. Facts that must be verified before any code

1. **Endpoints.** `GET /keyholder/locks`, `POST /locks/{id}/messages` and `POST /conversations/{id}/messages` are taken from the request, not from the
   Chaster OpenAPI document this project verified against. Only `GET /locks`, `POST /locks/{id}/emergency-unlock`, `add-time`/`remove-time` (owner-supplied
   contract) and `freeze` (documented) are confirmed. Their paths, request bodies, response shapes (`user._id`, `user.username`, `title`, `isFrozen`,
   `status`) and OAuth scopes must be checked against the live OpenAPI document first (the project rule: nothing is inferred). This needs permission to
   download the spec again, and an owner decision whether the existing OAuth app may request additional scopes.
2. **Receiving Chaster messages.** The request mentions "webhooks". Whether Chaster offers a push channel for lock or conversation messages, or only polling,
   is unknown. Polling means a new periodic task and rate-limit handling.
3. **Freeze for managed locks** uses the same documented endpoint and the keyholder connection that already exists; for a wearer's own lock the wearer
   connection would need a freeze scope it does not have today (read-only).

## 3. Risks that change the scope (decisions needed)

- **Other people's locks.** Everything built so far is a personal system for one consenting owner (`philosophy.md`; onboarding and consent are per user).
  Managing locks of other wearers needs, at minimum: explicit per-wearer consent and onboarding, per-wearer limits and Trust domains, a per-wearer emergency
  path that always works, and an owner-level switch to disable all managed-lock actions. Automated barrier actions on a third party's lock must not ship without that.
- **One engine, many wearers.** The release engine, check-ins, daily evaluation, supervision and the rules snapshot all take a single user today. Supporting
  several wearers means passing a wearer context through every periodic task, one execution control (halt) per wearer or global, and per-wearer ledgers (the schema
  already keys most tables by user id; the composition root and the `CHASTER_EXECUTION_*` settings do not).
- **A CLI that freezes any lock bypasses the domain.** The project rule is that physical actions come from decisions (limits, windows, halt, audit). A direct
  `freeze-lock` is useful for live verification (it is exactly the pending test-lock check) but must be explicit: owner-only, `--yes` required, refuses while the
  runtime halt is set, only for locks the bot keyholds, never for a wearer's own lock, and writes an audit row. It is not a path the model can reach.
- **Untrusted inbound text.** A message arriving through a Chaster chat is untrusted input from a third party, like a Discord message but with weaker identity
  proof. It must only ever be conversation input (never a command, never an approval), keep the existing secrecy guards (no release timing in any channel), the
  safety-word handling, and the "free text only" rule for check-in replies. Outbound messages through Chaster publish text to an external service and must be
  behind its own flag.
- **Channel preference and secrets.** Replies must be routed to the channel the user chose; the existing prompt guardrails and the model-facing coarse enums apply
  unchanged to every adapter.

## 4. Proposed design (once the facts in section 2 are verified)

**4.1 Role-aware lock model (read-only first).** `chaster/models.py`: `LockRole` (`wearer` | `keyholder`) and a `ManagedLock` value (lock id, title, wearer
Chaster id and username, `is_frozen`, status, role). `chaster/lock_client.py`: keep `list_active_locks()` as is; add `list_keyholder_locks()` using the keyholder
connection. A thin service merges both into a categorised result; failures of one side never hide the other (partial results are labelled).

**4.2 Storage (additive migration 032, nothing altered or dropped).**
- `managed_locks`: `chaster_lock_id` PK, `role`, `keyholder_user_id`, `wearer_user_id` (nullable), `wearer_chaster_id`, `wearer_discord_id` (nullable), `title`,
  `last_seen_at`; observation data stays in `chaster_provider_observations`.
- `message_routes`: per (wearer user, lock) `channel` (`discord` | `chaster`), `chaster_lock_id`, `chaster_conversation_id`, `updated_at`; one active
  channel per user and lock, default `discord`.
- Mapping of a Chaster wearer to a local user only through the existing identity tables and an explicit link step (no guessing by username).

**4.3 Messaging layer (new package `messaging/`).**
- `UnifiedMessage(user_id, channel_type, content, raw_payload, reply())` (frozen; `reply` delegates to the adapter that produced it).
- `BaseMessagingAdapter` with `send_message(target_id, content) -> bool` and `handle_incoming_message(message)`; `DiscordMessagingAdapter` wraps the existing
  Discord delivery (and the check-in sender keeps working through it); `ChasterMessagingAdapter` calls `send_lock_message` / `send_conversation_message` and,
  if a push or poll source is verified, feeds `handle_incoming_message`. A router picks the adapter from `message_routes`.
- Existing replies keep going through `ApplicationService`; only delivery moves behind the adapter. All sends are best-effort with a boolean result and never
  raise into the periodic cycle.

**4.4 CLI (`python -m cli`).** `list-locks` (read-only, two categorised tables) and `freeze-lock <id> --state true|false --yes` with the safeguards in section 3.
Network calls only on explicit invocation; tests mock the transport.

**4.5 Phases (each approval-gated).** A: verify endpoints, role model, read-only keyholder listing, CLI `list-locks`. B: messaging port, `UnifiedMessage`, Discord
adapter, migration 032 for routes. C: Chaster outbound adapter (flagged off) and the inbound source if one exists. D: guarded `freeze-lock`. E (separate
decision): multi-wearer support in the composition root and periodic tasks.

## 5. Questions for the owner

1. May I download the Chaster OpenAPI document again to verify the three endpoints, the payload shapes and the scopes? (Network use needs your yes.)
2. Is the goal managing OTHER wearers' locks in production, or only listing/testing locks the bot keyholds (for example your own test lock)? If other wearers:
   how is their consent captured, and is a per-wearer emergency path required before any managed-lock action?
3. Should the CLI `freeze-lock` be allowed on a wearer's own lock (needs a freeze scope on the wearer connection), or only on locks the bot keyholds?
4. Outbound Chaster messages: allowed by default per user, or only after the user opts in to the `chaster` channel?
5. Is multi-wearer support in scope now (phase E), or after phases A-D?

## 6. Phase A result: the OpenAPI document verified (fetched 2026-10-10 from api.chaster.app/api-json) and what changed

**Corrections to the request (the three named endpoints were wrong or do not exist).**

| Request | Verified fact |
|---|---|
| `GET /keyholder/locks?status=active` | Does not exist. Managed locks come from `POST /keyholder/locks/search` (operation `KeyholderController_searchLocks`, OAuth scope `keyholder`). Body: `status` (`locked`, `unlocked`, `deserted`, `archived`), `page` (from 0), `limit` (1-50), optional `search`, `criteria`. Response `{pages, total, locks[]}` of `LockForKeyholder` (201). It only searches, so it is read-only. |
| `POST /locks/{id}/messages` ("lock messages") | Does not exist. The public API has no lock-level message endpoint (the nearest things are `/locks/{id}/history` and extension logs, neither a chat). |
| `POST /conversations/{id}/messages` | The send endpoint is `POST /conversations/{conversationId}` with body `{message, attachments?, nonce?}` (201, `MessageForPublic`), scope `messaging`. `GET /conversations/{id}/messages` (params `limit`, `lastId`) reads messages. A conversation is found with `GET /conversations/by-user/{userId}` or created with `POST /conversations` (`users`, `type` private/group, `message`; a `turnstileToken` field exists). |
| Webhooks for incoming chat | None in the public spec. Inbound Chaster messages would have to be polled (`GET /conversations`, `GET /conversations/{id}/messages`). |
| Freeze for managed locks | `POST /locks/{lockId}/freeze` is documented as usable by keyholders on the wearer's locks (scope `locks`): the existing barrier code already works for a managed lock. |

Available OAuth scopes: `profile`, `email`, `locks`, `shared_locks`, `keyholder`, `messaging`, `developer`. The existing connections were made with `locks`
only, so listing managed locks needs the keyholder account to be re-connected with `locks keyholder` (and `messaging` for phase C).

**Implemented in Phase A (read-only).**
- `chaster/managed_locks.py`: `LockRole`, `ManagedLock`, `parse_lock`. Secrets in the payload (`combination`, `offerToken`, `sharedLinkToken`) are never copied.
- `ChasterLockClient.search_keyholder_locks()` (all pages, bounded, no partial lists).
- `chaster/lock_directory.py`: `LockDirectory.list_locks()` merges both roles; each side has its own connection, scope check and short problem code; one side
  failing never hides the other. The existing self-lock path (`list_active_locks`, status context, observation) is untouched.
- `CHASTER_OAUTH_SCOPES` (default `locks`) feeds `ChasterOAuthClient(scopes=...)`; `chaster connect` requests exactly those.
- `python -m cli list-locks`: two categorised tables (ID, title, wearer, wearer id, status, `isFrozen`, test lock) and a PROBLEMS section; exit code 2 when a side
  has a problem. There is deliberately no write command yet.

**How to try it.** Set `CHASTER_OAUTH_SCOPES=locks keyholder` in `.env`, run `chaster connect` from the keyholder account's Discord identity, then
`python -m cli list-locks`. The first real call is made only by that command.

Next (phase B): the messaging port with `UnifiedMessage`, the Discord adapter and migration 032 (roles, mapping, channel preference). Phase C must be built
on `POST /conversations/{id}` plus polling, not on the endpoints named in the original request.

## 7. Phase B result (messaging layer)

Implemented in the new `messaging/` package (see its README): `UnifiedMessage`, `BaseMessagingAdapter`, `DiscordMessagingAdapter`, `MessageRouteRepository`
(migration 032, `message_routes`: default channel per user, optional per-lock override, conversation id required for `chaster`), and `MessageRouter` (stored
preference decides the adapter; no silent redirect unless `fallback_to_discord`). `main()` builds the Discord adapter and the router (`bot.messaging_router`).

Decisions taken: only `message_routes` was added in 032 (the `managed_locks` mapping table of 4.2 waits for the wearer-linking step); the existing
`on_message` flow and the check-in sender are NOT rerouted yet. The check-in port distinguishes delivered / undeliverable / unknown (a miss must never be
charged for a prompt that was not delivered), which a boolean `send_message` cannot express, and `on_message` builds embeds and audit logs. Moving them behind
the adapter is a mechanical follow-up once the Chaster adapter exists; it needs your go-ahead because it touches the working reply path.

## 8. Phase C result (Chaster chat channel)

Built as described in `messaging/README.md` (Phase C): conversation client (send / poll / find), `ChasterMessagingAdapter`, a polling loop with arming and at-most-once
hand-over (migration 033), the opt-in service and the `channel status|discord|chaster` commands, and `ApplicationService.handle_conversation_only` for untrusted
inbound text. Off by default (`ENABLE_CHASTER_MESSAGING=false`); the bot account must be connected with `CHASTER_OAUTH_SCOPES="locks keyholder messaging"`.

Unverified against the live service (to check on the first real run): the ORDER of `results` in `GET /conversations/{id}/messages` and how many it returns by default
(the poller sorts by `createdAt` itself and asks for 50), the maximum message length (chunks are 1500 characters), whether a `pending` conversation accepts messages from the
bot, and the rate limits (hence the 15-second floor on polling). Check-in prompts and replies stay on Discord.

## 9. Phase D result (guarded manual freeze)

Built as specified: `python -m cli freeze-lock <lock_id> --state true|false --owner-id <id> --yes` (service: `chaster_execution/manual_freeze.py`, audit table
`manual_lock_actions`, migration 034). Safeguards: owner-only (`OWNER_USER_ID`), explicit `--yes`, runtime halt check (fail closed), lock-scope verification against a live
keyholder search (only locked locks the keyholder account manages; wearer self-locks are refused), already-in-state short-circuit, and an audit row for every invocation
(timestamp, actor, lock id, requested state, outcome, HTTP status), written before the Chaster call for real attempts.

Decisions: "owner identity" is the internal user id compared with `OWNER_USER_ID`; because the CLI is a local process, the real boundary is access to the machine,
the `.env` and the database, and the check is a deliberate-typing guard plus an audit trail rather than authentication. `cli` was added to the packages that may import the
execution plane (next to `scripts`). The manual command deliberately bypasses the barrier limit pool and the release engine, so a manual freeze can be undone by the
release tick's re-freeze/release logic only through their normal rules; use it on the test lock.
