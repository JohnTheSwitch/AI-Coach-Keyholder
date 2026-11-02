# behavioral_telemetry

v0.39 Phase G1 (`store.py`, `collector.py`; tables `behavioral_telemetry` and `telemetry_watermarks`, migration 039). Specification:
[`docs/architecture/phase_g_proposal.md`](../docs/architecture/phase_g_proposal.md).

**What it records.** Derived numeric facts about the user's behaviour, per database (each tenant has its own):

| event_type | from | main metrics |
|---|---|---|
| `response_delay` | concluded check-ins | level, status, verdict, delay_seconds, window_seconds |
| `token_burn` | token spends in the ledger | amount, balance_after, hours_since_last_earn |
| `inflation_resilience` | Progressive Conditioning increments | outcome, training_inflation_factor, factor_before, days_since_previous_increment, progress_avg_7d, clean_days_7d |
| `daily_summary` | daily evaluations | the day's facts, pool, streak, training_inflation_factor in force, operating_mode, bot_mood |

`record_in` refuses anything that is not a number, a boolean or a short enum string, so no check-in answer, message or prompt text can be stored. `bot_mood`
comes from engine state (penalty window or supervision -> `strict`; else `demanding` / `approving` / `neutral` from the day's result), never from the model.

**How it runs.** `TelemetryCollector.run(now)` is the last step of the periodic cycle, in its own `try` (a failure is logged and affects nothing else). Each
source has a `(timestamp, id)` watermark; at most 500 rows per source per cycle; `INSERT OR IGNORE` on `source_key` makes reruns, restarts and a second
instance harmless. Facts older than 180 days are never derived, and older rows are deleted once a day. Nothing here runs in the reply path.

**Consent.** In a tenant database the collector records only after the wearer accepted an invitation whose text contains `TELEMETRY_DISCLOSURE` (checked in
`consent_log`); tenant zero is the owner's own database. The wearer sees categories and counts with `telemetry` (never a value: the conditioning factor stays
hidden) and erases everything with `forget all` (together with the Coaching Memory). Nothing leaves the database; the opt-in export is Phase G4.

**Guards (tests).** Only this package, `system/` and tests import it; no package that builds model context mentions the table; the package writes only its
own two tables and never calls `datetime.now()`.
