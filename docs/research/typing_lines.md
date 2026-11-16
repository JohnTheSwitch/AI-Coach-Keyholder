# Typing Lines: desk research (work queue Q10, 2026-11-16)

**Question (roadmap Ideas):** could the bot use Chaster's Typing Lines extension, i.e. can a partner extension be resent unchanged when the
bot rewrites the lock's extension list, or driven by API? Desk research only: no write to any lock, nothing tried live.

## What we already know from our own live tests (docs/architecture/chaster_capability_map.md)

- `typing-lines` is in the group "games and rewards that change time or freeze": as installed today, failing its lines adds time or freezes,
  which this system must never cause (the system never changes a lock's duration).
- **A partner extension survives an unchanged resend.** `POST /locks/{id}/extensions` with the complete list, the partner entry included as
  read back, keeps it (same `_id`). A *changed* partner config is silently dropped (HTTP 201, old value stays; verified on
  `simon-memory-game`). So the bot can keep rewriting the lock's own extensions (tasks, verification picture, hygiene opening) with Typing
  Lines installed, as long as it sends the Typing Lines entry back exactly as read and re-reads after every write.

## What Chaster's public docs say (docs.chaster.app, Extensions API)

- A partner extension's per-lock config is created and saved only through **its own configuration page**: the page receives a
  `partnerConfigurationToken`, and the extension's backend calls `GET`/`PUT /api/extensions/configurations/:token`. The page opens for a
  wearer creating a self-lock, a keyholder creating a shared lock, or a keyholder editing a running session. The only other documented route,
  `PATCH /api/extensions/sessions/:sessionId` (a `config` key), is part of the Extensions API and needs **the extension developer's own
  token** ("every call to the Extensions API must pass through your backend").
- **Extension communication** (experimental): one extension can discover and call another extension's capabilities on the same lock
  (`GET /api/extensions/sessions/:sessionId/capabilities`, `POST .../actions/:commandId`), but only capabilities whose `invocableBy` contains
  `extension`, authenticated with a developer token or an extension iframe token. Typing Lines is not among the documented built-in
  namespaces (tasks, hygiene opening, pillory, penalties, verification picture); whether it publishes callable commands is not documented.
- Chaster's **MCP server** is for extension developers too (developer token, only on sessions of your own extension).

## Conclusion and proposal

1. **The bot cannot configure or drive Typing Lines** through the public API it uses (a user's OAuth token). Configuring it means the
   keyholder (here: the owner on his own lock) opens its configuration page by hand; driving it means being a Chaster extension developer.
2. **Coexistence is possible:** if the owner installs and configures Typing Lines himself, the bot's extension rewrites keep it unchanged
   (send it back as read, re-read after each write). Its own punishments (adding time / freezing) would still be Typing Lines' own, set by
   him, which conflicts with this system's "never changes duration" only if the bot were the one to set it, and it is not.
3. **Recommendation:** keep the lines in the bot (paper sheets, typed fallback, swats), which the bot can judge and count. Typing Lines adds
   nothing the bot can use. If the owner still wants it as a manual game, the only engineering needed is a test that our extension writes
   preserve an unknown partner entry byte for byte, which the capability map already observed. **Not built; nothing to approve unless he
   wants that test.**
4. A real integration would need a Chaster extension of our own (developer application, Chaster review). Out of scope.

Sources: docs.chaster.app pages "Extensions API: Getting started", "Configuration", "Create your extension: Configuration page",
"Extension communication: Calling extensions" and "MCP"; the repo's own capability map.
