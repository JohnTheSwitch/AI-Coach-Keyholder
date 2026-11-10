# infractions

The automated detectors (migration 047). Canonical: `docs/architecture/backlog_completion.md`, section 3. On by default: `ENABLE_HYGIENE_WATCH`, `ENABLE_LOCK_CLAIM_WATCH` (each needs its Trust domain to exist, or it reports NOT active).

- **`signals.py`**: the one path from a detection to an Incident: waiting, open, confirmed or dismissed. Only the keyholder's button confirms.
- **`excuses.py`**: Emergency Override, exemption, vacation. A failed read excuses too.
- **`hygiene.py`**: `HYGIENE_MISUSE`, from Chaster's `temporary-opening` state and the wearer's own answer.
- **`lock_claims.py`**: `EPISTEMIC_MISREPRESENTATION`, from `lock_reports` and `unlockedAt`.
- **`wiring.py`**: the configuration and the loop.

## What it never does

- It never confirms, never touches Trust, a token, a window or a lock, and imports nothing that could (guard test). It only reads Chaster and writes local UNCONFIRMED records.
- Mandatory Health Access and the Emergency Override never reach an Incident: they are cleared before one exists, not undone after.
- A health need is never measured or counted. An unanswered opening counts for nothing. Neither is ever a basis for scrutiny (philosophy 3.9).
- It never creates a Trust domain, and a hygiene finding is never filed under another domain (3.7).
