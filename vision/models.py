"""
vision/models.py

Types of the Zero-Trace vision core (v0.39 Phase H1; docs/architecture/phase_h_proposal.md). A result carries only enums, a boolean and numbers: the model
is never asked for, and nothing here can hold, a free-text description of an image.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = ["Verdict", "VerificationType", "ReasonCode", "MODEL_REASON_CODES", "VisionResult", "VisionError"]


class Verdict(StrEnum):
    VERIFIED = "verified"
    NOT_VERIFIED = "not_verified"
    UNCLEAR = "unclear"


class VerificationType(StrEnum):
    DEVICE_CHECK = "device_check"                  # rule compliance: the device is worn, code visible
    POST_OPENING_CHECK = "post_opening_check"      # rule compliance: the device is back on after a temporary opening, code visible
    TASK_PROOF = "task_proof"                      # goal / task evidence: the task is shown done, code visible


class ReasonCode(StrEnum):
    # what the model may answer
    MATCHES = "matches"
    DEVICE_NOT_VISIBLE = "device_not_visible"
    DEVICE_NOT_WORN = "device_not_worn"
    TASK_NOT_SHOWN = "task_not_shown"
    CODE_MISSING = "code_missing"
    CODE_UNREADABLE = "code_unreadable"
    IMAGE_UNCLEAR = "image_unclear"
    NOT_RELEVANT = "not_relevant"
    OTHER = "other"
    # set by our own checks, never by the model
    CODE_MISMATCH = "code_mismatch"
    NO_EXPECTED_CODE = "no_expected_code"
    INVALID_OUTPUT = "invalid_output"


MODEL_REASON_CODES = tuple(c.value for c in ReasonCode if c not in (ReasonCode.CODE_MISMATCH, ReasonCode.NO_EXPECTED_CODE, ReasonCode.INVALID_OUTPUT))


@dataclass(frozen=True)
class VisionResult:
    verdict: Verdict
    reason_code: ReasonCode
    code_matched: bool | None          # None: no expected code was checked
    latency_ms: int


class VisionError(Exception):
    """A system error (fetch, model, transport). It carries a short code only, never a URL, a payload or model output. The caller decides nothing on it:
    the item stays pending (phase_h_proposal section 5)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
