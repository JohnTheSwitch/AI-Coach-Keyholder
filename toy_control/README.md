# toy_control

**Question it answers:** can this PC reach the owner's toy through Intiface Central, and does a short, capped buzz work?

Story idea P6 (bot control of the Satisfyer Deep Diver). Pairing works since 2026-11-17 (the toy, named "Kirka", had to be paired in Windows
Settings first; Intiface starts its server on launch). The owner chose option 1: the bot runs the toy during a task, and nothing else.

- `client.py`: a minimal Buttplug v3 client over Intiface Central's local WebSocket (`ws://127.0.0.1:12345`, local only): handshake,
  device list / scanning, `ScalarCmd` vibration, `StopDeviceCmd`. The transport is injected (aiohttp in use; a fake in tests).
- Safety in code: the level is capped at `TEST_MAX_LEVEL` (0.5) and a run at `TEST_MAX_SECONDS` (10 s); the toy is always told to stop in
  a `finally`, also on an error or Ctrl+C.
- The owner's test: `python -m cli toy-test --level 0.2 --seconds 2 --yes` (Intiface Central running, server started, toy paired).
- Bot sessions (patterns, overdue buzz, edging) are a later design that needs his approval: a new kind of action on his body.

## The bot runs it during a task (owner, 2026-11-17)

`session.py` (`ToySessions`): a task whose text has the Deep Diver gets the button "It's in: run it" (`obedience/service.py`, `uses_toy`).
Pressed on an assigned task, the bot plays a shaped session (`plan_pattern`, owner 2026-11-17: "more intelligently done") for the
task's own time (`task_minutes`, 20 when none, at most `MAX_MINUTES` 90). `profile_for` fits it to the task: default = warm-up, build in
"Steps" (his favourite from the Satisfyer app: four bursts at about 40/60/80/100 %, 0.5 s on and 0.5 s off (measured from
his screen recording), then back to the lowest; mixed with waves and a fast pulse so it never stays one thing), teasing (steps past his level, then almost
nothing, wait), a strongest finish, and every 2-4 minutes a surprise (off 3-8 s, then 2-5 s at a level he can't predict; `_surprises`, also in
prostate training); prostate training = slow build and long holds; a long wear = mostly off
with surprise bursts (quiet 1-4 minutes, owner 2026-11-23; bursts 10-40 s, each a shape he can't guess: climb, drop, steps, pulses, wave
or random jumps, `_burst`); not alone = gentle waves, at most 0.25. Every session is built around his own level (`store.py`, `toy_sessions`,
migration 092): after it he answers Too weak (+0.1) / Right (+0.02, so it still climbs over the weeks) / Too much (-0.1), within 0.2-1.0,
starting at 0.5; no answer keeps it. One session at a
time; "Stop" ends it; it always ends with StopDeviceCmd. It runs on its own thread, so Intiface never blocks the bot; when the toy cannot be
reached he gets a DM to run it himself. Only the owner's bot sets `obedience.toy`; tenants have none. Teasing at random times, edging and
an overdue buzz were offered and not chosen (he can't edge with it: only penis stimulation would do that).

**Pause (owner, 2026-11-23).** `pause()` / `resume()`: while paused the toy is off (StopDeviceCmd) and the current step's time waits; on
Continue the level is sent again and the step goes on. A session left paused for `MAX_PAUSED_SECONDS` (12 h) ends. Stop works while paused.

**Staying connected, and the check buzz (live and owner, 2026-11-23).** In a 3.7-minute quiet step nothing was sent, Intiface's
WebSocket pings went unanswered and it dropped the bot. The socket is now read all the time (`_aiohttp_connect`, plus a 10 s heartbeat), the
current command is sent again every `KEEPALIVE_SECONDS` (30 s; the toy also turns itself off after about 5 idle minutes), and Buttplug's
`Ping` goes out when the server asks for one. Every session starts with `CHECK_BUZZ` (0.4 for 0.6 s) and then a DM: "Connected ... you
should have felt a short buzz" (with Pause / Stop).

**toy.log (owner, 2026-11-23).** `logs/toy.log` (`infrastructure/decision_log.toy_log`, not in bot.log): the planned session (minutes, mode,
profile, level, steps, top), its start (device), every command sent (level and how long, off), Pause / Continue / Stop, a failure, and the
end (stopped early or not, seconds paused, seconds in all).
