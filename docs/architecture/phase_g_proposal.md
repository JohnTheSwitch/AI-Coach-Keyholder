# Phase G: Behavioral Telemetry and the opt-in Hive-Mind

Status: G0 specification (binding); G1 (local derived telemetry), G2 (runtime buffer) and G3 (owner analysis) built. G4–G6 are specified here and not built yet.

Owner decisions (2026-11-02): the three-layer consent model; the hidden factor is exported only as a 0.10 band; retention 180 days locally and 365 days at the
receiver; `forget all` purges local telemetry; revoking `research on` or running `forget all` queues an erase at the receiver; `bot_mood` is derived from engine
state, never added to the structured-output schema; the receiver is FastAPI and must treat every sender as untrusted (other community instances will send).

## 1. Principles

| Rule | Source | Consequence |
|---|---|---|
| Consent is specific and revocable | `philosophy.md` 2.5 | Three layers (section 2). Export needs the wearer's own `research on`; the owner's flag alone is never enough. |
| Transparency | 2.6 | The wearer can see what is recorded (`telemetry`) and erase it (`forget all`). |
| The hidden token economy stays hidden | 4.2, 4.5 | The conditioning factor is stored locally for the owner only. The wearer's view never shows it; export (G4) only as a band of 0.10. Nothing telemetry-related ever reaches a prompt. |
| Health first | 2.3 | The analysis (G3) is a behavioural summary, not a diagnosis. Every recommendation that touches the token economy is labelled `critical_change` (needs the wearer's consent). |
| No personal data in the platform database | Phase E1 | Telemetry, watermarks and (G4) the export queue live in each tenant's own database. |
| The live reply is never slowed | owner priority | No telemetry code runs in the reply path in G1. G2 adds at most one O(1) in-memory append (section 4). |

## 2. Consent layers

1. **Local derived telemetry (G1)** and **2. the owner's analysis (G3)** are covered by the invitation disclosure ("behavioral telemetry"). Accepting the
   invitation records the shown text in the tenant's `consent_log`. The collector of a tenant database runs only when that record contains the disclosure, so a
   wearer who accepted an older text is not recorded until they accept a text that discloses it. Tenant zero is the owner's own database and needs no invitation.
3. **Export (G4)**: off unless BOTH `ENABLE_RESEARCH_TELEMETRY=true` (owner) AND the wearer's own `research on` (default off, revocable). Revoking it stops sending
   at once and queues an erase request for the receiver.

## 3. Local table and metric dictionary (G1)

Migration 039: `behavioral_telemetry` (id, user_id, occurred_at, event_type, metrics JSON, source_key UNIQUE, created_at, synced_at) and `telemetry_watermarks`
(source, position_at, position_id). `synced_at` stays NULL until G4. `metrics` holds numbers, booleans and short enum strings only, never free text (no check-in
response, no message, no prompt).

| event_type | Source (read-only) | metrics |
|---|---|---|
| `response_delay` | `check_ins` in a concluded status (answered, answered_uncertain, insufficient, missed) | level, slot_index, status, verdict, delay_seconds (responded − sent, null when missed), window_seconds (deadline − sent) |
| `token_burn` | `token_ledger` spends | amount, balance_after, hours_since_last_earn |
| `inflation_resilience` | `training_increments` | outcome, training_inflation_factor (after), factor_before, days_since_previous_increment, progress_avg_7d, clean_days_7d (from the 7 evaluated days before) |
| `llm_generation` (G2) | runtime buffer, one per model turn | latency_ms, path (plain, structured, tools, context), outcome (ok, generation_failed, validation_failed), structured_status (ok, side_dropped, unparseable, or null), trusted |
| `conversation_gap` (G2) | runtime buffer | seconds_since_last_reply: from the end of the bot's previous reply (received time + latency) to the wearer's next message |
| `daily_summary` | `daily_evaluations` | local_date, goals_assigned, goals_met, check_ins_due, check_ins_ok, violations, clean, streak_after, tokens_earned, progress, pool_after, trust_used, training_inflation_factor (factor in force when the day was evaluated), operating_mode, bot_mood |

`bot_mood` is derived, not generated: `strict` while a penalty window is active or frozen or any Trust domain is under supervision; otherwise `demanding` after a
day that was not clean, `approving` after a clean day with a streak of at least 3, else `neutral`. It is the engine's stance at collection time (within one
periodic cycle of the evaluation).

Retention: rows whose `occurred_at` is older than 180 days are deleted (checked once per day). The collector never derives facts older than that.

## 4. The hook: why the live reply is not touched

- **Tier A (G1, built).** `TelemetryCollector.run(now)` is the last step of the periodic cycle (`system/periodic.py`), inside the cycle lease but in its own
  `try`, so a telemetry failure is logged and never affects check-ins, the daily evaluation, the release tick or execution. Each source has a composite
  watermark `(updated_at or created_at, id)`; each cycle reads at most 500 rows per source and inserts with `INSERT OR IGNORE` on `source_key`, so a re-run, a
  restart or two instances never duplicate a row. The conversation engine and the application's reply path do not import or call it.
- **Tier B (outbox consumers).** Not used: an event with no consumer is lost, and a consumer that raises affects event processing. Every G1 metric is already in
  a table.
- **Tier C (G2, built).** Only for facts that exist nowhere but the reply path (LLM latency, parse failures, the wearer's reply latency): a `TelemetrySink` that does a
  single `deque.append` onto a bounded `collections.deque(maxlen=N)` (no I/O, no SQLite, no await, never raises), drained into the tenant database by the periodic
  cycle. A crash loses at most N samples (accepted). Built as `behavioral_telemetry.runtime.RuntimeTelemetryBuffer` (N = 2000), handed to
  `ConversationEngine(runtime_sink=...)` by injection. The engine measures with `time.monotonic` and calls the sink once per model turn inside
  `_observe`, which catches everything (the first failure is logged once); even a failing clock only disables the sample. The collector drains the
  buffer first, in ONE transaction; without consent the samples are discarded; `forget all` marks the user in the buffer before deleting rows, so
  undrained samples taken earlier are dropped. Proactive pings (check-ins) are already measured exactly by `response_delay`.
- The periodic cycle only runs when at least one periodic feature is enabled; without one there is nothing to derive (check-ins, the ledger and daily
  evaluations are all produced by that cycle).

## 5. Wearer controls (G1)

- `telemetry`: whether recording is active for this user, the number of records per category, the oldest record and the retention. Never a factor value.
- `forget all`: erases the Coaching Memory AND all of the user's telemetry. The watermarks stay, so erased facts are not derived again. (From G4 it also queues
  the erase request for the receiver.)

## 6. Later steps (specified, not built)

**G3 (built)** `python -m cli research analyze <tenant_id> [--days 30] [--output FILE] [--allow-while-live] [--timeout 600] --owner-id <you>`
(`behavioral_telemetry/analysis.py`, `cli/main.py`):
- Owner only (`TenantAdmin.authorize`, audited refusal). `--days` 1..180.
- Live guard: refuses while the bot runs, unless `--allow-while-live`. The periodic cycle releases the startup lease after each pass, so "held right now"
  alone would almost never be true; the lease release therefore keeps `acquired_at` ("last acquired"), and the bot counts as live when the lease of the
  tenant's or the owner's database is held now or was acquired within the last 10 minutes. A bot with no periodic feature enabled takes the lease only at
  startup, so it is detected only for 10 minutes after starting.
- The database is opened READ-ONLY (`mode=ro`). Per user, a summary of about 1–2 KB: daily results, check-in status and reply delays, token velocity,
  conditioning factor path and the 7-day performance before each increment, chat latency, structured-output failures, reply gaps, and a weekly trend.
  No raw rows, no ids beyond an 8-character prefix in the report header, no free text.
- The local model only: a non-local `OLLAMA_HOST` is refused. `RESEARCH_OLLAMA_MODEL` (default `OLLAMA_MODEL`). The analyst prompt asks for a
  "Psychological Profile & Recommendations" report: behavioural, not diagnostic, numbers only, wellbeing first, consent marked on every parameter change.
- The code appends the fixed disclaimer "WARNING: Any recommended parameter changes are critical_changes and require explicit wearer consent before
  implementation." to every report.
- Output to stdout or `--output` (a plain file; database file names and the tenant folder are refused). Nothing is written to any database, the idea box or
  any memory; tests compare database fingerprints before and after.

**G4** Export. Settings `ENABLE_RESEARCH_TELEMETRY=false`, `TELEMETRY_ENDPOINT`, `TELEMETRY_TOKEN`, `TELEMETRY_HMAC_KEY`. Wearer `research on|off|status`.
Payload allowlist per event type; the subject is `HMAC-SHA256(TELEMETRY_HMAC_KEY, tenant_id + ":" + user_id)` and the instance an HMAC of a random instance id
(never a plain hash: `tenant_0` is guessable); timestamps rounded to the hour; `training_inflation_factor` as a band (`1.00-1.10`, `1.10-1.20`, …); no
`local_date`-level free text. Store-and-forward worker outside the cycle's critical section: batches of rows with `synced_at IS NULL` and consent, the row id as
the idempotency key, TLS with verification (never `verify=False`), timeouts, exponential backoff, one warning when the link goes down and one info line when it
comes back. `synced_at` is set only on HTTP 200. Erase requests (`research off`, `forget all`) are queued per user and sent the same way.

**G5** `hive_mind_receiver/`: standalone FastAPI + SQLite, no imports from this codebase. Bearer tokens per sending instance (revocable), strict schema validation
(unknown fields and types refused), request size and rate limits, `UNIQUE(id)` for idempotency, `DELETE` by subject HMAC, 365-day retention job, a README for the
Cloudflare tunnel. Every sender is untrusted.

**G6** Hardening: cross-tenant isolation tests for telemetry, an injection test (wearer text never reaches a payload), an audit of the outbound payload against
the allowlist, a runbook section.
