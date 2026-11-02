# coaching_memory

v0.39 Phase F2 (`service.py`, table `wearer_dossier`, migration 036). A separate package because `memory_system/` is deliberately free of database access and logging.

Short notes about the user's own `behavior_pattern`, `stated_goal` and `quote`, written only through the validated structured-output sink (never from an
untrusted channel) after code-level filters (health, medical, injury, trauma, abuse, self-harm, suicide, distress; instruction-like text), with a
daily limit, a total cap and de-duplication. The 5 newest entries are rendered as fenced data (`<context>`) after every authoritative prompt part.
The user lists them with `memory` and deletes them with `forget <id>` / `forget all`. See docs/architecture/phase_f_proposal.md section 7.
