# infractions

The automated detectors (migrations 047, 050) and the one path every finding takes to an Incident. On by default: `ENABLE_HYGIENE_WATCH`, `ENABLE_LOCK_CLAIM_WATCH`
(each needs its Trust domain to exist, or it reports NOT active).

## The wearer is asked, the system judges (owner, 2026-11-10)

There is no second person to press a keyholder button: the Keyholder is the system's own role (philosophy 2.9). So a finding is a question first:

1. **Asked.** The wearer gets what was found, with **Explain** and **It was a health or safety need**, and twelve hours to answer.
2. **Health and safety, by code.** The health button, or an answer that mentions a health need, pain, an injury or an emergency, clears it without any
   model (3.9, 3.10). An Emergency Override, an approved exemption or a vacation overlapping the finding clears it at every step.
3. **Judged by the model** (`judge.py`), against the detector's facts, not on trust: `explained` clears it; `unclear` gets one follow-up question, and
   still unclear clears it (2.12); `not_explained` decides it. An apology or admission is not a reason (live, 2026-11-10, the model took one for one):
   it decides it too, and `admitted` makes the confirmed Incident self-disclosed, which softens its Trust effect. "Already explained" is judged on
   the wearer's recent answers to the same kind of finding.
4. **Silence.** An unanswered hygiene finding never counts (it may be the health need Mandatory Access exists for). Anything else unanswered is decided,
   filed under `DETECTION_UNANSWERED`, which is warning-only: Trust and a Warning, never a Penalty Window.
5. **Appeal.** A decided finding counts after two hours. In that time the wearer may appeal once; an accepted appeal clears it, a rejected one counts at once.
6. **Counted.** The system registers the Incident and confirms it itself (`ConfirmationSource.SYSTEM_VERIFIED`). The Penalty Engine decides what follows.

A model that is down or answers nonsense is never a verdict: the answer waits as `judging` and is retried; after three days it is dropped. The wearer's
own words are kept for the audit and erased by `forget all`. Rejected community photos (`peer_review`, through the bot's wiring) and the check-in
escalation's Incident (adopted from `incidents`) take the same path.

Every question, verdict (with its reason and whether code or the model decided), clearing and confirmation is written to `logs/decisions.log`
(`infrastructure/decision_log.py`). After the PC slept, the detector loop moves open deadlines by the time it was asleep (`SLEEP_GAP`, 10 minutes
over the interval).

## Files

- **`signals.py`**: the path above.
- **`judge.py`**: health and safety in code, then the model's strictly parsed verdict. The explanation is fenced as data.
- **`excuses.py`**: Emergency Override, exemption, vacation. A failed read excuses too.
- **`hygiene.py`**: `HYGIENE_MISUSE`, from Chaster's `temporary-opening` state and the wearer's own answer.
- **`lock_claims.py`**: `EPISTEMIC_MISREPRESENTATION`, from `lock_reports` and `unlockedAt`.
- **`wiring.py`**: the configuration and the loop.

## What it never does

- It never touches a token, a window or a lock, and imports nothing that could (guard test). It reads Chaster, writes local records and queues notices;
  the Trust Manager is reached only through the two callables the wiring passes in.
- Mandatory Health Access and the Emergency Override never reach an Incident, and never depend on a model.
- A health need is never measured or counted. Neither is ever a basis for scrutiny (philosophy 3.9).
- Limits derived from Trust are shown to the judge only, never to the wearer.
- It never creates a Trust domain, and a hygiene finding is never filed under another domain (3.7).

## The conversation model knows the questions (owner, 2026-11-12)

Live, "What about my incident?" ten minutes after a finding was counted got "No report from you yet". `get_recent_questions()` hands the open ones
and those of the last 7 days (wording, stage, local deadline; never a score) to
`conversation_engine/context_providers/detector_questions_provider.py`, rendered as authoritative state. Nothing said in the chat counts as the
answer; the user still answers under the system's message.

## Owning up (owner, 2026-11-12, option A; migration 052)

Live, the wearer wrote that they had edged during a hygiene opening, and no rule saw it. Now `self_report.is_hygiene_confession` (deterministic, English
and Czech, deliberately broad) puts a card under the bot's reply: "Did you just tell me you used a hygiene opening for something other than hygiene?".
`InfractionSignals.offer_self_report` stores it as `offered` (kind `self_report_hygiene`, the wearer's words in `explanation`, erased by `forget all`).

- **Yes, I did** -> `decided`, verdict `self_reported`, `admitted` = 1, rule group HYGIENE_MISUSE; after the appeal window it is confirmed with
  `self_disclosed` cooperation, so it weighs less than a detected one. One appeal is possible, as everywhere.
- **No, you misread me** -> cleared, nothing recorded. An unanswered card lapses after 12 hours, also without a trace.
- An approved hygiene opening never excuses it (it happened during one by definition).
- One open card at a time. The model sees the card as `awaiting_confirmation` in its detector-question context.

## The reply to a confession (2026-11-12)

Live, the model answered a confession with "Good for you, taking care of your hygiene needs", and an earlier turn saved it to memory inverted
("without edging"). `ApplicationService.confession_check` (the same `is_hygiene_confession`) now runs before the model: such a turn gets
`CONFESSION_NOTE` as its last system message (never praise, name what they did, not pleased, telling was right, no mention of the card), is a plain
context turn (no coach proposal, nothing saved to memory) and is remembered as conversation. The card under the reply is still what records it.
