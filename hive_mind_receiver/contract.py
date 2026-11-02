"""
hive_mind_receiver/contract.py

The wire contract `hive-telemetry/1` as plain data (standard library only). The sender's documentation is the source of truth: a record's `metrics` must
contain exactly the keys listed for its `event_type`; a key mapped to a tuple takes one of those strings or null; a key mapped to None takes a number, a
boolean or null.
"""

import math

SCHEMA = "hive-telemetry/1"
MAX_RECORDS_PER_BATCH = 200
MAX_BODY_BYTES = 512 * 1024          # the sender caps its bodies at 256 KB
MAX_ABS_NUMBER = 1e12

_STATUS = ("answered", "answered_uncertain", "insufficient", "missed")
_VERDICT = ("SUFFICIENT", "INSUFFICIENT", "UNCERTAIN", "NOT_EVALUATED")
_BANDS = ("1.00-1.10", "1.10-1.20", "1.20-1.30", "1.30-1.40", "1.40-1.50")

METRICS = {
    "response_delay": {"level": None, "status": _STATUS, "verdict": _VERDICT, "delay_seconds": None, "window_seconds": None},
    "token_burn": {"amount": None, "balance_after": None, "hours_since_last_earn": None},
    "inflation_resilience": {"outcome": ("increased", "capped", "not_token", "wearer_off", "owner_off"), "training_inflation_band": _BANDS,
                             "days_since_previous_increment": None, "progress_avg_7d": None, "clean_days_7d": None},
    "daily_summary": {"goals_assigned": None, "goals_met": None, "check_ins_due": None, "check_ins_ok": None, "violations": None, "clean": None,
                      "streak_after": None, "tokens_earned": None, "progress": None, "pool_after": None, "trust_used": None,
                      "training_inflation_band": _BANDS, "operating_mode": ("standard", "advanced"),
                      "bot_mood": ("strict", "demanding", "approving", "neutral")},
    "llm_generation": {"latency_ms": None, "path": ("plain", "structured", "tools", "context"), "outcome": ("ok", "generation_failed", "validation_failed"),
                       "structured_status": ("ok", "side_dropped", "unparseable"), "trusted": None},
    "conversation_gap": {"seconds_since_last_reply": None},
}


def check_metrics(event_type: str, metrics: dict) -> None:
    """Raises ValueError unless `metrics` matches the contract for `event_type` exactly."""
    spec = METRICS[event_type]
    if not isinstance(metrics, dict) or set(metrics) != set(spec):
        raise ValueError("metrics keys do not match the contract")
    for key, allowed in spec.items():
        value = metrics[key]
        if value is None:
            continue
        if allowed is not None:
            if not isinstance(value, str) or value not in allowed:
                raise ValueError(f"{key}: not an allowed value")
        elif isinstance(value, bool):
            continue
        elif isinstance(value, (int, float)):
            if not math.isfinite(value) or abs(value) > MAX_ABS_NUMBER:
                raise ValueError(f"{key}: number out of range")
        else:
            raise ValueError(f"{key}: must be a number, a boolean or null")
