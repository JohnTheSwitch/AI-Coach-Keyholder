"""
application/natural_queries.py

Zero-Command UX: natural questions for the read-only commands. A question maps to the same handler as the old phrase, so the answer comes from real records and
never from a model. Only whole questions match (with Czech variants), so a sentence that merely mentions a task stays conversation.

READ-ONLY by construction: everything that changes something keeps an exact phrase or a button, because those must never be guessed (phase_j_proposal.md 3.3).
A test pins that every target here is a read-only command.
"""

from __future__ import annotations

import re

from infrastructure.text import plain_text

__all__ = ["read_only_command", "READ_ONLY_TARGETS", "CAPABILITIES_TEXT", "COMMANDS_TEXT"]

CAPABILITIES_TEXT = (
    "I'm your coach and keyholder, and you can just talk to me.\n\n"
    "Ask me things like \"what's my task?\", \"am I on vacation?\", \"how am I doing?\" or \"what's my lock status?\", or tell me how your day went. "
    "If life needs a break from the device, just say you need an exemption.\n\n"
    "Anything that changes something, I'll confirm with a button or an exact phrase first. Health and safety always come first, and they never need my permission."
)

# Owner, 2026-11-16: "these commands are piling up. Can you do a help command?" `help` now also lists what he can type, grouped; `help all`
# lists every command. Only commands that are wired in this bot are named (a line about a feature that is off says nothing harmful).
COMMANDS_TEXT = (
    "**Things you can type**\n\n"
    "**Your lock**\n"
    "- \"What is holding my release?\" or \"Am I under supervision?\": what holds it, and about when it ends\n"
    "- `chaster status`: your lock as Chaster reports it\n\n"
    "**Training**\n"
    "- `log squat 105 4x9` or `log pullups 8,8,7`: record your sets (the weight goes up when every set hits the top)\n"
    "- `extra training`: one more short session today (once a week)\n\n"
    "**Tasks**\n"
    "- `extra task` or `extra task at 21:00`: one more erotic task today (once a day; counts like any task)\n\n"
    "**Your gear**\n"
    "- `gear`: what you own\n"
    "- `gear add <piece>`, `gear add <piece> as <your name for it>`, `gear remove <piece>` (while locked a removal waits 24 h)\n\n"
    "**Switches**\n"
    "- `features`: what is on and off for you\n"
    "- `<feature> off` / `<feature> on` (while locked, off waits 24 h)\n\n"
    "**Other**\n"
    "- `vacation`, `photo check-ins on|off`, `weekly review on|off`, `persona`, `memory`, `forget all`, `channel discord|chaster`, `mode`\n"
    "- Need a break from the device? Just say you need an exemption. Health and safety never need my permission.\n\n"
    "`help all` lists every command."
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

_EDGES = re.compile(r"^[\s,.!?]+|[\s,.!?]+$")


def read_only_command(message: str) -> str | None:
    """The read-only command this whole message asks for, or None."""
    if not isinstance(message, str) or len(message) > 120:
        return None
    text = _EDGES.sub("", plain_text(message).replace("'", ""))
    text = re.sub(r"\s+", " ", text)
    for command, pattern in _QUESTIONS:
        if pattern.fullmatch(text):
            return command
    return None
