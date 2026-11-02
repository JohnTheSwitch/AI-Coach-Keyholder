# iron_contract

v0.39 Phase G6 (`guard.py`, `vacation.py`; migration 041). Specification: [`docs/architecture/phase_g_proposal.md`](../docs/architecture/phase_g_proposal.md)
section 7.

**The Iron Contract (`guard.py`).** While the wearer is locked:
- `mode request standard` and `mode confirm` toward Standard (lowering the difficulty) are refused through `@requires_unlocked` in
  `application/service.py`;
- the model sees `[STATE: LOCKED]` in the application state (`conversation_engine/context_providers/iron_contract_provider.py`), and guardrail rule 7
  tells it not to negotiate deadlines, extensions or excuses. Rule 7 is never cruel, treats real life (illness, family crisis, exams, work) as real, and
  keeps the safety exception of rule 3 on top (philosophy 2.3).

**Lock status.** Locked when the latest Chaster observation of the observed wearer OR the wearer's own latest report says locked. Unlocked when one says
unlocked and neither says locked. Otherwise unknown. A guard needing "unlocked" refuses an unknown status as well, and tells the wearer to report the lock
state.

**Consent.** In a tenant database the difficulty gate and the locked stance apply only after the wearer accepted an invitation containing
`IRON_CONTRACT_DISCLOSURE`; tenant zero is the owner's own database. Wearers who accepted an older invitation are not bound by them.

**Never blocked** (philosophy 2.5: consent is revocable at any time, and the invitation promises it): `halt`, `resume`, `leave`, `conditioning off`,
`research off`, `forget all`, `vacation end`, the emergency path, lock reports, help and status. A test pins the exact set of guarded handlers.

**Vacations (`vacation.py`).** `vacation schedule <first day> <last day>` (local dates, inclusive, at most 14 days, starting at most 90 days ahead, one at
a time) and `vacation cancel` (before it starts) work only while unlocked, everywhere. `vacation end` works at any time. A vacation starting today begins
at that moment, never at the past midnight. While it is in force:
- no check-in is sent, and open ones become `skipped`, never a miss;
- a day lying entirely inside it is recorded with `daily_evaluations.paused = 1`: no pool progress, no token, and the clean-day streak neither grows nor
  breaks. Such days are not behavioural telemetry.
- a confirmed release cannot raise the Progressive Conditioning factor; it is audited once in `conditioning_paused_releases`.

Penalty windows, incidents, the token ledger, the rules, the release engine and every safety path are untouched.
