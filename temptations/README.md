# temptations

**Question it answers:** is a temptation on offer now, and what did accepting ones cost his token progress on a given day?

Owner, 2026-11-15/16 (story idea P5, autonomous queue B7): about once a week (`DAILY_CHANCE` per day), at a random moment in free time
(never quiet hours, work, a wear exemption, a vacation or a Penalty Window), the bot offers: 15 minutes of edging with the Deep Diver in,
right to the edge as often as he likes, never cumming. The exact cost (`COST`, 0.05 of token progress) is never shown: the offer says what it
probably means for his next token, from his average progress over the last 14 days. About 1 in 4 is free (`FREE_SHARE`). **Accept** (button,
scope `temptation`) within `OFFER_FOR` (1 hour) = the cost is taken off that day's pool progress at the daily evaluation (never below 0);
**ignore** = nothing. A slip is told to the bot and goes the existing confession path; nothing here judges it. Honour-based; the bot-run
version waits for the Deep Diver control (P6).

- **Owns:** `temptation_offers` (migration 084).
- **API:** `get_temptation_costs(core, user_id, lo, hi)` (read, a Decimal), `Temptations.tick(now)`, `Temptations.press(...)`.

## Another wearer (owner, 2026-11-17)

`Ports.offer_text`: for another wearer the offer is one of their own approved temptations that fits their gear (`custom_tasks/`, kind `temptation`), stored on the offer (`temptation_offers.text`, migration 094); none fitting means no offer. The owner's fixed offer is unchanged.

## Caged edging, run by the bot (owner, 2026-11-25/28)

The offer says it plainly: caged edging, the Deep Diver in, hands only around the cage. After **Accept**, "It's in: run it" (`temptation:toy:<id>`, within an hour of accepting) runs the Deep Diver for 15 minutes on the `tease` profile (toy_control), with Pause / Continue / Stop; the toy's own button always works. Wired in bot/discord_bot.py from the obedience toy (one session at a time). Honour-based as before: a slip is told.
