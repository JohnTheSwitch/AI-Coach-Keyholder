# settings_watch

Gap fix G4 (owner, 2026-11-15): settings changes are visible. The watched settings are the time zone, quiet hours, workday hours, holiday
country, weekend release start (from the configuration, so they change only with a restart) and the runtime halt (on / off). The first
sighting is only remembered; a later difference is recorded in `settings_changes`, asked about in the DM ("tell me why", free answer, never a
consequence by itself; clearing the halt is only reported) and listed in the Sunday review.

Tables `settings_seen`, `settings_changes` (migration 075), written only by `service.py`. Event: `settings_watch.changed`. Wired in
`bot/discord_bot.py` for the owner's single wearer.
