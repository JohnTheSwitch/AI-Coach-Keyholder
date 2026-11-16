# Bot control of the Satisfyer Deep Diver: research (2026-11-15)

Roadmap idea 8 (not approved yet). The owner will run the pairing test himself; this file is the preparation for it.

## Can it work?

Probably, with a real risk on the radio. Intiface Central (the Buttplug server) supports the Satisfyer line through one shared protocol,
documented from the Royal One and used across the product line. The Deep Diver is not named in any public list, but Satisfyer Connect toys
use the same protocol, and newer models no longer use the "SF" name prefix (Buttplug 5.1.10 handles both).

The main risk is the Bluetooth chip. Intiface's Satisfyer page says Windows and Linux need a Cambridge Silicon Radio (CSR) dongle; other chips
(Intel, Realtek, Broadcom) "may connect but fail to communicate". The owner's PC has built-in Bluetooth and he does not want to buy a dongle,
so the test decides it. Forum reports show some Satisfyer toys working on built-in Windows 11 Bluetooth.

A known bug in early Intiface Central v3 (toy stops responding after a few seconds, missing keepalive) is reported fixed in later versions:
use the newest release.

## The owner's test (corrected steps)

1. Close the Satisfyer Connect app on the phone (the toy pairs with ONE host at a time; afterwards the phone app needs re-pairing).
2. Factory reset the Deep Diver: hold its reset button about 10 seconds (3 vibrations, then 5 quick pulses after letting go; check the manual
   for which button).
3. Windows Settings > Bluetooth: set "Bluetooth devices discovery" to **Advanced**, add a device, pair the toy (it may show as
   "SF Deep Diver", a plain name, or "Unknown Device").
4. Install and open **Intiface Central** (newest version), press Start Server, then Devices > Start Scanning.
5. If the toy appears, move its vibration slider. Leave it running a minute to check it does not stop responding.

Results to report: (a) not found, (b) found but no vibration, (c) works but stops after seconds, (d) works.

## How the bot would control it

- **Server:** Intiface Central (or its headless engine) runs on the same PC, WebSocket `ws://127.0.0.1:12345`, local only.
- **Client:** the official Python package `buttplug` (1.0.0, Feb 2026, Python 3.10+): `ButtplugClient.connect`, `start_scanning`,
  `device.run_output(DeviceOutputCommand(OutputType.VIBRATE, level))`, `device.stop()`. Satisfyer speed is 0-100; after a few seconds at 0
  the toy leaves start mode, which the server handles.
- **Shape in this repo:** a new module (e.g. `toy_control/`) that owns one table (sessions and pattern runs), takes commands only from a
  closed catalogue of patterns in code (never from model output), and is wired in `system/`. No path from the model to the toy.

## Safety and limits to propose with the design

- A DM word (`stop`) and a button stop it at once, always, without consequence; so does the "health or safety" button, a wear exemption,
  a vacation, a runtime halt and quiet hours.
- Hard caps in code: maximum intensity, maximum continuous run (for example 30 minutes, matching the plug-wear rules), a rest after each run.
- Only while a task says the plug is in (it is never started blind), and "Not alone" = lowest settings only (noise).
- Connection lost or Intiface not running = nothing happens and nothing is counted against him.

## Task ideas (for the owner to approve)

- **Overdue buzz:** while the plug is in during a task, short pulses until the photo arrives.
- **Tease during wear tasks:** random pulses through a 60-90 minute wear task while he does something else.
- **Edging pattern:** slow build, stop, build again, ending without release (he stays locked).
- **Reward:** a long pleasant pattern after a verified task or a full training week.
- **Random check:** a pulse at a random free-time moment while a wear task is running; he answers in the DM within a few minutes.

Sources: [Intiface: Satisfyer](https://intiface.com/docs/intiface-central/brands/satisfyer),
[Buttplug: Satisfyer protocol](https://buttplug.io/stpihkal/protocols/satisfyer/),
[keepalive bug thread](https://discuss.buttplug.io/t/only-works-for-a-couple-seconds-after-connecting/869),
[buttplug on PyPI](https://pypi.org/project/buttplug/),
[Buttplug connectors docs](https://buttplug.io/docs/dev-guide/writing-buttplug-applications/connectors),
[Buttplug 5.1.10 changes](https://tangled.org/buttplug.io/buttplug/commits/buttplug-5.1.10).

## Groundwork built (2026-11-16)

`toy_control/` (not wired into the bot): a minimal Buttplug v3 client over aiohttp and the owner's own test,
`python -m cli toy-test --level 0.2 --seconds 2 --yes` (capped at 50% and 10 seconds, always stopped). Intiface Central 3.2.2 is
installed on the PC; the PC's Realtek Bluetooth adapter showed Code 43 on 2026-11-16 and must be fixed first.
