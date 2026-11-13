# Honesty and proof: a system made for a wearer who may lie (DRAFT)

Owner, 2026-11-13: "the system should also be more made for brats who could lie", and: think about what Chaster makes possible, about
convenience, and that to prove something it has to be reported right after it. **Draft: nothing here is approved until the owner approves a slice.**

## 1. The problem, on our own data

Most of what earns Trust and goal results is the wearer's word. From `daily_commitments`:

| Commitment | Made | Reported kept | What it shows |
|---|---|---|---|
| "Do a full workout today" (11-05) | 20:26:58 | 20:27:25, **27 seconds later** | Already caught: "kept too soon" earns nothing since today |
| "Go to bed 30 minutes earlier" (11-10) | 11-10 | **11-12**, two days later | Not caught. Nobody can prove a bedtime two days later, in either direction |

A brat learns within a week that a tap on **Kept** is free. Detecting every lie is impossible. The goal is to make a lie a **bad bet**:
what a lie can win must be smaller than what it risks, and owning up must always be cheaper than being caught. The honest path stays one tap.

## 2. Principles

1. **Proof is bound to time.** Proof is only possible close to the act, so a claim is only worth something when it is made close to the act:
   not before it could have happened (the existing "too soon" rule), and not long after (new: "too late").
2. **The bot asks at the right moment.** The wearer should not have to remember to report. The commitment carries a time; the bot asks "Done?"
   when that time ends, while proof is still possible.
3. **Proof is a spot check, not a tax.** Most claims are never checked. Which ones are is random and unknown in advance, so every claim carries risk.
4. **Someone other than the wearer judges a proof.** There is no second person and the local model sees no pictures. Chaster's community does
   (its peer verification), Chaster's own code proves the picture is fresh, and Chaster's history gives exact times. The bot only reads results.
5. **Unknown stays unknown.** A claim that cannot be proven is never treated as a lie. Only a failed spot check or a proven contradiction counts.
6. **Health is never scrutinized** (philosophy 3.9). No proof is ever asked for a health-related commitment, and the health answer clears any question.

## 3. What Chaster gives us (from `chaster_capability_map.md`)

| Capability | Status | Use here |
|---|---|---|
| **Tasks: `assignTask` as keyholder** (`POST /locks/{id}/extensions/{extId}/action`) | **Verified** 2026-11-04 | The bot assigns a real Chaster task at the moment of a spot check |
| Task items `verificationRequired`, `codeRequired` | Spec; currently forced off by the I3 consent revert | Proof photo in Chaster, with a code Chaster shows, so an old photo does not work |
| Tasks `peerVerification` (community votes) | In use for community review; the bot already reads the results (`peer_review/watch.py`) | The judge of the proof is not the wearer and not a model |
| Task timer: `start-task-timer`, `complete-task` | Spec | Duration activities: Chaster timestamps the start and the end |
| `POST /locks/{id}/history` (`tasks_task_completed`, openings, freezes) | Verified | Exact provider times for claims and contradictions |
| Verification picture request (`POST /extensions/verification-picture/{id}/request`) | Spec; a request cannot be cancelled | Already the basis of photo check-ins (lock is on). Not proof of an activity |
| Partner `hygiene-recorder`, `taskverification` | Installs; behaviour untested | Not needed for the first slices |

**To check live before building** (one short test on the live lock, with the owner's go-ahead, task unassigned afterwards):
whether `codeRequired` shows the wearer a code to put in the photo; whether Chaster accepts a picture from the gallery or only the camera;
how long community votes take in practice; what the history entries of an assigned, timed and completed task contain.

What Chaster cannot do: judge whether a photo really shows the activity (the community does that, imperfectly), and prove anything that is
not visible (sleep, abstinence, "did not").

## 4. Claim levels

Every report gets a level, recorded with it. The level decides what the claim earns, not whether it is "true".

| Level | What | Earns | Example |
|---|---|---|---|
| **P: provider fact** | Chaster's own record | Full | A relock time from the history; a picture submitted before the deadline |
| **A: proven** | A spot check passed: community-verified task photo with Chaster's code | Full, plus a small Honesty bonus | Workout task photo verified |
| **T: timely claim** | One tap inside the report window | Small (today's values) | "Done" at 19:05 for a workout planned 18:00-19:00 |
| **L: late or too soon** | Outside the report window | Nothing, and no penalty | Kept two days later; kept 27 seconds after making it |

## 5. Time binding

- **At creation, the commitment gets a window.** "When will you do it?" with buttons (morning / afternoon / evening / a time), or a time read
  from its text. No answer: the whole waking day (quiet hours excluded). The window must end before quiet hours, like the check-in slots.
- **The report window** runs from the window start plus the duration the text names (the existing too-soon rule) to the **window end plus
  REPORT_GRACE** (proposal: 2 hours). A report in it is level T; outside it, level L.
- **The bot asks at the window end**: "Done with <commitment>?" **Done / Not done / Still doing it** (the last moves the question once, by
  up to an hour, never past the grace). Unanswered: the commitment lapses at the end of the grace, at no cost, as today.
- **Duration activities (option):** the wearer starts the Chaster task timer when starting and completes it when done; Chaster's two times are
  level P for "it took this long". Depends on the live test.

## 6. Spot checks

On a **Done** at level T, the bot decides at random whether to check it.

- **Rate by Honesty level** (proposal): level 4: 10 %, level 3: 20 %, level 2: 35 %, level 1: 50 %. At most one open check at a time, at most
  one a day. No check during an exemption, a vacation, an Emergency Override, or for a commitment with a health or safety topic.
- **The challenge:** "Show it: in Chaster, complete the task '<commitment>' with a photo that includes the code Chaster shows. You have 60
  minutes." The bot assigns that task at this moment (verification and code required, community review on).
- **Answers:**
  - Photo submitted in time and **verified** by the community: level A. Small Honesty bonus.
  - **"I can't show it now"** (a button): the claim drops to level L (earns nothing), no penalty. One per week is free; beyond that it counts as
    **Not kept** (the existing minor reduction). Declining is never a lie, but it never pays either.
  - **Nothing by the deadline**, or the photo **rejected**: the finding takes the existing infractions path (asked, health clears it in code,
    explanation judged, one appeal). Decided: the claim is voided (the commitment becomes Not kept, its goal day missed) and a **false report**
    Incident is confirmed in the Honesty domain.
  - Chaster unreadable, or the vote still running: never a miss. The check waits for the vote, and after a limit it is dropped.
- The bot never sees the picture (as with photo check-ins). Only the community does, and **it sees the task text**: see question 4.

## 7. The Honesty Trust domain

A new domain, `honesty`, created with consent (TI1).

- **Evidence:** a passed check (+0.01), a confirmed false report (large, like a confirmed detector Incident), a confirmed lock-claim
  misrepresentation (move `LOCK_CLAIM_TRUST_DOMAIN` here: it is a lie about the lock), a retraction (neutral, see 8).
- **Effect, first version:** only the spot-check rate (section 6). It does not change releases, the token pool or supervision; it only
  decides how closely claims are watched. Later, if wanted: the weight of level T claims.
- Short-term and long-term work as for the other domains.

## 8. Owning up beats being caught

| What the wearer does | Cost |
|---|---|
| Reports **Not done** | The usual minor reduction in routine. Nothing in Honesty |
| Reports **Done**, then retracts it ("Actually I didn't", a button under the report) within 12 hours and before any check | Same as Not done. Nothing in Honesty |
| Confesses after being checked, before the verdict | Decided as admitted: self-disclosed, which softens it (existing rule) |
| Is caught: check missed or rejected | Not done **plus** the false-report Incident in Honesty, and more checks afterwards |

## 9. Contradictions from facts we already have

Deterministic, and only on facts. A contradiction is a question first (the infractions path), never an automatic verdict.

- **Bedtime and "no phone after" commitments:** a DM to the bot after the stated time that night contradicts "kept". Our own message times, no model.
- **Lock claims and hygiene:** already built (`infractions/lock_claims.py`, `hygiene.py`).
- **Chaster history:** a task completed outside the time the wearer claimed.
- **Not done, deliberately:** guessing from chat content or Discord presence. Too speculative; it would punish honest wearers.

## 10. Convenience

- Honest and unchecked: **one tap**, at a moment the bot chose for them. Checked: one photo in the Chaster app they already use, 1-2 a week
  when trusted, at most one a day.
- Proof is asked only for what can be proven. Commitments that cannot be photographed (sleep, abstinence) are never challenged; they stay level T
  and are covered only by contradictions.
- Optional at creation: "How would you show it?" **Photo / Timer / Can't be shown**. The last makes it uncheckable and worth level T at most.

## 11. Consent

- The owner's consent for the Honesty domain, for turning task `verificationRequired` / `codeRequired` / community review on (reverses part of
  I3), and for spot checks.
- Additional wearers: opt-in (`proof checks on|off`), off by default, named in the invitation text. Off means level T claims stay as today,
  with no checks and no Honesty domain effect.

## 12. Slices

1. **Time binding (built 2026-11-13, see `coach/README.md`):** commitment windows, the "Done?" ask at the window end, the report window
   (levels T / L). No Chaster, no checks. Retraction moved to slice 3: it only matters once a check can follow a Done.
2. **Live Chaster test:** task with code and community review, timer, gallery, history entries. Results into `chaster_capability_map.md`.
3. **Honesty domain and spot checks** with Chaster tasks, retraction of a Done, the "can't show it now" pass, the infractions path for missed or rejected proof.
4. **Contradictions:** bedtime / "no phone after", Chaster task times. Move lock claims to Honesty.
5. **Additional wearers:** opt-in and invitation text (with the multi-wearer parity slices).

## 13. Owner decisions (2026-11-13)

Approved as a design, built one slice at a time:

1. Spot-check rates 10 / 20 / 35 / 50 % by Honesty level, at most one check a day.
2. Report grace after the window: 2 hours (default, not objected to).
3. One free "can't show it now" pass a week (default, not objected to).
4. The Chaster task shows the **commitment text as written**: reviewers can judge the photo.
5. A confirmed false report costs **Trust and a Warning first**, no Penalty Window (like `DETECTION_UNANSWERED`); tighten later.
6. Lock-claim Incidents move to the `honesty` domain (default, not objected to; done in slice 4).
7. The live Chaster test (slice 2) may run when that slice is reached; the owner is told right before it runs.
