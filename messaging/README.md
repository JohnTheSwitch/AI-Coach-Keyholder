# messaging

Channel-neutral messaging layer (v0.39 dual-role, Phase B). See `docs/architecture/dual_role_and_messaging_proposal.md`.

- `models.py`: `ChannelType` (`discord`, `chaster`) and `UnifiedMessage(user_id, channel_type, content, raw_payload, reply())`. `reply()` answers on the
  channel the message came from; `raw_payload` (ids only) is kept out of `repr`/equality; content is capped at 4000 characters.
- `adapter.py`: `BaseMessagingAdapter` (`send_message(target_id, content) -> bool`, `handle_incoming_message(payload)`). `send_message` never raises.
- `discord_adapter.py`: `DiscordMessagingAdapter` wraps the Discord client: DM delivery (split at 2000 characters), `unify(discord.Message, user_id)`, and
  `handle_incoming_message` that calls an injected handler and replies with its answer (a failing handler gets a fixed neutral text).
- `routes.py` + migration 032 (`message_routes`): per-user default channel and optional per-Chaster-lock override; no row means Discord; a `chaster` route
  must carry the conversation id (`POST /conversations/{id}`). Domain-service writes only; nothing here is reachable from a model.
- `send_dm` (`discord_adapter.py`) is the only code that opens a DM. Proactive notices reach it through `notifications/`, check-in prompts directly (`bot/checkin_sender.py`).
- `router.py`: `MessageRouter.send_to_user(user_id, text, chaster_lock_id=None)` picks the adapter from the stored route; a preference whose adapter is not
  registered is NOT silently redirected (opt in with `fallback_to_discord`); `send_blocking(...)` bridges worker threads (the periodic cycle).

Phase C (below) added the Chaster channel. Still not done: moving the bot's `on_message` and the
check-in sender onto the adapter (the check-in port needs the delivered / undeliverable / unknown distinction, which a boolean cannot carry).
`bot.messaging_router` is built in `main()` with the Discord adapter only.

## Phase C: the Chaster chat channel (migration 033, `ENABLE_CHASTER_MESSAGING`, default on; each user still opts in)

- `chaster/conversation_client.py`: `POST /conversations/{id}` (send), `GET /conversations/{id}/messages` (poll), `GET /conversations/by-user/{userId}`
  (find); scope `messaging`.
- `chaster_adapter.py`: `ChasterMessagingAdapter` (outbound via the bot account's token, chunks of 1500 characters, best effort; inbound through an injected
  handler, answered on the same conversation).
- `chaster_poller.py` + `chaster_loop.py`: Chaster has no webhooks, so conversations of OPTED-IN users are polled every `CHASTER_CHAT_POLL_SECONDS` (min 15).
  The first poll only arms (no history replay); only plain `message` entries sent by the user's own Chaster account after arming and not older than a day are
  handed on (the bot's own messages, other participants and system logs are ignored); at most 3 per poll; ids are recorded as seen BEFORE hand-over
  (at-most-once). The provider's page order is not assumed.
- `channel_service.py` + the `channel status|discord|chaster` commands: the per-user opt-in. `channel chaster` needs the flag, the user's Chaster
  connection, a bot connection with the `messaging` scope and an EXISTING Chaster conversation with the bot account (the user messages it first).
  Switching back is `channel discord`, only from Discord.
- **Untrusted text:** inbound Chaster messages enter `ApplicationService.handle_conversation_only` - conversation input only. Never a command, an onboarding
  answer, a check-in reply or an approval; plain text replies; no reply for a user who has not finished onboarding.
- **Secrecy:** the reply is produced by the same conversation engine (same guardrails, same coarse release enum, no timestamps). The `messaging` package imports
  no release/rules/penalty/trust module (AST-guarded).
- With the flag off, or when Chaster delivery fails, the production router falls back to Discord. Check-in prompts still go to Discord only.


## Keyholder inbox watch (owner, 2026-11-13)

Nobody reads the keyholder account. `keyholder_inbox.py` lists its Chaster conversations every 5 minutes (`GET /conversations`, statuses
`pending` and `approved`, scope `messaging`, read-only; `ChasterConversationClient.list_conversations`) and forwards each new last message from
anyone but the owner (`OWNER_CHASTER_ACCOUNT_ID`) and the account itself to the owner's DM through the notification queue, once. Forward only
(the owner's choice): nothing is ever sent from the account. The first successful pass is the baseline. State: `keyholder_inbox_seen`
(migration 059), conversation id and last message id only. Active when Chaster chat is ready and OWNER_USER_ID is set.
