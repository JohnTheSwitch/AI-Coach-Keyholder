"""
vision/parser.py

The strict parser of the vision model's answer (v0.39 Phase H1). Anything that is not exactly the schema becomes `unclear` / `invalid_output`.
`verified` stands only when the code the model transcribed matches the expected Chaster verification code; otherwise it is downgraded to `unclear`
("confirm, but never convict": a doubtful pass never becomes a fail, a doubtful anything never becomes a pass).
"""

from __future__ import annotations

import json
import re

from vision.models import MODEL_REASON_CODES, ReasonCode, Verdict

__all__ = ["parse_verdict", "normalize_code"]

_KEYS = {"verdict", "reason_code", "code_seen"}


def normalize_code(code: str | None) -> str:
    return re.sub(r"[\s\-_.]", "", code or "").upper()


def parse_verdict(raw: str, *, expected_code: str | None, require_code: bool = True) -> tuple[Verdict, ReasonCode, bool | None]:
    """(verdict, reason code, code matched or None when no expected code was compared)."""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return Verdict.UNCLEAR, ReasonCode.INVALID_OUTPUT, None
    if not isinstance(data, dict) or set(data) != _KEYS:
        return Verdict.UNCLEAR, ReasonCode.INVALID_OUTPUT, None
    verdict, reason, seen = data["verdict"], data["reason_code"], data["code_seen"]
    if verdict not in {v.value for v in Verdict} or reason not in MODEL_REASON_CODES or not (seen is None or (isinstance(seen, str) and len(seen) <= 32)):
        return Verdict.UNCLEAR, ReasonCode.INVALID_OUTPUT, None
    verdict, reason = Verdict(verdict), ReasonCode(reason)
    if verdict is Verdict.VERIFIED and reason is not ReasonCode.MATCHES:
        return Verdict.UNCLEAR, ReasonCode.INVALID_OUTPUT, None
    if verdict is not Verdict.VERIFIED:
        return verdict, reason, None
    if not expected_code:
        return (Verdict.UNCLEAR, ReasonCode.NO_EXPECTED_CODE, None) if require_code else (verdict, reason, None)
    matched = bool(seen) and normalize_code(seen) == normalize_code(expected_code)
    return (verdict, reason, True) if matched else (Verdict.UNCLEAR, ReasonCode.CODE_MISMATCH, False)
