# reminders

**Question it answers:** should this standing reminder be shown in full now, or was it shown in the last week?

Owner, 2026-11-16: "saying every time a lot of the same information, like if it's an emergency or check the skin, is annoying". Standing
reminders (stop if it hurts, discretion, the health or safety option, the skin check) are shown in full at most once a week each
(`WEEK`). The buttons that carry the way out (health or safety, someone's around) are never affected; they stay on every message.

- **Owns:** `reminder_log` (migration 081): per user and reminder key, when it was last shown in full.
- **API:** `due_in_transaction(tx, user_id, key, now)` (read), `mark_in_transaction(tx, user_id, key, now)` (write + `reminders.shown`
  event), and `take(core, user_id, key, now)` for callers without an open transaction: True and marked when it is due.
- A read failure means "due": a reminder shown once too often is harmless, one never shown is not.

Used by: obedience task DMs (safety, discretion, gear notes; a task with restraints always keeps "Key and shears within reach."),
hygiene openings (the skin check, roadmap 6f), detector questions (the health or safety sentence).
