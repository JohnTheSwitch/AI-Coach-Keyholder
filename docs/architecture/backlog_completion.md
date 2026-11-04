# Carried-forward backlog (owner's request, 2026-11-04)

Closes what `phase_j_proposal.md` 3.3, 4.4 and 6.2 carried forward: the notification engine, the zero-command migration, the two detectors and the exemption
workflow. Migration 047. `philosophy.md` was not changed.

## 1. Notification engine (`notifications/`)

One durable outbound queue. A notice is a row first and a DM second, written in the same transaction as the fact it reports where that fact is local.

| Behaviour | Where |
|---|---|
| Idempotent enqueue (`dedupe_key`), at-least-once delivery | `queue.py` |
| Retry with rising delays, then hourly, `failed` after 30 attempts and never deleted | `queue.py` |
| A row stranded in `sending` by a crash returns after 5 minutes | `queue.py` |
| Quiet hours: routine notices wait at no attempt cost; `immediate` ones (what the user is waiting for) do not | `dispatcher.py` |
| Channel: plain notices follow the stored preference through `MessageRouter`; cards with buttons or an embed always go to Discord, which is the only channel that can show them | `delivery.py` |

Going through it: community review notices and watch alerts, the check-in Warning, the release notices, exemption requests and decisions, detector cards, and every
Penalty Engine consequence (`system/penalty_notices.py`, outbox consumers, so the notice commits with the consumption). A window's start and its first Extension
are one message.

`messaging/discord_adapter.send_dm` is now the only code that opens a DM (a test pins it).

**Deliberately not queued.** Check-in prompts stay synchronous on that same `send_dm`: their deadline starts at delivery and a prompt that was not provably delivered is never
a miss, so the service must know the outcome at once. Tenant invitations keep their own ledger, on the same `send_dm`. Tenants have no queue yet.

## 2. Zero-command UX

- `help` is a short natural description. `CommandRouter.help_text()` stays as the internal contract the prompt tests read, and no user sees it.
- `application/natural_queries.py`: natural questions for the read-only commands, English and Czech, whole questions only, answered by the same handlers. What changes
  something keeps an exact phrase or a button (6.2 question 1). The exact phrases keep working; they are no longer advertised.
- `lock status` carries **I'm locked / I'm unlocked** buttons, the same write as the phrases.
- No CLI command in a DM any more, except the one owner-only attestation in `system/review_consent_dm.py`, which has no button because it is a once-per-lock setup step.
- Tests pin: no `python -m` in the notices, no Trust wording in the new texts, no slash-command tree or prefix framework in `bot/`.

## 3. Detectors (`infractions/`)

Both are off by default, read Chaster only, write only local UNCONFIRMED records, and keep running during a halt. One path to an Incident for both:

```
detected -> waiting (12 h: "it was a health or safety need" clears it for good) -> open (UNCONFIRMED Incident + keyholder card) -> confirmed | dismissed
```

Excuses are asked again just before the Incident is filed: an Emergency Override, an approved exemption or a vacation overlapping the time clears the signal, and a failed
read also clears it. **An unconfirmed Incident has no consumer**, so nothing happens until the keyholder's button (2.12, 3.2).

### `HYGIENE_MISUSE`

There was no hygiene module to read from, so this follows Chaster's own `temporary-opening` state (`GET /extensions/temporary-opening/{lockId}`, `openedAt`). Chaster
cannot tell a shower from a medical need and nothing may try to (3.9), so each opening asks the wearer:

| Answer | Effect |
|---|---|
| Health or comfort need | Mandatory Access. Never measured, never counted, and it can be said at any time, even after "planned" |
| Planned break | Measured: length against the session, number against a rolling week |
| No answer | Nothing. Silence is not evidence |

Limits are `hygiene_privilege_technical_design.md` 2.2, by the `hygiene` Trust domain; a 10-minute grace applies to length. Texts never show them, because they derive from Trust.
A first overrun confirmed by the keyholder is a Warning (minor evidence), not a window (3.8: proportionate). A break the bot did not follow has an unknown close and is
never judged on length.

Chaster's own late-relock penalty is held at 0 by `chaster_execution/extension_guard.py` (owner, 2026-11-04), so an overrun has one judge: this watch, and a health answer really does protect the wearer.

**Limit, by design.** A wearer who calls a planned break a health need is not caught. That is the philosophy's trade-off: a false accusation over a health need is the worse failure.

**Not built.** The rest of the Hygiene Privilege design: requesting a break, denying one, and the Penalty Window override tiers. Those need a decision, because the design doc
is still a draft, and Chaster's own extension already controls when an opening is possible.

### `EPISTEMIC_MISREPRESENTATION`

Compares each `lock_reports` row with the worn locks. The question is "was it false when made", which `unlockedAt` answers, so a report that merely aged is never a lie.

| Case | Outcome |
|---|---|
| Any worn lock is locked | consistent |
| All unlocked, and all ended more than 5 minutes before the report | **contradicted**: a signal |
| The lock ended after the report, or too close to call | stale |
| `emergencyUnlocked`, an exemption, an override, a vacation | excused |
| Missing `unlockedAt`, a deserted lock, no listed lock | stale: uncertainty is left alone |
| A failed read | judged on the next pass |
| Reported **unlocked** while Chaster shows locked | a note to the keyholder, nothing else: honesty never costs anything |

Reports older than the first pass are the baseline. Check-in replies are not read for lock claims: that would be a model's reading of a person's words deciding an Incident.

## 4. Exemptions (`exemptions/`)

```
"I need an exemption" -> reason -> length -> keyholder's card -> Approve -> PenaltyEngine.freeze(temporary_wear_exemption, expires_at)
   -> penalty_window.frozen -> penalty_hold -> unfreeze intent -> penalty barrier (under the runtime halt)
ends by time or "I'm back" -> resume -> freeze intent -> barrier
```

- **Who decides:** the keyholder's button. Never a model, and nothing under `conversation_engine` or `coach` can import this.
- **I7:** `decide()` reads the request only, and the keyholder's card does not mention a window. Applying is a separate step, `tick()`, which also pauses a window that starts during
  an exemption.
- **Time:** no hour moves anywhere. Only the frozen flag and the Penalty Window's own countdown, and the redemption hold stays on.
- **Safety:** every message says an urgent health or safety matter needs none of this.
- **Length:** 6 h, 24 h, 3 days or 7 days. Longer is what a vacation is for.

On this deployment the wearer and the keyholder are the same person, so an approval is a deliberate second step rather than a second opinion. With `ENABLE_EXEMPTIONS` on and the
barrier later enabled, that person can lift a penalty freeze for up to 7 days by pressing two buttons. That follows the owner's request and 3.6.

## 5. Safeguard 3: task and check-in failures

"Affect ONLY the internal Trust score and issue Warnings. They MUST NEVER activate Penalty Windows or lock freezes." Enforced in the Penalty Engine by rule group
(`WARNING_ONLY_RULE_GROUPS`): `CHECKIN_REPEATED_NONCOMPLIANCE` is a Warning however often it is confirmed, and it cannot extend a running window. This **supersedes** the
earlier decision that `check-in-escalations confirm` could lead to a window (`phase_j_proposal.md` 4.4).

Open for the owner: a **rejected task photo** still follows the 4.1 ruleset (a repeated one opens a window). It was left alone because 4.1 names it a confirmed infraction rather than
a task failure.

## 6. Going live

Nothing here is live until a restart. After it: the queue and the penalty notices run, and exemptions are on (`ENABLE_EXEMPTIONS` defaults to true). The detectors need
`ENABLE_LOCK_CLAIM_WATCH=true`, and for hygiene `ENABLE_HYGIENE_WATCH=true` plus a `hygiene` Trust domain (`python -m cli trust-domain create`, a `critical_change`): the runtime never
creates one, and a hygiene finding is never filed under another domain.
