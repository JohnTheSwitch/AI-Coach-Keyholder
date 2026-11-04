# Chaster capability map

What this system can do on Chaster, from three sources, on 2026-11-04: the official OpenAPI document (v0.35.0, 190 paths), the public catalogues (`GET /extensions`,
`/permissions/definitions`, `/extensions/penalty/*`), and live tests on our own test lock. **Verified** means tried on the real API. **Spec** means read in the document and not tried.

## 1. Extensions

### How writing them works (verified)

`POST /locks/{lockId}/extensions`, body `{"extensions": [...]}`, HTTP 201. Each item needs `slug`, `config`, `mode`, `regularity`. Needs the `lock.extensions.edit` permission.

| Behaviour | Result |
|---|---|
| The list is the whole truth | A slug left out is removed. A new slug is added. |
| A slug that stays is updated in place | `_id`, `createdAt` and `userData` survive. Resending an identical list changes nothing at all, not even `updatedAt`. |
| Config edit | Applied at once. Does not touch cooldowns. |
| `regularity` edit | Applied at once. `nextActionDate` did not move. |
| Removing, then adding back | A new `_id` (verified on `pillory`, which holds no state). So expect **`userData` to be lost**: for `tasks` that would be the wearer's task list, for `verification-picture` the history. Deliberately not tested on those two. |
| Invalid request | 400 with a clear message, and nothing is applied: a bad item does not half-apply the list. A mode the extension does not offer: "The selected mode is not supported by this extension." Unknown slug: "Extension x not found". |
| A frozen lock | Editable. The frozen state, the end date and the status were unaffected. |

**Rule:** to change an extension, resend the full list with that one change. Never remove `tasks` or `verification-picture` to "reset" them.

`chaster/extension_plan.py` still refuses to rebuild a list containing a partner extension. That is the safe default, not a hard limit: see the partner section below for what actually happens.

### First-party

| Slug | Name | Modes | Default config | Changes lock time by itself? | Use for us |
|---|---|---|---|---|---|
| `temporary-opening` | Hygiene opening | non_cumulative, schedule | `openingTime` 900, `penaltyTime` 43200, `allowOnlyKeyholderToOpen`, `requireVerificationPictureBefore/After`, `freezeLockWhileOpen` | **Apparently**: `penaltyTime` reads as time added on a late relock (not observed) | In use: the hygiene watch reads it |
| `tasks` | Tasks | non_cumulative, cumulative, unlimited, schedule | `tasks[]` (`task`, `points`, `verificationRequired`, `codeRequired`, `duration`), votes, points, `allowWearerTo*`, `actionsOnAbandonedTask`, `peerVerification` | Only through configured actions | In use: community review |
| `verification-picture` | Verification picture | non_cumulative, unlimited, schedule | `visibility`, `peerVerification`, `triggerInitialVerification` | No | In use: community review |
| `penalty` | Penalties | unlimited | `penalties[]` | **Yes**, if a penalty is configured | Chaster's own rule engine, see below. Conflicts with Zero-Time unless limited to freeze-type consequences |
| `pillory` | Pillory | unlimited | `timeToAdd`, `limitToLoggedUsers` | **Yes**: public votes add time | On our locks. Not the bot's doing, still a time source |
| `link` | Share links | unlimited | `timeToAdd`, `timeToRemove`, `enableRandom`, `limitToLoggedUsers`, `visibleOnProfile` | **Yes**: visitors | On our locks. Same note |
| `dice` | Dice | 4 modes | `multiplier`, `nbSides` | **Yes** | Not with Zero-Time |
| `wheel-of-fortune` | Wheel of Fortune | 4 modes | `segments[]` | **Yes** | Not with Zero-Time |
| `guess-timer` | Guess the Timer | unlimited | `minRandomTime`, `maxRandomTime` | **Yes** | Not with Zero-Time |
| `random-events` | Random Events | unlimited | `difficulty` | **Yes** | Not with Zero-Time |

### Partner (23)

Tried live on 2026-11-04: each added with its default config on the keyholder test lock, read back, removed at once. **21 of 23 were accepted** with a plain `slug`, `config`, `mode` and `regularity`; no
`partnerConfigurationToken` is needed to add one. Nothing was used or played, so this shows that they install, not how they behave over time. Installing one also tells that partner's own server about the
lock (Chaster's `extension_session.created` event). Which group each belongs to is a reading of its description and default config.

| Group | Extensions |
|---|---|
| **Skipped, on purpose** (`isLockingProvider`: they could take over how the lock opens) | `research-and-desire-extension`, `reach-lock` |
| **Can lead to an unlock** (never on a lock we care about) | `unlock-condition`, `keyshare` |
| **Run on their own** | `programmable-lock` (runs code: its default freezes on start and adds time every 10 hours), `random-actions`, `weatherlock`, `pay-attention`, `quest` |
| **Games and rewards that change time or freeze** | `find-the-key-1`, `gambling`, `scratchcards`, `simon-memory-game`, `jigsaw-puzzle`, `wordle`, `behave`, `typing-lines`, `rewards-shop` |
| **Worth a look** | `hygiene-recorder` (video proof and keyholder review), `taskverification`, `pay-attention` (can freeze instead of adding time), `obedience-1` |
| Not relevant | `footwear-control`, `sync-with-exposedrush` |

Adding `guess-timer`, `pay-attention`, `jigsaw-puzzle` or `wordle` turned the lock's `extensionsAllowUnlocking` to false while installed; it went back on removal. The other 22 did not change it, and none
changed `status`, `isFrozen` or `endDate`.

**A partner's config cannot be edited through `POST /locks/{id}/extensions`.** Resending the list with a partner in it, with no token, keeps it (same `_id`), but a changed config is silently dropped: HTTP 201 and
the old value stays (verified on `simon-memory-game`). Its config lives behind its own configuration page and token. So an HTTP success proves nothing here: re-read after every write.

### Chaster's own Penalties engine

`penalty` can punish missing a habit: verification pictures, tasks, opening time limits, voting on others' pictures, tasks or pillories (`GET /extensions/penalty/punishable-actions`, `recurring` or
`time_limit`). It overlaps our Penalty Engine, and I2 deliberately keeps Chaster's own punishments empty, so a rejection is decided in one place only.

## 2. Lock actions

| Endpoint | What it does | Status for this system |
|---|---|---|
| `POST /locks/{id}/freeze` | Freeze or unfreeze | In use (`set_frozen`), under the halt |
| `POST /locks/{id}/extensions` | Edit extensions | In use (`set_extensions`), under the halt |
| `POST /locks/{id}/settings` | `displayRemainingTime`, `hideTimeLogs`, `minLimitDate`, `emergencyUnlockEnabled`, `emergencyUnlockPhrase`; 204; **verified** with `hideTimeLogs` | Available. Keep away from the two emergency fields |
| `PUT/GET/DELETE /keyholder/notes/lock/{id}` | A private note on a lock; **verified** | Available: a place for keyholder context |
| `POST /locks/{id}/history` | Lock log. A read, despite the POST; **verified** | Available: `extension_enabled`, `extension_updated`, `lock_frozen`, `lock_unfrozen`, `time_changed`, `tasks_task_assigned`, `tasks_task_completed`, ... |
| `GET /extensions/temporary-opening/{id}` | Is it open now; **verified** with the production parser. An unlocked lock can keep reporting a stale `openedAt` (seen on `6aba61d...`, from October), so read it only on a locked lock, as the watch does | In use by the hygiene watch |
| `POST /extensions/tasks/{id}/unassign-task` | Take a task back; **verified**, 201 | Available |
| `POST /extensions/tasks/{id}/complete-task`, `/start-task-timer` | Task control | Spec |
| `POST /extensions/verification-picture/{id}/request` | Ask for a picture | Spec. A natural keyholder tool |
| `POST /locks/{id}/extensions/{extId}/action` | Generic extension action; names are per extension and undocumented. **Verified** for `tasks`: `{"action": "assignTask", "payload": {"task": {"task": "...", "points": 0}}}` as keyholder, 201 | **New capability, not used yet:** the bot could assign real Chaster tasks |
| `GET/PATCH /locks/{id}/extensions/{partyId}/schedule` | Extension schedules | Spec. Needs `lock.extensions.schedules` |
| `POST /locks/{id}/permissions/proposal` (+ accept, reject, withdraw) | Change the permission contract. Parts that only widen the other party apply at once | Spec. A `critical_change` in spirit |
| `POST /conversations/{id}` | Chat | In use (Chaster channel) |

### Never, for the reasons given

| Endpoint | Why |
|---|---|
| `POST /locks/{id}/unlock`, `/emergency-unlock` | Owner's ban. It would also mean setting the lock up again |
| `POST /locks/{id}/update-time` | Zero-Time-Modification |
| `POST /extensions/temporary-opening/{id}/open`, `/resume`, `/combination` | Physically opens the lock and hands out the combination |
| `POST /locks/{id}/archive`, `/resign`, `/convert-to-self-lock`, `/trust-keyholder`, `/max-limit-date`, `PUT .../deserted-combination-recovery-policy` | Irreversible, or end the keyholding |

## 3. Permissions

`GET /permissions/definitions`, public. Each lock has a contract: a `preset` plus `grants`, per resource and per subject (wearer, keyholder). Resources: `lock.time.add`, `lock.time.remove`, `lock.freeze`,
`lock.unfreeze`, `lock.min_limit_date`, `lock.max_limit_date` (wearer only), `lock.timer_visibility`, `lock.time_logs_visibility`, `lock.extensions.edit`, `lock.extensions.schedules`,
`extension.link.visible_on_profile`, `lock.bondage_safety`, `lock.emergency_unlock`.

Note: the `standard` preset gives **nobody** `lock.extensions.edit`.

| Lock | Preset | Our role | Extensions edit | Freeze | Test lock |
|---|---|---|---|---|---|
| `6ae98ca...` | `full_trust` | wearer | wearer and keyholder | wearer and keyholder | yes |
| `6aca95c...` | `trusted_keyholder` | keyholder | keyholder only | keyholder only | yes |

## 4. Lock fields worth knowing

`status` (locked, unlocked, deserted), `isFrozen`, `frozenAt`, `unlockedAt`, `endDate`, `minLimitDate`, `maxLimitDate`, `emergencyUnlocked`, `isTestLock`, `trusted`, `canBeUnlocked`, `reasonsPreventingUnlocking`,
`extensionsAllowUnlocking`, `displayRemainingTime`, `hideTimeLogs`. On each extension: `_id`, `mode`, `regularity`, `nbActionsRemaining`, `nextActionDate`, `userData`, `config`.

## 5. Partner API

About 50 endpoints under `/api/extensions/*` and `/api/developers/*`, with a developer token. Registering this system as a real Chaster extension would give event streams (`GET .../sessions/{id}/events`),
regular actions, session logs, task assignment, verification requests and a page inside Chaster. It would replace our polling, but it means a published or testing extension. A
larger decision, not needed today.

## 6. Open points from this map

1. **Hygiene late-relock penalty: resolved by the owner, 2026-11-04.** `penaltyTime` is now 0 on both locks, and the bot keeps it there itself (`chaster_execution/extension_guard.py`, every 15 minutes, driven by
   `REQUIRED_SETTINGS` in `chaster/extension_plan.py`). A relock that is late for a health reason no longer costs time, and the bot alone judges overruns. Still open: Chaster's own limits (15 minutes,
   every 2 days) are tighter than the design doc's 20 to 30 minutes and 3 to 7 a week, so the frequency limit can only ever trigger at the lowest level.
2. **`pillory` and `link` still change time** on our locks, by themselves. Zero-Time-Modification binds the bot, not Chaster. If the goal is that time never moves at all, removing
   `pillory` and `link` (no stored state, verified) achieves it.
3. **A stricter barrier option:** `temporary-opening.freezeLockWhileOpen`, and `allowOnlyKeyholderToOpen` could make every opening a keyholder decision. The second would conflict with Mandatory Access, so it stays off.
4. **`requireVerificationPictureBefore/After`** on hygiene openings is off since the consent revert (I3); turning it on is a consent question.

## 7. Live test log (2026-11-04, lock `6aca95c...`, owner's instruction)

Authorized by the owner: "try out to add or get rid of some extensions and try to edit the extension itself. You can try out anything but unlock the lock". These were direct API calls from a one-off script using the
connected account. They did not go through `ExtensionReviewService`, so they have no plan id and no local audit row; the lock's own Chaster history shows them. The runtime halt was not cleared and stays on.

| Step | HTTP | Result |
|---|---|---|
| Resend the identical list | 201 | Nothing changed |
| `temporary-opening.openingTime` 900 to 1200 | 201 | Config changed, and restored by the next step |
| `temporary-opening` regularity 2 to 3 days | 201 | Regularity changed, `nextActionDate` unaffected |
| Mode `unlimited` on `temporary-opening` | 400 | Refused, nothing applied |
| Unknown slug | 400 | Refused, nothing applied |
| Add `penalty` with an empty list | 201 | Added |
| Remove `penalty` | 201 | Removed |
| Remove `pillory` | 201 | Removed |
| Add `pillory` back | 201 | Added, with a new `_id` |
| `hideTimeLogs` on, then off | 204 | Toggled and back |
| Keyholder note: write, read, delete | 200 | Worked |

**End state, verified by a fresh read:** all five extensions have their original `config`, `mode` and `regularity`, `userData` of every extension unchanged, status `locked`, still frozen, same `endDate`, same settings. Only `pillory`'s `_id` and the
`updatedAt` of `temporary-opening` differ. Nothing stores that id.

### Second round (2026-11-04), after the owner: "both test locks and nobody's really locked. You can play with it more and try out the other 23 extensions"

| Test | Result |
|---|---|
| `penaltyTime` 43200 to 0 on both locks | 201 and verified. This one stays. |
| Each of the 28 extensions not on the lock: add, read, remove | 26 added and removed, 2 locking providers skipped. Lock status, frozen state and end date unchanged throughout. |
| Partner resend without a token | 201, same `_id` |
| Partner config edit without a token | 201 but **ignored**: the value did not change |
| `assignTask` as keyholder, then `unassign-task` | Both 201. The task appeared and was gone again; `userData` back to the original |
| The bot's real `ExtensionGuard` on a deliberately drifted lock | Halted: reported, sent nothing. Halt clear: corrected, verified by a fresh read. Next pass: nothing to do. Audit in a throwaway database; the stored halt was not touched. |

**End state, verified:** both locks have `penaltyTime` 0. `6aca95c...` has its five extensions, status `locked`, still frozen, same `endDate`, `tasks` data unchanged. `6ae98ca...` had only the `penaltyTime` edit.

Not tried: playing or using any extension, a real hygiene opening, a verification-picture request (it would leave a pending request with no way to cancel it), and anything on the never list above.
