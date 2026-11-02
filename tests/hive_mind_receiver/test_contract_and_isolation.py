"""Phase G5, from the bot's side (no FastAPI needed): the standalone receiver shares no code with the bot, and its wire contract matches exactly what the G4
sender produces, so the sender's indefinite retry can never stall on a contract mismatch. The receiver's own suite (FastAPI TestClient) lives in
hive_mind_receiver/tests and runs with its own dependencies."""

from __future__ import annotations

import ast
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from behavioral_telemetry import research
from hive_mind_receiver import contract

ROOT = Path(__file__).parent.parent.parent
RECEIVER = ROOT / "hive_mind_receiver"
THIRD_PARTY = {"fastapi", "pydantic", "starlette", "uvicorn", "httpx", "pytest"}


def _imports(path: Path) -> list[str]:
    names = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.append(node.module)
    return names


class TestIsolation:
    def test_the_receiver_imports_only_the_standard_library_its_framework_and_itself(self) -> None:
        offenders = []
        for path in RECEIVER.rglob("*.py"):
            for name in _imports(path):
                top = name.split(".")[0]
                if top not in sys.stdlib_module_names and top not in THIRD_PARTY and top != "hive_mind_receiver":
                    offenders.append(f"{path.relative_to(ROOT)}: {name}")
        assert offenders == []

    def test_the_bot_never_imports_the_receiver(self) -> None:
        offenders = []
        for path in ROOT.rglob("*.py"):
            rel = path.relative_to(ROOT)
            if rel.parts[0] in ("hive_mind_receiver", "tests") or rel.parts[0].startswith(".") or "__pycache__" in rel.parts or "venv" in rel.parts[0]:
                continue
            if any(n.split(".")[0] == "hive_mind_receiver" for n in _imports(path)):
                offenders.append(str(rel))
        assert offenders == []

    def test_its_own_requirements_and_readme(self) -> None:
        reqs = (RECEIVER / "requirements.txt").read_text(encoding="utf-8")
        for package in ("fastapi", "pydantic", "uvicorn"):
            assert re.search(rf"^{package}>=", reqs, re.M), package
        root_reqs = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        assert "fastapi" not in root_reqs and "uvicorn" not in root_reqs
        readme = (RECEIVER / "README.md").read_text(encoding="utf-8")
        for part in ("pip install -r hive_mind_receiver/requirements.txt", "python -m hive_mind_receiver", "cloudflared", "TELEMETRY_TOKENS",
                     "/v1/records", "/v1/erase"):
            assert part in readme, part


class TestContract:
    def test_the_same_schema_tag_and_limits_with_headroom(self) -> None:
        assert contract.SCHEMA == research.SCHEMA
        assert contract.MAX_RECORDS_PER_BATCH >= research.MAX_BATCH
        assert contract.MAX_BODY_BYTES >= research.MAX_PAYLOAD_BYTES + 64 * 1024     # the batch is capped before the envelope is added

    def test_the_same_event_types_keys_and_enum_values(self) -> None:
        sender = {t: {k: (None if v is None else set(v)) for k, v in keys.items()} for t, keys in research.ALLOWLIST.items()}
        receiver = {t: {k: (None if v is None else set(v)) for k, v in keys.items()} for t, keys in contract.METRICS.items()}
        assert sender == receiver

    @pytest.mark.parametrize("event_type,metrics", [
        ("response_delay", {"level": 2, "slot_index": 1, "status": "missed", "verdict": None, "delay_seconds": None, "window_seconds": 1800}),
        ("token_burn", {"amount": 1, "balance_after": 0, "hours_since_last_earn": None}),
        ("inflation_resilience", {"outcome": "capped", "training_inflation_factor": 1.5, "factor_before": 1.5, "days_since_previous_increment": 3.5,
                                  "progress_avg_7d": None, "clean_days_7d": 0}),
        ("daily_summary", {"local_date": "2026-11-02", "goals_assigned": 0, "goals_met": 0, "check_ins_due": 0, "check_ins_ok": 0, "violations": 2,
                           "clean": False, "streak_after": 0, "tokens_earned": 0, "progress": 0.0715, "pool_after": 0.0, "trust_used": 0.0,
                           "training_inflation_factor": 1.0, "operating_mode": "advanced", "bot_mood": "strict"}),
        ("llm_generation", {"latency_ms": 0, "path": "context", "outcome": "validation_failed", "structured_status": None, "trusted": False}),
        ("conversation_gap", {"seconds_since_last_reply": 86400 * 30}),
        ("daily_summary", {"progress": float("inf"), "bot_mood": "furious", "note": "free text"}),      # dirty input: the sender nulls it
    ])
    def test_every_sender_record_passes_the_receiver_validator(self, event_type, metrics) -> None:
        record = research.build_record(key="k" * 40, tenant_id="tenant_x", row_id="r1", user_id="u1",
                                       occurred_at=datetime(2026, 11, 2, 23, 59, tzinfo=timezone.utc), event_type=event_type, metrics=metrics)
        contract.check_metrics(record["event_type"], json.loads(json.dumps(record["metrics"])))
        assert re.fullmatch(r"[0-9a-f]{32}", record["id"]) and re.fullmatch(r"[0-9a-f]{64}", record["subject"])
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:00:00Z", record["occurred_hour"])
        assert re.fullmatch(r"[0-9a-f]{64}", research.instance_pseudonym("k" * 40))

    def test_the_receiver_validator_refuses_what_the_sender_never_sends(self) -> None:
        good = {"latency_ms": 1, "path": "plain", "outcome": "ok", "structured_status": None, "trusted": True}
        for bad in ({**good, "path": "free text"}, {**good, "extra": 1}, {k: v for k, v in good.items() if k != "trusted"}, {**good, "latency_ms": "1"},
                    {**good, "latency_ms": float("nan")}, {**good, "latency_ms": 2e12}):
            with pytest.raises(ValueError):
                contract.check_metrics("llm_generation", bad)
