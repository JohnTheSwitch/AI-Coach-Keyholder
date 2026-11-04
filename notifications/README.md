# notifications

The one durable outbound queue (migration 047). Canonical: `docs/architecture/backlog_completion.md`, section 1.

- **`queue.py`**: `enqueue_in_transaction` (same transaction as the fact), `NotificationQueue`, retries, `dedupe_key`. No channel type.
- **`dispatcher.py`**: empties the queue through one injected async port; quiet hours; a failing notice never stops the next one.
- **`delivery.py`**: the channel decision. Plain notices follow the stored preference; cards with buttons or an embed go to Discord.

To add a notice, queue it and give it a stable `dedupe_key`. Pass `immediate=True` only for what the user is waiting for right now. Texts never carry a Trust value, a level or a release time.

A button on a queued card must match a pattern in `bot/action_buttons.REGISTERED_TEMPLATES`, or it is dropped when the card is sent.
