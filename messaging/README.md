# messaging

Channel-neutral messaging layer (v0.39 dual-role, Phase B). See `docs/architecture/dual_role_and_messaging_proposal.md`.

- `models.py`: `ChannelType` (`discord`, `chaster`) and `UnifiedMessage(user_id, channel_type, content, raw_payload, reply())`. `reply()` answers on the
  channel the message came from; `raw_payload` (ids only) is kept out of `repr`/equality; content is capped at 4000 characters.
- `adapter.py`: `BaseMessagingAdapter` (`send_message(target_id, content) -> bool`, `handle_incoming_message(payload)`). `send_message` never raises.
- `discord_adapter.py`: `DiscordMessagingAdapter` wraps the Discord client: DM delivery (split at 2000 characters), `unify(discord.Message, user_id)`, and
  `handle_incoming_message` that calls an injected handler and replies with its answer (a failing handler gets a fixed neutral text).
- `routes.py` + migration 032 (`message_routes`): per-user default channel and optional per-Chaster-lock override; no row means Discord; a `chaster` route
  must carry the conversation id (`POST /conversations/{id}`). Domain-service writes only; nothing here is reachable from a model.
- `router.py`: `MessageRouter.send_to_user(user_id, text, chaster_lock_id=None)` picks the adapter from the stored route; a preference whose adapter is not
  registered is NOT silently redirected (opt in with `fallback_to_discord`); `send_blocking(...)` bridges worker threads (the periodic cycle).

Not built yet (phase C): `ChasterMessagingAdapter` (outbound `POST /conversations/{id}`, inbound by polling), and moving the bot's `on_message` and the
check-in sender onto the adapter (the check-in port needs the delivered / undeliverable / unknown distinction, which a boolean cannot carry).
`bot.messaging_router` is built in `main()` with the Discord adapter only.
