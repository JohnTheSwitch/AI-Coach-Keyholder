# lines

Brat taming B2 (owner, 2026-11-14; migration 066). Every confirmed incident gets lines:

- **20** for a Warning-level incident (`penalty_warning.recorded`), **40** for one that opens or extends a Penalty Window
  (`extension.decision_recorded`, which now carries `rule_group_id`). One assignment per incident; only incidents confirmed after this
  was deployed (the outbox never replays published events).
- The sentence matches the rule broken (`SENTENCES`; anything new gets "I follow the rules I agreed to.").
- The wearer types them in the bot DM. **Code counts them, no model:** a line counts when it is the sentence exactly, ignoring case, spacing,
  a leading number ("12." / "12)") and the closing full stop. Several per message, across messages; extra lines go to the next open
  assignment, wrong lines are named and don't count. A message with no owed sentence in it is an ordinary message.
- **What waits while lines are open:** the next planned (discretionary) hygiene break (`exemptions/service.py`; the card offers only "It's a
  health need" and "Not now") and, as release debt (roadmap B4), spending a token on a release (`ReleaseGates.release_debt`, a hard veto like
  a Penalty Window; the token is kept). **A health need never waits** (philosophy 3.9), and a read failure holds nothing.

Table: `line_assignments` (written only by `service.py`). Events: `lines.assigned`, `lines.completed`. The single wearer only (the owner's
notice user); tenants are not wired.
