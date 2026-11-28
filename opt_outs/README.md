# opt_outs

Gap fix G3 (owner, 2026-11-15): "the check-ins turning off after 24h shouldn't be automatic and should ask after 24h if I really wanna do it."

- While the wearer is locked (fails closed: not KNOWN to be unlocked counts as locked), `photo check-ins off`, `conditioning off` and
  `weekly review off` are not applied at once. The request waits 24 hours (`COOLING`); then the keyholder asks "Do you still want it off?"
  with "Yes, switch it off" / "Keep it on". Only a yes applies it; "Keep it on" or no answer within 24 hours leaves it on.
- Owner, 2026-11-16 ("gear remove shouldn't work instantly"): `gear remove <piece>` while locked waits the same way (key `gear:<piece>`,
  "Yes, remove it" / "Keep it"); until then tasks may still use the piece. `gear remove <piece> broken` / `lost` / `never had` removes it at
  once and is noted in the decision log. `gear add <piece>` withdraws a waiting removal. Adding is always immediate.
- Owner, 2026-11-16: the per-wearer feature switches (`features/`) wait the same way (key `feature:<key>`).
- Switching the same thing back on while a request waits withdraws it. Unlocked: everything applies at once, as before.
- Never delayed: the runtime halt, leaving, health, the emergency option, and `research off` (a privacy right that also asks for erasure).

Table `opt_out_requests` (migration 074), written only by `service.py`. Events: `opt_outs.requested`, `opt_outs.asked`, `opt_outs.decided`.
Buttons: scope `optout` through `system/dm_actions.OptOutDm`.

**Schedule changes (owner, 2026-11-25).** A `schedule` change that ends a school day earlier (more free time, an earlier possible release)
waits COOLING while locked and is asked about like a switch-off (key `schedule:<days>=<HH:MM|default>`, `register_schedule`). A later end
is always at once.
