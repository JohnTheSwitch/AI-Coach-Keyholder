"""
application/router.py

A minimal, explicit command router. Knows nothing about Discord (or
any channel) and nothing about how any handler is implemented
internally -- it only matches trimmed, lowercased message text against
a fixed, explicitly registered set of commands and calls the matching
handler.

Deliberately NOT a natural-language intent parser -- that is real
scope, later. This slice matches exact command strings, nothing more,
consistent with the request for "explicit and limited" supported
actions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from application.models import OutgoingMessage, UserAccount

__all__ = ["RequestContext", "RouteResult", "CommandRouter"]


@dataclass(frozen=True, kw_only=True)
class RequestContext:
    """What a handler receives -- the resolved UserAccount and the
    request's own `now`, nothing channel-specific. `external_message_id`
    is likewise channel-agnostic in shape (a plain optional string) even
    though only Discord populates it today -- see
    application/models.py's IncomingMessage for why it exists."""
    user: UserAccount
    now: datetime
    external_message_id: str | None = None


Handler = Callable[[RequestContext], OutgoingMessage]


@dataclass(frozen=True, kw_only=True)
class RouteResult:
    """CommandRouter.route()'s own return type -- explicit matched/
    unmatched signal, not a fixed fallback string baked into the
    router itself. Exactly one of the two states is representable:
    matched with an outgoing message, or unmatched with none."""
    matched: bool
    outgoing: OutgoingMessage | None

    def __post_init__(self) -> None:
        if self.matched != (self.outgoing is not None):
            raise ValueError("matched must be true exactly when outgoing is present.")


class CommandRouter:
    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}
        self._descriptions: dict[str, str] = {}
        self._families: dict[str, Handler] = {}

    def register(self, command: str, description: str, handler: Handler, *, listed: bool = True) -> None:
        """`listed=False`: a working command that `help` does not list (one of many variants a listed command explains)."""
        key = command.strip().lower()
        self._handlers[key] = handler
        if listed:
            self._descriptions[key] = description

    def register_family(self, family: str, *, invalid_handler: Handler) -> None:
        """A family token (e.g. "mode") whose own bare, standalone
        invocation (e.g. "mode" alone, with nothing else) gets a
        deterministic, family-specific reply instead of falling
        through to Conversation Engine's ordinary unmatched-text path.

        Command Family Fallback Precision: fires ONLY when the entire
        trimmed/lowercased input equals the family word exactly --
        never merely because the input STARTS WITH it. A multi-word
        input starting with a family word (a near-miss command typo
        like "task compelte", or ordinary conversation that happens to
        start with a common English word like "task"/"lock"/"mode",
        e.g. "task is really weighing on me today, can we talk?") is no
        longer intercepted here -- if it is not an exact registered
        command either, it now falls through as ordinary unmatched
        text, reaching Conversation Engine exactly like any other
        unmatched multi-word input already does. This does not change
        exact-command matching at all -- "task request"/"lock status"/
        etc. are still matched by `self._handlers` first, before this
        method's own lookup ever runs. Deliberately NOT fuzzy matching
        -- an exact string equality check, nothing approximate."""
        self._families[family.strip().lower()] = invalid_handler

    def route(self, text: str, context: RequestContext) -> RouteResult:
        command = text.strip().lower().lstrip("/!").strip()       # a leading `/` or `!` is a habit from other bots: it means the same
        handler = self._handlers.get(command)
        if handler is not None:
            return RouteResult(matched=True, outgoing=handler(context))

        family_handler = self._families.get(command)
        if family_handler is not None:
            return RouteResult(matched=True, outgoing=family_handler(context))

        return RouteResult(matched=False, outgoing=None)

    def help_text(self) -> str:
        lines = ["Available commands:"]
        for command in sorted(self._descriptions):
            lines.append(f"  {command} — {self._descriptions[command]}")
        return "\n".join(lines)

    def unrecognized_text(self) -> str:
        """Public now -- ApplicationService's own conversation_engine is
        None branch (no engine configured) needs this same fallback
        text; route() itself no longer produces it directly (RouteResult's
        matched=False leaves that decision to the caller)."""
        return "I don't recognize that yet. Ask me for help and I'll tell you what I can do."
