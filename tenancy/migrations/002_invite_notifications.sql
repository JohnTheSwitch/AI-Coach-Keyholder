-- =============================================================================
-- Platform database, migration 002 -- v0.39 Phase E3: invitation delivery
-- =============================================================================
-- `invite_notified_at`: when the bot delivered the invitation DM to the invited person (NULL = not yet). The bot sends it after the
-- owner invites from the CLI or from a DM; a failed delivery is simply retried on the next pass.
-- =============================================================================

ALTER TABLE tenants ADD COLUMN invite_notified_at TEXT;

INSERT INTO schema_version (version, applied_at, description)
VALUES (2, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'platform: tenants.invite_notified_at');
