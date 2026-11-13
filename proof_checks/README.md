# proof_checks

Proof spot checks (owner, 2026-11-13; honesty and proof, slice 3; migration 063). Design and the owner's decisions:
`docs/architecture/honesty_and_proof_proposal.md`. Chaster behaviour, verified live: `docs/architecture/chaster_capability_map.md` section 8.

## What happens

1. **Picked.** When the wearer presses **Done** inside a commitment's report window (`coach/desk.py`), `ProofChecks.select` rolls against the rate
   of the Honesty level (`CHECK_RATE`: level 4 10 %, level 3 20 %, level 2 35 %, level 1 50 %). Never during a vacation, for a commitment that
   mentions health or safety, more than once a local day, or while another check is open. Picked: the Kept is recorded as `checking` and
   earns nothing yet.
2. **Started** (at once, and by the loop every minute): the bot assigns a Chaster task **"Show: <commitment>"** with a photo and Chaster's code
   required, community review on, and sends the challenge (`immediate`) with **I can't show it now** and **I didn't actually do it**.
   `PROOF_DEADLINE` is 60 minutes for the submission. A check that cannot start (runtime halt, no lock, no `tasks` extension, the wearer already
   has an open task, Chaster unreachable) is cancelled and the Kept simply counts.
3. **Decided** (the loop reads the task history every 2 minutes while a check is open):

| What | Status / outcome | The Kept | Honesty |
|---|---|---|---|
| Photo in time, community **verified** | passed / verified | counts (Trust, goal result as of the report) | `PROOF_VERIFIED` +0.01 |
| Vote still open after `VOTE_LIMIT` (12 h), or history unreadable for `UNKNOWN_LIMIT` (6 h) past the deadline | passed or cancelled / unknown | counts | nothing |
| Photo **rejected**, nothing by the deadline (the task is taken back), or a photo after it | failed / rejected, missed, late | earns nothing | the infractions path: asked to explain; unexplained -> `FALSE_REPORT`, **warning only** (Trust and a Warning, never a Penalty Window) |
| **I can't show it now**, first in 7 days | declined / free_pass | earns nothing | nothing |
| **I can't show it now** again within 7 days | declined / pass_used_up | becomes **Not kept** | nothing |
| **I didn't actually do it** | retracted | becomes **Not kept** | nothing: owning up beats being caught |

## Activation (owner, 2026-11-13: on by default)

`ENABLE_PROOF_CHECKS` is on. The checks run once the `honesty` Trust domain exists, which `setup init` creates with the other standard domains
(its consent record shows the explanation), or `python -m cli trust-domain create honesty ...`. It is never created silently. Honesty starts at
**0.75** (level 3, a 20 % rate: `trust_manager.domain_admin.STARTING_SCORES`), so a new wearer is not met with suspicion. The lock needs Chaster's
Tasks extension with community review, or every check is cancelled (`setup check` says so). Additional wearers: not yet, `tenant_config` turns it
off until their invitation discloses spot checks and they can switch them off.

## What it never does

- Changes time, freezes, unfreezes or unlocks (`chaster_tasks.py` has exactly two writes: assign and take back a task, only while the runtime
  halt is clear). Imports no execution, release or penalty code (test).
- Sees the photo: the community judges it, the bot reads only the result.
- Counts a review twice: the community review watch skips a spot check's task (`PeerReviewWatch.is_spot_check`), and when it cannot tell, it
  skips too.
- Holds an unknown against the wearer.

`forget all` erases the stored commitment text.
