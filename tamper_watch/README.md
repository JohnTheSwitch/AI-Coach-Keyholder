# tamper_watch

Owner, 2026-11-16: "the worst thing is ... that I can always go and unlock myself through the system". He is the wearer and the operator,
so changing the system cannot be prevented; the tamper watch makes it visible.

At every start (60 s after the bot is up) it checks, and raises each finding on the detectors' one path (`SignalKind.TAMPERING`: asked to
explain, judged on the explanation, unexplained = `SYSTEM_TAMPERING`, a moderate Incident in the Honesty domain; silence is the usual
warning; 10 lines "I never change my keyholder's system behind its back."):

- **a dirty start**: the running code has uncommitted changes (tests, `docs/` and `.md` files are ignored: they never change behaviour);
- **commits not by Claude** since the last start (no `Co-Authored-By: Claude` trailer);
- **history edited while the bot was off**: a SHA-256 fingerprint of every domain event and every piece of Trust evidence up to a row id,
  taken every 15 minutes and at each start, no longer matches (rows written later by the bot itself never count).

Not seen, deliberately said here: an edit while the bot runs and retakes its fingerprint, a change to `.env` (the settings watch covers the
time settings and the halt), other state tables, and anything outside this repository. The first start only records.

Table `tamper_watch_state` (migration 087, which also widens the `infraction_signals` kind CHECK), written only by `service.py`.
Wired in `bot/discord_bot.py` (owner only; additional wearers cannot reach the code).

## A copy run by someone else (owner, 2026-11-16)

Someone running their own copy pulls the owner's commits, and those carry no Claude line when he commits by hand. Set
`TAMPER_TRUSTED_REF` to the git ref of the upstream they follow (e.g. `upstream/main`): a commit that is part of it
(`git merge-base --is-ancestor`) is never asked about, while their own commits and a dirty start still are. Pushing upstream changes
nothing for them until they pull. Wearers inside the owner's bot (multi-wearer) are never checked: the watch covers only the owner.

Leave it unset on the owner's own install: trusting his own remote would let a change through by pushing it and pulling it back.
