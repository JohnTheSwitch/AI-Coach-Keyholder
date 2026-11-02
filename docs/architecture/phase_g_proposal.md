# Phase G: Behavioral Telemetry and the opt-in Hive-Mind

Status: G0 specification (binding); G1 (local derived telemetry), G2 (runtime buffer), G3 (owner analysis), G4 (opt-in export), G5 (receiver) and G6 (hardening, two-step erase, Iron Contract, vacations) built. Phase G is complete.

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

**G4 (built)** Opt-in store and forward (`behavioral_telemetry/research.py`, migration 040).
- Settings: `ENABLE_RESEARCH_TELEMETRY=false`, `TELEMETRY_ENDPOINT` (https only), `TELEMETRY_TOKEN`, `TELEMETRY_HMAC_KEY` (at least 32 characters). The token
  and key are excluded from `Config`'s repr and never logged.
- Wearer: `research status|on|off`, default off. `research on` records an `approved` consent row with the explanation shown; only records that occur AFTER it
  are exported (not retroactive, also after a later re-consent). `research off` and `forget all` (which switches research off too) record `revoked` and
  queue an erase request.
- Gates: a record leaves only with the owner's flag AND the wearer's `research on`. Erase requests are sent whenever the link is configured, even with the
  flag off, so a revocation is never stranded.
- Payload: an allowlist per event type; every string must be one of a fixed enum set and every number finite, otherwise the value becomes null. Subject =
  `HMAC-SHA256(key, "subject:<tenant id>:<user id>")`; record id = first 32 hex of `HMAC(key, "record:<tenant id>:<row id>")` (stable for idempotent
  retries, unlinkable to the local row); instance = `HMAC(key, "instance")`; `occurred_hour` rounded down to the hour; `training_inflation_factor` only as
  `training_inflation_band` (`1.00-1.10` … `1.40-1.50`, the cap in the last band). Never sent: tenant id, user id, row id, `local_date`, `slot_index`,
  `factor_before`, any text.
- Link: one background task in the bot (`research_export_loop`, a worker thread every 60 s) over the owner's database and every live tenant's. Per pass and
  database: erase requests first, then records in batches of at most 200 and 256 KB (halved until they fit; only what was sent is marked). Timeouts 5 s
  connect / 20 s read, no redirects, TLS verification as the library default (never disabled). Only HTTP 200 counts; then `synced_at` / `sent_at` is set.
  Any other status or exception: stop the pass, back off 1, 2, 4 … minutes up to 1 hour, one warning when the link goes down, one info line when it
  recovers, nothing in between.
- Changing `TELEMETRY_HMAC_KEY` changes every pseudonym: earlier exports can then no longer be erased by pseudonym. Keep the key stable and secret.

**Wire contract for G5** (`schema` `hive-telemetry/1`, header `Authorization: Bearer <token>`, JSON body):
- `POST <endpoint>/v1/records`: `{"schema", "instance": <64 hex>, "records": [{"id": <32 hex>, "subject": <64 hex>, "event_type", "occurred_hour":
  "YYYY-MM-DDTHH:00:00Z", "metrics": {...allowlisted keys...}}]}` (1–200 records). Answer 200 only after every record is stored or already present.
- `POST <endpoint>/v1/erase`: `{"schema", "instance", "erase": {"id": <32 hex>, "subject": <64 hex>}}`. Answer 200 after deleting every record of that
  subject (and remembering the erase id, so a retry is harmless).

**G5 (built)** `hive_mind_receiver/` (see its README): a standalone FastAPI + SQLite service with its own `requirements.txt`; it imports only the
standard library, FastAPI/Pydantic/Starlette/Uvicorn and itself (guard test in the bot's suite).
- Pure ASGI guard before any body is read: Bearer tokens (`TELEMETRY_TOKENS` / `TELEMETRY_TOKEN`, one per sending instance, constant-time comparison) or
  401; a shared budget for failed authentications (429); a request budget per token (429 + Retry-After); JSON only (415); `Content-Length` required (411);
  at most 512 KB declared or actually sent (413). The body is then read by the guard and replayed to the application.
- Pydantic models with `extra="forbid"` plus `contract.check_metrics`: exactly the contract keys per event type, enum strings, finite numbers up to 1e12,
  booleans or null; 422 without echoing the input; one bad record refuses the batch.
- Records keyed by (sender, id): a retransmission is ignored, never overwritten. Erase deletes the subject's records of the SAME sender only and is
  remembered by (sender, erase id). Senders are stored as a SHA-256 prefix of the token. 365-day retention for records and erase receipts.
- No documentation endpoints, no server header, counts-only logs; binds 127.0.0.1:8787 by default for a Cloudflare tunnel.
- Tests: the receiver's own suite (`hive_mind_receiver/tests`, FastAPI TestClient, in process; 60 tests) and, in the bot's suite, the isolation guard and
  a contract test that runs real G4 `build_record` output through the receiver's validator and compares the allowlists key by key.
- `hive_mind_receiver/clock.py` is the one documented exception to the repository's `datetime.now()` guard (a standalone service cannot use the bot's Clock).

**G6** Hardening: cross-tenant isolation tests for telemetry, an injection test (wearer text never reaches a payload), an audit of the outbound payload against
the allowlist, a runbook section.

## 7. G6: hardening, the two-step erase, the Iron Contract and vacations (built)

Owner request (2026-11-03): a confirmation for `forget all`; an "Iron Contract" that, while locked, blocks changing check-in schedules, lowering the
difficulty and approving new rules, injects `[STATE: LOCKED]` and makes the model refuse negotiation; vacations schedulable only while unlocked; and a
cross-tenant audit of everything in Phase G.

**`forget all`** now takes two steps. The first answer is the owner's warning ("This will permanently delete your psychological profile and revoke sharing
consent ..."), followed by what exactly is erased. Only `yes` (case-insensitive) within 5 minutes erases; any other message cancels, and a late `yes`
erases nothing. The pending request is held in memory per user (a restart cancels it) and can never be confirmed from the untrusted Chaster chat path.

**Constitutional limits on the Iron Contract** (`philosophy.md`):
- 2.5: consent is revocable "at any time", including "a slowdown, a relaxation, or a suspension". So `conditioning off`, `research off`, `forget all`,
  `halt`, `resume`, `leave` and `vacation end` are never blocked. The invitation already promises `conditioning off` "at any time". Lowering the operating
  mode is a different matter: it is a commitment the wearer chose, already gated (30 days, 24 h, penalty windows), and the new lock gate is disclosed in the
  invitation and applied only after that disclosure was accepted.
- 2.3 / 2.4: "real-life circumstances (exams, illness, crises, work overload)" always come first. Rule 7 therefore refuses negotiation and excuses but never
  treats real life as an excuse, never mocks, and keeps the safety exception.
- The `[STATE: LOCKED]` marker is application state in the domain-context section; the behaviour is guardrail rule 7. Nothing is placed inside the fenced
  `<context>` memory block, which the model is told is data, never instructions. `_SYSTEM_BOUNDARIES` is unchanged.

**What the gate covers today.** The wearer has no command to change the check-in schedule and no command to approve rules (idea-box adoption is an owner
decision followed by the wearer's normal consent flow). The guard therefore covers `mode request standard` and `mode confirm` toward Standard, plus
`vacation schedule` / `vacation cancel` (which need "unlocked" everywhere, as a property of the vacation feature). Any future command of those kinds should
carry `@requires_unlocked`.

**Lock status and vacations:** see `iron_contract/README.md`. Defaults chosen here: at most 14 days, at most 90 days ahead, one at a time, a vacation
starting today begins at that moment, and only days lying entirely inside it are paused.

**Cross-tenant audit** (`tests/iron_contract/test_iron_contract.py::TestCrossTenantAudit`). For two tenants built by the factory, these are separate
objects over separate databases: the runtime buffers, telemetry stores, research preferences, contracts and vacations. A chat turn of tenant A lands in
A's database only. An opt-in and an erase of A are invisible to B. Pseudonyms differ per tenant for the same user id. The G3 summary reads one database. The
owner's database receives nothing. The receiver keeps senders apart (keys and erase scoped by sender; its own suite covers it end to end).
