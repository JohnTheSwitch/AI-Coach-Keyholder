# idea_box

v0.39 Phase F3 (`service.py`, table `ai_suggestions`, migration 037; `ENABLE_AI_SUGGESTIONS`, default off, needs `CONVERSATION_STRUCTURED_OUTPUT=true`).

A one-way channel from the model to the keyholder. The model may add a `submit_suggestion` (type `rule_tweak`, `new_punishment` or `system_flaw`, at most
500 characters) to its structured reply; the engine's sink stores it in the CURRENT database (each tenant has its own), only for trusted turns, and never when the
suggestion or the user's message of that turn touches health, medical matters, trauma, self-harm or distress. 5 per user per day, duplicates suppressed.

**Write-only for the model.** Nothing that builds model context imports this package or queries the table; tests guard it statically and show end to end that a
submitted idea never appears in a later prompt (while Coaching Memory still does).

**Owner review.** `python -m cli ai-ideas list [--status new|accepted|rejected|archived|all] [--tenant <id>] --owner-id <you>` reads every database (the owner's
and, with multi-wearer on, every live tenant's). `ai-ideas accept|reject|archive <tenant>:<id> [--note ...] --owner-id <you>` records a verdict on one suggestion
and changes nothing else: adopting an idea is a separate change that needs the affected wearer's consent.
