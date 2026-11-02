# behavioral_telemetry

v0.39 Phase G1 (`store.py`, `collector.py`; tables `behavioral_telemetry` and `telemetry_watermarks`, migration 039). Specification:
[`docs/architecture/phase_g_proposal.md`](../docs/architecture/phase_g_proposal.md).

**What it records.** Derived numeric facts about the user's behaviour, per database (each tenant has its own):

| event_type | from | main metrics |
|---|---|---|
| `response_delay` | concluded check-ins | level, status, verdict, delay_seconds, window_seconds |
| `token_burn` | token spends in the ledger | amount, balance_after, hours_since_last_earn |
| `inflation_resilience` | Progressive Conditioning increments | outcome, training_inflation_factor, factor_before, days_since_previous_increment, progress_avg_7d, clean_days_7d |
| `llm_generation` | the runtime buffer (G2) | latency_ms, path, outcome, structured_status, trusted |
| `conversation_gap` | the runtime buffer (G2) | seconds_since_last_reply |
| `daily_summary` | daily evaluations | the day's facts, pool, streak, training_inflation_factor in force, operating_mode, bot_mood |

`record_in` refuses anything that is not a number, a boolean or a short enum string, so no check-in answer, message or prompt text can be stored. `bot_mood`
comes from engine state (penalty window or supervision -> `strict`; else `demanding` / `approving` / `neutral` from the day's result), never from the model.

**How it runs.** `TelemetryCollector.run(now)` is the last step of the periodic cycle, in its own `try` (a failure is logged and affects nothing else). Each
source has a `(timestamp, id)` watermark; at most 500 rows per source per cycle; `INSERT OR IGNORE` on `source_key` makes reruns, restarts and a second
instance harmless. Facts older than 180 days are never derived, and older rows are deleted once a day.

**The one live-path piece (G2).** `runtime.RuntimeTelemetryBuffer.record` is the conversation engine's `runtime_sink`: a counter step, a dict update
and `deque.append` on a bounded deque (2000), no I/O, never raises. The engine guards every call as well. The collector drains the buffer at the
start of each run in one transaction (discarded without consent; samples of a user taken before `forget all` are dropped).

**Consent.** In a tenant database the collector records only after the wearer accepted an invitation whose text contains `TELEMETRY_DISCLOSURE` (checked in
`consent_log`); tenant zero is the owner's own database. The wearer sees categories and counts with `telemetry` (never a value: the conditioning factor stays
hidden) and erases everything with `forget all` (together with the Coaching Memory and, if it was on, research sharing).

**Owner analysis (G3).** `analysis.py` + `python -m cli research analyze <tenant_id> --days 30 --owner-id <you>`: a read-only summary, the local
model, the fixed consent disclaimer; printed or written to a plain file, never stored. Refuses while the bot is running unless `--allow-while-live`.

**Opt-in research export (G4).** `research.py`: the wearer's `research on|off|status` (default off, consent-logged, not retroactive), an allowlist
payload (HMAC pseudonyms, hour rounding, the conditioning factor only as a 0.10 band, enum strings only), and a store-and-forward link in its own background
task (HTTPS, bearer token, batches of 200 / 256 KB, HTTP 200 only, exponential backoff, one warning per outage). `research off` and `forget all` queue an
erase of the pseudonym. Needs `ENABLE_RESEARCH_TELEMETRY`, `TELEMETRY_ENDPOINT`, `TELEMETRY_TOKEN`, `TELEMETRY_HMAC_KEY`.

**Guards (tests).** `conversation_engine/` imports no database module; the sink works with every database and file access blocked; a raising sink
or clock never changes a reply. Only this package, `system/` and tests import it; no package that builds model context mentions the table; the package writes only its
own two tables and never calls `datetime.now()`.
