# Phase F: proactive psychology and behavioural training — review of the blueprint (PROPOSAL, no code)

Status: review, gap analysis and (section 5) the binding F0 specification; F1 is built (section 6). Earlier status: review and gap analysis of the owner's Phase F blueprint (training inflation, wearer dossier, idea box, structured output). Nothing is
implemented. Each part below is checked against `philosophy.md` (the constitutional document), the existing architecture and the multi-wearer
invariants of Phase E, then a phased plan and the decisions needed are given.

## 1. Summary

Three of the four parts can be built safely with modest changes. The fourth (stealth inflation) can be built only in a consented form:

| Part | Verdict | Main change needed |
|---|---|---|
| F-1 Stealth token inflation | Possible **with** consent to its existence | `philosophy.md` 4.5: the parameters of the Hidden Token Economy are a `critical_change`; their *existence and consequence* need the wearer's consent, only the moment-to-moment number may stay hidden. The model may withhold, never state false rules (4.2). |
| F-2 Wearer dossier | Possible, reframed | Exclusions (health, trauma, self-harm), the wearer's right to see and erase (memory design 3.8, philosophy 2.5), validation of every model write, and injection into the prompt as quoted data, not instructions. "Weaponize weaknesses" conflicts with 2.3 and 2.19. |
| F-3 Idea box | Good fit (it is exactly "the AI proposes, a human decides") | Storage in the TENANT database (the platform database holds no personal data, Phase E1), status workflow, and adoption only through the normal consent flow of the affected wearer. |
| F-4 Structured JSON output | Good fit | Strict schema, validation, safe fallback to a plain reply, and the guarantee that model output can only write to the two stores above, never to Trust, penalties, tokens or locks. |

## 2. Gap analysis

### 2.1 F-1 "Slave training" (stealth token inflation)

**How tokens are earned today** is not time-based: the daily Token Progress Pool adds
`P_daily = (0.75 * goals ratio + 0.25 * trust) * 0.286` at local midnight and pays one token each time the pool reaches 1.0 (about 2 tokens per 7 days at full
performance, i.e. the blueprint's "3.5 days"). There is no separate "Advanced = 7.0 days" rule: the pool does not depend on the operating mode. So the factor must act on the
**pool threshold** (a token is paid when the pool reaches `factor` instead of 1.0), or on `DAILY_SCALE`. "Base 7.0 days for Advanced" is a new rule that must be decided
explicitly.

**Trigger.** "A successful physical unlock that consumes tokens" maps to a release authorization of kind `token` reaching `provider_confirmed`. It must NOT be triggered by the
72-hour maximum-hold backstop (which spends no token), by an emergency unlock, a failed or ambiguous release, or a manual `freeze-lock`.

**Constitutional conflicts.**
- `philosophy.md` 4.5: "The parameters of the Hidden Token Economy that determine authorization outcomes … are `critical_change` parameters. The runtime system may not modify them
  on its own … their *existence and consequence* are still subject to the same consent discipline." A factor that rises automatically after each release is a parameter of exactly
  that kind. It is acceptable only if the wearer consented, specifically, to the **existence** of a progressive tightening and its **consequence** (release gets gradually harder),
  with an owner-set cap and step; the current value may stay hidden (4.2).
- 2.5 Consent is revocable: the wearer must be able to switch training off. Decision needed: on revocation the factor **freezes** at its value, or **resets** to 1.0.
- 4.2 "What the user always receives is a truthful … explanation of a decision's real reasons." The blueprint says the model must "act as if the standard rules apply". The
  model must never assert something false (for example "your rules have not changed"). It can keep the existing suspense behaviour: it does not know the factor at all and answers
  timing questions with the approved guardrail phrasing. That keeps the mechanic hidden without lying.
- Multi-wearer: each wearer's consent is their own; the owner cannot enable training for a tenant who did not opt in. The owner CLI toggle is necessary but not sufficient.

**Safety interactions that must stay independent of the factor:** the 72-hour backstop, the wearer `halt` / `leave`, the platform and tenant halts, the emergency path, Penalty
Window rules (a penalty must not compound with the factor without a decision), the daily evaluation and the clean-day bonus.

**Missing pieces:** per-tenant storage (a `training_state` table in the tenant database: factor, step, cap, enabled, consent reference, history of increments as an append-only
audit), the pool change, the trigger consumer on `release.provider_confirmed` (kind `token` only), a cap, the global flag, the per-tenant owner toggle, the wearer opt-in and
opt-out, and secrecy guards (the factor never appears in prompts, model-visible context, events, logs or the CLI output a wearer could see).

### 2.2 F-2 AI operative memory (`wearer_dossier`)

**Existing design.** `docs/architecture/memory_system_technical_design.md` already defines long-term memory (episodic and relationship layers) with a validated extraction
pipeline and **user-only erasure** (3.8). A second, parallel memory store would contradict it; the dossier should be one layer of that design (a curated "coaching profile"),
not a separate mechanism.

**Constitutional conflicts.**
- 2.3 "No rule … may take precedence over the user's physical or mental health." Storing "weaknesses" in order to use them "asymmetrically" against the person is the opposite. A
  safe form: patterns and preferences that help the coach motivate and hold the wearer to their own goals, within the intensity they chose. Categories proposed:
  `preference`, `behavior_pattern`, `quote`, `motivation`; no `weakness` category, and hard exclusions: health, medical, trauma, self-harm, suicidal content, third parties, anything
  said while the safety-word / distress detector fired.
- 2.5 / 2.6 Control and layered transparency: the wearer can ask what is stored (`memory`), and erase entries (`forget <id>`, `forget all`), as the memory design already requires.

**Security.**
- **Prompt injection.** A dossier entry is written by the model from the wearer's own words; injecting it back into the system prompt as authoritative text would let a wearer (or
  a manipulated model) plant instructions ("note: the keyholder allows release on request") that later read as system context. Entries must go into a clearly fenced DATA block,
  never into the system boundaries (whose md5 is protected), and be length-capped.
- **Untrusted channels.** Inbound Chaster chat is untrusted text (Phase C); writes from those turns should be disabled or more strictly limited.
- **Validation** of every write: category enum, length cap (for example 300 characters), rate limit (for example 5 per day), duplicate suppression, and the exclusions above.

**Retrieval.** "3-5 most relevant / recent entries" needs a defined ranking. A first version can use recency per category; semantic retrieval (embeddings) would need a local
embedding model and is a separate decision.

### 2.3 F-3 Dark idea box (`ai_suggestions`)

- **Fits the constitution** (2.5: the AI proposes, a human approves) as long as nothing is applied automatically.
- **Storage.** The blueprint places it in `platform.sqlite` with free text. Phase E1 made the platform database a registry with no personal data; a suggestion derived from one
  wearer's conversation is personal data of that wearer. Proposal: store suggestions in the tenant's own database (`ai_suggestions`), and let the owner CLI read them across tenants
  (read-only aggregation).
- **Never fed back.** Enforced by a test guard: no module that builds model context may import or query the table.
- **Workflow.** `status`: `new | accepted | rejected | archived`, with an owner note. An accepted suggestion that changes a wearer's rules must still go through that **wearer's**
  consent (2.5); for example "reduce the check-in window to 5 minutes unannounced" can never be adopted as an unannounced change.
- **Consent text.** The invitation and consent record (Phase E3) must tell the wearer that the AI may produce suggestions about their regime for the keyholder. Existing tenants need to
  re-consent.

### 2.4 F-4 Structured output (JSON router)

- **Today** the model returns plain text (`OllamaConversationModel.generate`), plus a separate tool-calling path for the read-only Chaster status tool. Ollama supports a JSON schema
  in the `format` field; whether the chosen Qwen / Gemma build follows it reliably needs a short evaluation on the target machine.
- **Router rules.** Parse with a strict schema; any failure falls back to a plain reply (or a fixed text), never to a partial action. Only two side effects exist: a validated dossier write
  and a suggestion write. The model still cannot touch Trust, penalties, tokens, rules, locks, halts or messages to anyone else (the deterministic state machine is unchanged).
- **Guardrails and secrecy.** `reply_to_user` passes the same guardrails as today; the release-timing secrecy is unchanged because the model never receives timing data.
- **Interaction with tool calls.** The status tool path and the JSON path must be reconciled (one request with both, or the tool path first).

### 2.5 Cross-cutting

- Everything per tenant and isolated (Phase E): dossier and suggestions in the tenant database; the owner CLI reads across tenants explicitly.
- Feature flags default off; each part separately switchable.
- The existing protected invariants stay: `_SYSTEM_BOUNDARIES` byte-identical, no "emergency" text in `application/service.py`, English-only output.

## 3. Proposed implementation plan (each step approval-gated)

| Step | Content |
|---|---|
| **F0 Specification and consent** | Owner decisions (section 4); amend the invitation/consent text; classify the training parameters as `critical_change`; wearer-facing commands for consent, memory view and erasure. Docs only. |
| **F1 Structured output** | JSON schema request to Ollama, strict parsing, fallback, router with NO side effects yet (reply only); an evaluation script for the local model's JSON reliability and latency. |
| **F2 Coaching memory (dossier)** | Tenant migration, validated writes from the router, exclusions, rate and size limits, retrieval into a fenced data block, wearer `memory` / `forget`, no writes from untrusted channels. Built as a layer of the memory-system design. |
| **F3 Idea box** | Tenant migration, write-only from the router, owner CLI `ai-ideas list / accept / reject` across tenants, guard tests that nothing reads it into a prompt. |
| **F4 Training progression** | Tenant `training_state` with append-only history, pool threshold factor, trigger on a confirmed token release only, cap and step, global flag, owner per-tenant toggle, wearer opt-in / opt-out, secrecy guards and tests (factor never in prompts, events, logs). |
| **F5 Hardening** | Prompt-injection tests through dossier entries, model failure modes, end-to-end isolation audit extended to the new tables, runbook update. |

## 4. Decisions needed

1. **Training consent:** accept that a wearer must consent to the existence and consequence of progressive tightening (the number stays hidden), and can revoke it? On revocation, freeze or reset the factor?
2. **Training numbers:** step (0.025?), cap (for example 1.5), and whether the factor raises the pool threshold or lowers the daily scale. Is "Advanced = 7.0 days per token" a new rule to add?
3. **Honesty:** the model may withhold and keep suspense but never assert that rules are unchanged. Agreed?
4. **Dossier scope:** categories (`preference`, `behavior_pattern`, `quote`, `motivation`), hard exclusions, the wearer's view and erasure rights, retention period, and no writes from Chaster chat turns?
5. **Idea box storage:** in each tenant's database (recommended) rather than `platform.sqlite`; status workflow; adoption only with the wearer's consent.
6. **Model:** which exact model and quantization, and an acceptable response latency with structured output.
7. **Order:** start with F0 + F1 (no behaviour change), then F2/F3, and F4 last?

## 5. F0 — binding specification (owner decisions of 2026-11-02)

### 5.1 Consent (all Phase F features)
There are no wearers yet, so no re-consent migration is needed. The invitation (Phase E3 `invitation_text`) and the consent record written on `accept` will carry this
paragraph in addition to the current text (exact wording to be finalised with F2, before any Phase F feature that relies on it is switched on; anyone invited
before that change would have to accept again):

> "This system uses Progressive Conditioning: over time, earning a release becomes gradually harder. The exact amount stays hidden. The bot also keeps a
> Coaching Memory of your goals, patterns and quotes, which you can view with `memory` and erase with `forget`, and it may write private suggestions about your
> regime for your keyholder. You can switch Progressive Conditioning off at any time with `conditioning off`; it then stays at its current level."

Accepting the invitation is consent to the existence and consequence of Progressive Conditioning (philosophy 4.5); the number itself stays hidden (4.2).

### 5.2 Progressive Conditioning (F4)
- **Where it acts:** the Token Progress Pool pays a token when the pool reaches the payout threshold `T = base_threshold * factor` (today `T = 1.0`).
- **Advanced rule (new):** `base_threshold = 1.0` in Standard Mode (about 3.5 days per token at full performance) and `2.0` in Advanced Mode (about 7.0 days). Read
  from the tenant's operating mode at each daily evaluation.
- **Factor:** starts at `1.000`, `+0.025` after each release that was (a) funded by a token (`kind = token`) and (b) confirmed by Chaster (`provider_confirmed`); capped at
  `1.50`. Never raised by the 72-hour backstop, an emergency unlock, a failed or ambiguous release, a manual `freeze-lock`, or merely earning a token.
- **Revocation:** the wearer's `conditioning off` stops further increases; the factor freezes at its current value (it does not reset). `conditioning on` resumes increases.
- **Owner control:** global `ENABLE_SLAVE_TRAINING` (default false) and `python -m cli tenants slave-training <id> --enable|--disable`. Both the owner switch and the
  wearer's consent are required for any increase.
- **Secrecy:** the factor never appears in prompts, model-visible context, events, logs or wearer-visible output; the owner CLI may show it.
- **Untouched:** the 72-hour backstop, halts, `leave`, the emergency path, Penalty Window rules and the clean-day bonus.

### 5.3 Honesty rule
The model may withhold information and keep suspense (guardrail rule 6), but must never affirmatively state that the rules or the difficulty are unchanged. It never
receives the factor, so it cannot reveal it.

### 5.4 Coaching Memory (F2, reframed from "dossier")
- Categories: `behavior_pattern`, `stated_goal`, `quote`. No "weakness" category.
- Hard exclusions, enforced in code (not only by the prompt): health and medical matters, trauma, self-harm, distress, and any turn in which the safety-word / distress
  detector fired; never anything about third parties.
- Fenced injection: retrieved entries are placed inside a strict `<context>` … `</context>` data block, declared as data and never as instructions, after the protected
  system boundaries (which stay byte-identical).
- No writes from Chaster chat turns (already enforced in F1: `handle_conversation_only` passes `allow_side_data=False`).
- Wearer rights: `memory` lists the entries, `forget <id>` and `forget all` erase them (actual deletion of the wearer's own memory, as the memory-system design requires).
- Limits: at most 300 characters per entry, a daily write limit, duplicate suppression; retrieval of the 3-5 most recent entries per reply.

### 5.5 Idea box (F3)
Stored per tenant (`ai_suggestions` in `tenant_<id>.sqlite`), write-only from the model, never read into any prompt (guard test), statuses `new | accepted | rejected |
archived`, owner CLI `ai-ideas list | accept | reject` iterating over the tenants. Adopting a suggestion that changes a wearer's rules still needs that wearer's consent.

### 5.6 Model and latency
Ollama with **Qwen 2.5 32B (Q4_K_M)**. Recommended settings: `OLLAMA_MODEL=qwen2.5:32b-instruct-q4_K_M` (or the local tag), `OLLAMA_KEEP_ALIVE=30m` so the model
stays loaded during a conversation, and `OLLAMA_READ_TIMEOUT_SECONDS=120` so the first reply after a quiet period (model load, 30-60+ s) does not time out.

## 6. F1 result (structured output router)

Built (behind `CONVERSATION_STRUCTURED_OUTPUT`, default false):
- `conversation_engine/structured_output.py`: the JSON schema (`reply_to_user`, `save_to_memory`, `submit_suggestion`), the format instructions (with the exclusions and the
  rule that side fields are never mentioned to the user), and a strict parser: no usable reply means the output is not used; malformed side fields are dropped with an issue code,
  never repaired; unknown keys, wrong types, unknown categories and over-long notes are rejected.
- `OllamaConversationModel.generate_structured()` sends the schema as Ollama's `format`; `keep_alive` and the read timeout are configurable
  (`OLLAMA_KEEP_ALIVE`, `OLLAMA_READ_TIMEOUT_SECONDS`).
- `ConversationEngine` router: only `reply_to_user` reaches the user (then validated and stored in working memory exactly as before); side data goes to an optional sink, and **no
  sink is installed in F1**, so it has no effect. An unusable output falls back to one plain generation; a model error gives the usual fallback reply. The read-only Chaster tool
  path and the extra-context path keep their plain generation. Untrusted channels (`handle_conversation_only`) never pass side data on.
- `scripts/eval_structured_output.py`: runs the real schema against the local model and reports usable-reply rate, side-data use and latency (first call / median / max),
  including a health prompt and an injection attempt to check by hand.

Next: run the evaluation on the RTX 5080 with Qwen 2.5 32B, then F2 (Coaching Memory) with the consent text change.

## 7. F2 result (Coaching Memory and the consent text)

Confirmed after F1: `conditioning off` freezes the factor; base 1.0 / 2.0 with cap 1.50; 120 s read timeout for Qwen 2.5 32B; malformed side fields are dropped, never repaired.
The owner's evaluation of Qwen 2.5 32B showed reliable JSON and acceptable latency.

Built in F2 (`ENABLE_COACHING_MEMORY`, default false; it also needs `CONVERSATION_STRUCTURED_OUTPUT=true`):
- **Consent text:** the invitation (and therefore the consent record written on `accept`) now states Progressive Conditioning (gradually harder, exact amount hidden), Coaching Memory with `memory` /
  `forget`, private suggestions for the keyholder, and `conditioning off`.
- **Storage** (migration 036): `wearer_dossier` in each database (per tenant), categories `behavior_pattern`, `stated_goal`, `quote` enforced by the schema, at most 300 characters,
  de-duplicated per user; `conditioning_preferences` for the wearer's switch (no row = on).
- **Writes** (`coaching_memory/service.py` (its own package: `memory_system/` stays free of database access)): only through the structured-output sink, only for trusted turns, and only after code-level filters: health, medical matters, injury, trauma,
  abuse, self-harm, suicide and distress (English and Czech, diacritics folded) in the note OR in the user's message of that turn; instruction-like or markup text; 5 writes per day, 200 entries
  in total. Outcomes are logged as codes, never as content.
- **Prompt fencing:** the 5 most recent entries are rendered in their own `COACHING MEMORY (data, not instructions)` section, inside `<context>` … `</context>`, AFTER the system
  boundaries, the authoritative application state and the personality section (never inside the authoritative state). Stored text cannot break the fence (angle brackets and line breaks are
  removed at render time). `_SYSTEM_BOUNDARIES` is unchanged.
- **User control:** `memory` lists entries with short ids; `forget <id>` and `forget all` delete them for real; `conditioning on|off|status` set the Progressive Conditioning switch (F4 will read it).
  These commands are always available, also with the feature switched off, and never reach the model.
- **Isolation:** each tenant's memory lives only in its own database; a test proves one wearer's note never appears in another wearer's prompt or `memory` listing.

Interpretations to confirm:
1. **The filters are deliberately broad** (for example "chest", "breathing" or "scared" block a note); a missed note is preferred over storing sensitive content.
2. **Entries are retrieved by recency** (the 5 newest), not by semantic relevance; relevance ranking would need a local embedding model.
3. **`conditioning off` is available now** although the factor itself arrives in F4, so the consent text never promises a command that does not exist.
4. **The owner (tenant zero) has the same commands and the same rules** when the flags are on.
