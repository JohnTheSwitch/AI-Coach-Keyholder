# toy_control

**Question it answers:** can this PC reach the owner's toy through Intiface Central, and does a short, capped buzz work?

Story idea P6 (bot control of the Satisfyer Deep Diver) is approved but on hold until the pairing works (2026-11-15). This module is only
the engineering groundwork and the owner's own test; **nothing here is wired into the bot**, and nothing runs unless he starts it.

- `client.py`: a minimal Buttplug v3 client over Intiface Central's local WebSocket (`ws://127.0.0.1:12345`, local only): handshake,
  device list / scanning, `ScalarCmd` vibration, `StopDeviceCmd`. The transport is injected (aiohttp in use; a fake in tests).
- Safety in code: the level is capped at `TEST_MAX_LEVEL` (0.5) and a run at `TEST_MAX_SECONDS` (10 s); the toy is always told to stop in
  a `finally`, also on an error or Ctrl+C.
- The owner's test: `python -m cli toy-test --level 0.2 --seconds 2 --yes` (Intiface Central running, server started, toy paired).
- Bot sessions (patterns, overdue buzz, edging) are a later design that needs his approval: a new kind of action on his body.
