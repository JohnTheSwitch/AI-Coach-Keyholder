# peer_review: community review results -> UNCONFIRMED Incidents (v0.39 Phase I4)

Since Phase I2 the `tasks` and `verification-picture` extensions of the connected account's locks use Chaster community review
(`all_members`, no Chaster punishments). This package reads what the community decided and hands every rejection to the keyholder as
exactly ONE Incident that stays UNCONFIRMED until the keyholder decides (philosophy 2.10, 2.12: never act on a single signal).

## Flow

1. `peer_review_loop` (its own bot task, every `PEER_REVIEW_POLL_SECONDS`, default 600, at least 120) runs `PeerReviewWatch.poll()`.
2. The locked locks of the connected account (worn and held, `LockDirectory.list_account_locks`) are read with two GETs each
   (`chaster/peer_reviews.py`: `GET /extensions/tasks/{lockId}/history`, `GET /locks/{lockId}/verification-pictures`). Kept: review id,
   status (`ongoing` / `verified` / `rejected`), vote counts, submission time. Never kept: task text, verification code, image key, voters.
3. The first complete read of a lock is its BASELINE: reviews that had already ended never create an Incident; an `ongoing` one is
   followed. A partial first read (one of the two GETs failed) postpones the baseline.
4. Per lock, ONE transaction writes the latest state (`peer_reviews`), an append-only transition for every status change
   (`peer_review_transitions`) and, when a review first becomes `rejected`, claims an Incident id on that row. One review can never claim
   two Incidents, however often or by however many processes it is polled.
5. Claimed Incidents are registered through `TrustManager.register_incident_report(incident_id=...)` (rule group `peer_review_rejection`,
   Trust domain `PEER_REVIEW_TRUST_DOMAIN`, evidence: impact LOW, confidence LOW). The id is chosen in advance, so a retry after a crash
   fails on the primary key instead of duplicating the Incident.
6. The owner gets a Discord DM through the notification queue (`notifications/`), naming the incident id, with the buttons to decide.

## No double jeopardy

Nothing here deducts tokens, adds time, freezes, writes to Chaster or confirms. An UNCONFIRMED Incident has no consumer: the Penalty
Engine reacts only to `incident.confirmation_changed`, and every reader of Incidents (Penalty Engine, daily evaluation, plugin SDK) counts
CONFIRMED ones only. Tests assert this (no consumer of `incident.reported`, no confirmation record, no Trust evidence, and the poll class
cannot reach any confirm call). The watch does not stop for the execution runtime halt: it only reads Chaster and writes local,
unconfirmed records.

## Failure alert

`PollHealth` counts consecutive failed polls (a poll that raised or reported any problem: network error, Chaster 4xx/5xx, rate limit,
expired or missing connection). After 3 in a row the keyholder gets ONE Discord DM naming the problems (with the hint to run
`chaster connect` for `reauthorization_required` / `not_connected`); the first clean poll afterwards sends one recovery note. An
undelivered message is retried with the next poll; if the watch recovers before the alert went out, neither is sent.

## Formatted notices (Phase J1)

`notices.py` turns the rejection notice into an embed ("Photo review rejected": lock, votes, incident id, how to decide) carrying the
Confirm / Dismiss buttons, and the watch alert / recovery note into red / green embeds. The plain text remains the fallback.

## The wearer is asked first (owner, 2026-11-10)

With the detectors' path active (the bot's default), a rejection is not an UNCONFIRMED Incident for a keyholder button: it becomes the same question every detector asks (`infractions/README.md`). The wearer explains, the system judges, and an unexplained rejection is confirmed by the system itself. The owner still gets the poll-health alerts. The buttons and the CLI below remain only for the case without that path.

## The keyholder's decision: Discord buttons

The owner's DM about a rejection carries two buttons, **Confirm Penalty** and **Dismiss Incident** (`discord_buttons.py`). They call the
same `PeerReviewDecisions.decide()` as the CLI. Only the owner's verified Discord account can use them (anyone else gets an ephemeral
refusal); a click is the explicit confirmation; afterwards the buttons are removed and the outcome is appended to the message. They are
persistent (`discord.ui.DynamicItem`, custom id `peer_review:<confirm|dismiss>:<incident id>`), so buttons on older notices still work
after a restart. Confirming makes the Incident CONFIRMED (source `keyholder_review`); dismissing leaves it UNCONFIRMED for good.

## The keyholder's decision (CLI fallback)

```
python -m cli peer-reviews list --owner-id <id> [--all]
python -m cli peer-reviews confirm <incident id> --owner-id <id> --yes   # CONFIRMED (source keyholder_review): counts from now on
python -m cli peer-reviews dismiss <incident id> --owner-id <id> --yes   # stays UNCONFIRMED for good
```

## Configuration

`ENABLE_PEER_REVIEW_WATCH` (default true), `PEER_REVIEW_TRUST_DOMAIN` (default `routine`; must exist, otherwise the watch reports
"NOT active"), `PEER_REVIEW_POLL_SECONDS` (default 600). It also needs Chaster fully configured and `CHASTER_EXECUTION_KEYHOLDER_USER_ID`.
Migration 043 holds the ledger. Specification: `docs/architecture/phase_i_proposal.md` section 7.
