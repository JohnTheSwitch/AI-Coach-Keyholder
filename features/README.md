# features

Per-wearer feature switches (owner, 2026-11-16): "we should make a switch for working out and for all the other things that different users
could prefer to be off"; the relock switch covers only "the relock after token spending, all other should be mandatory".

Everything is on unless the wearer switched it off (no row = on, so nothing changed for the owner). In the DM: `features` lists them,
`<feature> off` / `<feature> on` changes one. While locked a switch-off waits 24 hours and is asked about (`opt_outs/`, key
`feature:<key>`); on is immediate and withdraws a waiting switch-off. A read that fails counts as on.

| Key | He types | Off means |
|---|---|---|
| `workouts` | workouts | no training plan sessions, no workout tasks |
| `erotic_tasks` | erotic tasks | no erotic / BDSM daily task (a training day's session still comes) |
| `protocol` | protocol | the protocol ladder neither ticks nor answers messages |
| `morning_report` | morning report | no morning report (never a miss) |
| `lines` | lines | no lines for confirmed incidents (the incident still counts; release lines to earn a release stay) |
| `temptations` | temptations | no temptation offers |
| `release_relock` | release relock | no 2-hour release window, so no consequence for relocking later after a token release |

Never switchable: check-ins, every other detector (hygiene, exemptions, relock pictures, lock claims...), Trust, Penalty Windows, tokens,
health and the emergency option.

Table `feature_switches` (migration 086), written only by `service.py`. Event: `features.switched`. Read with `get_feature_on` /
`get_feature_on_in_transaction`; the wiring hands those to the other modules as callables.
