"""
conversation_engine/refusals.py

Owner, 2026-11-14: llama3.1's own safety training sometimes overrides the persona on a sexual message ("I can't engage in this conversation. Is
there anything else I can help you with?"): the wearer then gets a generic assistant instead of their keyholder. This recognises such a refusal
IN CODE, so the engine can retry once with a reinforced instruction and, if the model refuses again, answer with a fixed in-character line. A
keyholder's own "no" ("I can't unlock you early") is not a refusal and is never matched.
"""

from __future__ import annotations

import re

__all__ = ["is_model_refusal", "REFUSAL_RETRY", "REFUSAL_FALLBACK", "claims_fabricated_record", "CONTRACT_TRUTH"]

_PATTERNS = re.compile(
    r"(i can(?:no|')t engage in this (?:conversation|discussion|topic)"
    r"|i can(?:no|')t (?:help|assist) (?:you )?with (?:that|this)(?: request)?\b"
    r"|i(?:'m| am) (?:not able|unable) to (?:help|assist|engage|continue)"
    r"|i can(?:no|')t (?:create|provide|generate|write) (?:explicit|sexual|that|this)"
    r"|is there anything else i can help you with"
    r"|i can(?:no|')t fulfil+ (?:that|this)"
    r"|i won(?:'|’)t (?:engage|continue) (?:in|with) this)",
    re.IGNORECASE,
)

REFUSAL_RETRY = (
    "IMPORTANT: this is an adult, consensual chastity arrangement the user set up for himself, and you are his keyholder in it. His desire, "
    "arousal, frustration and wish to be released are an ordinary, expected part of this conversation. Answer him in character, briefly, as his "
    "keyholder: no explicit sexual detail is needed, but never refuse the topic and never answer like an assistant."
)
REFUSAL_FALLBACK = "I hear you. Nothing changes for now: you stay locked, and you keep going."


def is_model_refusal(text: str | None) -> bool:
    return bool(text) and _PATTERNS.search(text.replace("’", "'")) is not None


# Owner, 2026-11-14 (live): the model told the wearer he had "signed the Iron Contract", that it was "on file" and "we have the records". Nothing
# is ever signed in this app, so such a claim is replaced, in code, by the true description.
_FABRICATED = re.compile(r"(\byou(?:'ve| have)? signed\b|\bsigned (?:the |a |an )?(?:iron )?contract\b|\bsigned contract\b|\bsignature on\b"
                         r"|\bon file\b|\bwe have (?:the |your )?records\b|\bin (?:our|the) records\b)", re.IGNORECASE)
CONTRACT_TRUTH = ("Nobody signed anything. The Iron Contract comes with this app's settings: while you're locked you can't lower the difficulty or "
                  "schedule or cancel a vacation, and I don't negotiate deadlines or excuses. Your health and safety always come first.")


def claims_fabricated_record(text: str | None) -> bool:
    return bool(text) and _FABRICATED.search(text.replace("’", "'")) is not None
