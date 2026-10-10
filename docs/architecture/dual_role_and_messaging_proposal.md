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
