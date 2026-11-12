# coaching_memory

v0.39 Phase F2 (`service.py`, table `wearer_dossier`, migration 036). A separate package because `memory_system/` is deliberately free of database access and logging.

Short notes about the user's own `behavior_pattern`, `stated_goal` and `quote`, written only through the validated structured-output sink (never from an
untrusted channel) after code-level filters (health, medical, injury, trauma, abuse, self-harm, suicide, distress; instruction-like text), with a
daily limit, a total cap and de-duplication. The 5 newest entries are rendered as fenced data (`<context>`) after every authoritative prompt part.
The user lists them with `memory` and deletes them with `forget <id>` / `forget all`. See docs/architecture/phase_f_proposal.md section 7.

## Notes carry their day (owner, 2026-11-12)

The owner's fear: a one-off ("I got burned this Wednesday") talked about for months. Each note reaches the prompt with the day it was saved
("saved Tue 10 Nov, 2 days ago"), the preamble says relative words mean that day and anything temporary is over unless the user raises it again, and
a note older than its category's age is no longer shown (`MAX_AGE` in `conversation_engine/context_providers/coaching_memory_provider.py`: quotes
7 days, patterns 21, stated goals 60). The note stays stored; the user still lists and erases it. Notes about other people are allowed when they
concern the user (owner's decision), and the model is told to write dates instead of "today".
