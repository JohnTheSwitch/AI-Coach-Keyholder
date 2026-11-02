# Phase F: proactive psychology and behavioural training — review of the blueprint (PROPOSAL, no code)

Status: review and gap analysis of the owner's Phase F blueprint (training inflation, wearer dossier, idea box, structured output). Nothing is
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
