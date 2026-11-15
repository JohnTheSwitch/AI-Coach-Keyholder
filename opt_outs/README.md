# opt_outs

Gap fix G3 (owner, 2026-11-15): "the check-ins turning off after 24h shouldn't be automatic and should ask after 24h if I really wanna do it."

- While the wearer is locked (fails closed: not KNOWN to be unlocked counts as locked), `photo check-ins off`, `conditioning off` and
  `weekly review off` are not applied at once. The request waits 24 hours (`COOLING`); then the keyholder asks "Do you still want it off?"
  with "Yes, switch it off" / "Keep it on". Only a yes applies it; "Keep it on" or no answer within 24 hours leaves it on.
- Switching the same thing back on while a request waits withdraws it. Unlocked: everything applies at once, as before.
- Never delayed: the runtime halt, leaving, health, the emergency option, and `research off` (a privacy right that also asks for erasure).

Table `opt_out_requests` (migration 074), written only by `service.py`. Events: `opt_outs.requested`, `opt_outs.asked`, `opt_outs.decided`.
Buttons: scope `optout` through `system/dm_actions.OptOutDm`.
