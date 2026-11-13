# ai

Reference data for the AI's identity. Design: `docs/architecture/ai_identity_technical_design.md`.

- `identity_catalog.py`: the 15 personas a wearer chooses at onboarding (name, archetype, six style numbers).
- `persona_voices.py` (owner, 2026-11-13; roadmap slice 3b, "personas made real"): each persona's own voice, a few lines plus two example lines.
  Before, only the six numbers reached the model and one fixed voice guide covered everyone, so all fifteen sounded like one keyholder.
  **All fifteen are keyholders** (owner's decision); they differ in how they hold the key. The shared core in
  `conversation_engine/prompt_builder.py` `VOICE_GUIDE` always applies first: frank about arousal and denial, no assistant phrases, warm and
  direct on health or distress, suggestive but never explicit, tone only and never authority. `persona_voice_text()` adds the voice after it.

Switching (`application/service.py`): `persona` lists all fifteen and marks the wearer's; `persona <name>` explains that only the voice changes;
`persona <name> confirm` switches (`OnboardingService.change_identity`, after onboarding only). Rules, Trust, tokens and every other state are
untouched (test).
