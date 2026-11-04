"""
application/natural_queries.py

Zero-Command UX: natural questions for the read-only commands. A question maps to the same handler as the old phrase, so the answer comes from real records and
never from a model. Only whole questions match (with Czech variants), so a sentence that merely mentions a task stays conversation.

READ-ONLY by construction: everything that changes something keeps an exact phrase or a button, because those must never be guessed (phase_j_proposal.md 3.3).
A test pins that every target here is a read-only command.
"""

from __future__ import annotations

import re

__all__ = ["read_only_command", "READ_ONLY_TARGETS", "CAPABILITIES_TEXT"]

CAPABILITIES_TEXT = (
    "I'm your coach and keyholder, and you can just talk to me.\n\n"
    "Ask me things like \"what's my task?\", \"am I on vacation?\", \"how am I doing?\" or \"what's my lock status?\", or tell me how your day went. "
    "If life needs a break from the device, just say you need an exemption.\n\n"
    "Anything that changes something, I'll confirm with a button or an exact phrase first. Health and safety always come first, and they never need my permission."
)

_QUESTIONS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (command, re.compile(pattern)) for command, pattern in (
        ("help", r"what can you do|how do you work|what can i ask( you)?|what can i do here|co umis|co vsechno umis"),
        ("task active", r"whats my( current| active)? task|what is my( current| active)? task|do i have a task( today)?|show( me)? my task|jaky mam ukol|mam nejaky ukol"),
        ("vacation", r"am i on( a)? vacation|do i have a vacation( planned)?|is my vacation (on|running)|mam dovolenou"),
        ("status", r"how long is left on my penalty( window)?|how long is my penalty( window)?|is there a penalty window|am i in a penalty window"
                   r"|do i have a penalty( window)?|whats my penalty( window)?( status)?"),
        ("memory", r"what do you (remember|know) about me|show( me)? (my|your) memory"),
        ("telemetry", r"what (do you|are you) (record|recording|track|tracking)( about me)?"),
        ("mode", r"what mode am i in|which mode am i in|whats my mode"),
        ("lock status", r"what did i report|whats my lock report"),
        ("channel status", r"where do you (send|message) (me|your messages)|which channel do you use"),
        ("preferences", r"what are my preferences|show( me)? my preferences"),
    )
)
READ_ONLY_TARGETS = frozenset(command for command, _ in _QUESTIONS)

_FOLD = str.maketrans("áčďéěíňóřšťúůýž", "acdeeinorstuuyz")
_NOISE = re.compile(r"['’`]")
_EDGES = re.compile(r"^[\s,.!?]+|[\s,.!?]+$")


def read_only_command(message: str) -> str | None:
    """The read-only command this whole message asks for, or None."""
    if not isinstance(message, str) or len(message) > 120:
        return None
    text = _EDGES.sub("", _NOISE.sub("", message.lower().translate(_FOLD)))
    text = re.sub(r"\s+", " ", text)
    for command, pattern in _QUESTIONS:
        if pattern.fullmatch(text):
            return command
    return None
