"""
infrastructure/decision_log.py

The decision log (owner, 2026-11-10): anyone with access to the files can follow what the system decided and why, without reading the database.

Plain-text files in the log folder, next to bot.log:
  - `decisions.log`: every decision and every domain event after it was committed (incidents, penalty windows, lock decisions and their execution,
    check-in warnings, detector questions and the judge's verdicts, release decisions, ...);
  - `release.log`: only the release (unlock) decisions, including the planned release time. It holds the secret release time, so a wearer who wants
    the surprise should not open it;
  - `model.log`: every exchange with the local model, the whole prompt and the raw answer, so a bad reply can be traced to what the model was given.

Trust scores are never written: a key containing "score" is replaced by "[hidden]".
Writing a line never raises; a broken log never stops a decision.
"""

from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

__all__ = ["decision", "release_decision", "log_event", "model_call", "install_decision_files", "DECISIONS_LOGGER", "RELEASE_LOGGER", "MODEL_LOGGER"]

DECISIONS_LOGGER = "ai_coach_keyholder.decisions"
RELEASE_LOGGER = "ai_coach_keyholder.decisions.release"
MODEL_LOGGER = "ai_coach_keyholder.model_calls"
_MAX_BYTES = 5 * 1024 * 1024
_BACKUPS = 5

_decisions = logging.getLogger(DECISIONS_LOGGER)
_release = logging.getLogger(RELEASE_LOGGER)
_model = logging.getLogger(MODEL_LOGGER)
_model.propagate = False            # whole prompts are long: they go to model.log only, never into bot.log


def _clean(value: Any, key: str = "") -> Any:
    if "score" in key.lower():
        return "[hidden]"
    if isinstance(value, dict):
        return {k: _clean(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def _line(area: str, what: str, details: dict[str, Any]) -> str:
    text = f"[{area}] {what}"
    cleaned = {k: v for k, v in _clean(details).items() if v is not None and v != ""}
    if cleaned:
        text += " | " + json.dumps(cleaned, ensure_ascii=False, default=str)
    return text


def decision(area: str, what: str, **details: Any) -> None:
    """One line in decisions.log: `area` is the module ("infractions", "check_ins", ...), `what` a plain sentence, `details` the reasoning."""
    try:
        _decisions.info(_line(area, what, details))
    except Exception:  # pragma: no cover - a log line never stops a decision
        pass


def release_decision(what: str, **details: Any) -> None:
    """One line in release.log (and decisions.log)."""
    try:
        _release.info(_line("release", what, details))
    except Exception:  # pragma: no cover
        pass


def log_event(event_type: str, source_module: str, payload: dict[str, Any] | None, occurred_at: Any = None) -> None:
    """A committed domain event, written by the outbox publisher. Release events also go to release.log."""
    details = dict(payload or {})
    if occurred_at is not None:
        details["occurred_at"] = occurred_at
    try:
        line = _line(f"event:{source_module}", event_type, details)
        (_release if event_type.startswith(("release", "release_plan")) else _decisions).info(line)
    except Exception:  # pragma: no cover
        pass


def model_call(*, model: str, messages: list, tools: list, structured: bool, answer: Any, error: str | None, seconds: float) -> None:
    """One block in model.log: the whole prompt and the raw answer (or the error). No model ever sees a release time or a Trust score, so none is here."""
    try:
        head = f"model={model} seconds={seconds} structured={structured}" + (f" tools={','.join(tools)}" if tools else "") + (f" ERROR={error}" if error else "")
        parts = [f"===== {head}"]
        for m in messages:
            parts.append(f"--- {m.get('role', '?')}" + (f" ({m['tool_name']})" if m.get("tool_name") else ""))
            parts.append(str(m.get("content", "")))
            if m.get("tool_calls"):
                parts.append(json.dumps(m["tool_calls"], ensure_ascii=False, default=str))
        if isinstance(answer, dict):
            parts.append("--- answer")
            parts.append(str(answer.get("content", "")))
            if answer.get("tool_calls"):
                parts.append(json.dumps(answer["tool_calls"], ensure_ascii=False, default=str))
        _model.info(chr(10).join(parts))
    except Exception:  # pragma: no cover
        pass


def install_decision_files(log_dir: str | Path) -> None:
    """Attaches the rotating files. Idempotent. Decision lines still reach the main log as well; model exchanges do not."""
    folder = Path(log_dir)
    folder.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(message)s")
    for logger, name in ((_decisions, "decisions.log"), (_release, "release.log"), (_model, "model.log")):
        path = str((folder / name).resolve())
        if any(isinstance(h, RotatingFileHandler) and getattr(h, "baseFilename", None) == path for h in logger.handlers):
            continue
        handler = RotatingFileHandler(path, maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8")
        handler.setFormatter(fmt)
        handler.setLevel(logging.INFO)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
