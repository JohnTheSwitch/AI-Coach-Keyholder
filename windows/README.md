# Windows deployment — Task Scheduler

Optional. The bot runs perfectly well started manually (`py -m
bot.discord_bot`, per the root `README.md`'s own "Running" section) —
these three scripts exist only to make it start automatically without
you having to open a terminal every time you log on to Windows.

Nothing here is required to develop or manually run the bot. Nothing
here is imported by, or changes the behavior of, any part of the
application itself — this is deployment tooling, not application code.

## Files

- **`common.ps1`** — shared `Find-PythonInterpreter` function, used by
  both scripts below. Not run directly.
- **`run_bot.ps1`** — the actual launcher. Derives the project root
  from its own location (never a hardcoded path), uses whichever
  Python interpreter it's told about via `-PythonPath` (or detects one
  itself for a manual run — see "Interpreter detection" below), and
  appends timestamped output to `..\logs\bot.log`.
- **`install_task.ps1`** — registers a Windows Scheduled Task that
  runs `run_bot.ps1` whenever you log on, with a real interpreter's
  absolute path already baked into the Task's own Action.
- **`uninstall_task.ps1`** — removes that Scheduled Task.

None of these four scripts contain a token, a path specific to any
one person's user account, or anything else that shouldn't be shared —
they're safe to commit.

## Interpreter detection — why this exists

**Found under real-world use, not theorized:** on one real Windows
install, neither `py` nor `python` resolved to a working interpreter
at all. Windows' own "App Execution Alias" feature had intercepted
both — near-empty stub executables under
`...\WindowsApps\python.exe`/`python3.exe`, installed by Windows
itself, that open a "choose an app"/Microsoft Store prompt instead of
running Python whenever no real interpreter has been installed through
a path that takes priority over that stub. The Scheduled Task
registered fine, showed `Ready`, but `Start-ScheduledTask` produced
`LastTaskResult = 1` and the log stopped right after "Starting bot via
py -m bot.discord_bot" — the actual launch line was never reached.

`install_task.ps1` now detects a real, working interpreter itself
(`common.ps1`'s `Find-PythonInterpreter`) **at install time** and
bakes its absolute path directly into the Task's Action — the Task
never depends on `py`/`python` resolving correctly at run time, on any
machine. Detection order: this project's own `.venv\Scripts\python.exe`
first (if one exists — guarantees the right dependencies, not just "a"
Python), then every `py`/`python`/`python3` match on `PATH`, skipping
anything under `WindowsApps` or implausibly small to be a real
interpreter. `install_task.ps1` prints exactly what it found:

```
Using Python:
  C:\Users\you\AppData\Local\Python\bin\python.exe
```

If this ever prints nothing and the script throws instead, it means no
real interpreter could be found anywhere it checked — install Python
from [python.org](https://python.org) with "Add to PATH" checked, or
create a `.venv` in the project root, then run `install_task.ps1`
again.

`run_bot.ps1` itself still works fine run manually, with no
`-PythonPath` given — it runs the same detection itself in that case.

## Install

**Open PowerShell as Administrator** (right-click PowerShell or
Windows Terminal, choose "Run as Administrator") **in the `windows\`
folder** (or use its full path) and run:

```powershell
.\install_task.ps1
```

Elevation is required — found necessary under real-world use, not
assumed: `Register-ScheduledTask` needs it even for a task that only
ever runs as your own account with no stored password. The script
checks for this itself and fails with a clear message if you forgot,
rather than letting `Register-ScheduledTask` fail deeper in with a
less obvious error.

This registers a Scheduled Task named `AICoachKeyholderBot` that
starts the next time you log on. To give it a different name (e.g. if
you're running more than one instance):

```powershell
.\install_task.ps1 -TaskName "MyBotName"
```

If PowerShell refuses to run the script at all
(`... cannot be loaded because running scripts is disabled...`), that's
Windows' default script-execution policy, not a problem with this
script — either run PowerShell once as:

```powershell
powershell -ExecutionPolicy Bypass -File .\install_task.ps1
```

or set your own execution policy for your user account
(`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`) if you're
comfortable with that as a general setting.

## Start it right now (without logging off/on)

```powershell
Start-ScheduledTask -TaskName "AICoachKeyholderBot"
```

## Stop it

```powershell
Stop-ScheduledTask -TaskName "AICoachKeyholderBot"
```

This stops the current run; the Scheduled Task itself remains
registered and will start again next logon (or the next time you run
`Start-ScheduledTask`).

## Uninstall

**Also needs an elevated (Administrator) PowerShell session** — same
reason as install. Checked the same way, before removal is attempted.

```powershell
.\uninstall_task.ps1
```

Removes the Scheduled Task. If a launched bot process happens to still
be running at that moment, this does not kill it — check Task Manager
if you need to stop it immediately too.

## Check logs

```powershell
Get-Content ..\logs\bot.log -Tail 50 -Wait
```

(`-Wait` follows the file live, like `tail -f`.) `logs\` is
git-ignored — see the root `.gitignore`; this directory is created
automatically by `run_bot.ps1` the first time it runs.

You can also check the Task itself from Task Scheduler's own GUI
(`taskschd.msc`) or:

```powershell
Get-ScheduledTaskInfo -TaskName "AICoachKeyholderBot"
```

## Manual runs during development

**Recommended, robust command:**

```powershell
.\run_bot.ps1
```

Runs the same interpreter-detection logic `install_task.ps1` uses (no
Scheduled Task needs to be installed for this — `run_bot.ps1` works
standalone), so it works even on a machine where `py`/`python` don't
(see "Interpreter detection" above — this is not a hypothetical
concern, it's what this whole document exists to work around).

`python -m bot.discord_bot`/`py -m bot.discord_bot`, as `README.md`'s
own "Running" section shows, still work too, **if** `python`/`py`
actually resolve to a real interpreter on your machine — they're
simpler for a quick one-off run, but `.\run_bot.ps1` is the one to
reach for by default on Windows, and the one to use if either of those
opens a "choose an app" prompt instead of starting the bot.

## AtLogOn vs. independent of logon — and why AtLogOn is the default

Windows Task Scheduler supports two fundamentally different modes for
who a task runs as:

- **"Run only when user is logged on" (`AtLogOn`, this project's
  default):** the task starts using your own already-active Windows
  session. No password is stored anywhere — Task Scheduler doesn't
  need one, since it's reusing your existing logged-on session. The
  bot stops if you log off. Simpler, and there is no credential of any
  kind sitting in Task Scheduler's own storage for anything to find.
- **"Run whether user is logged on or not":** the task can start even
  before anyone logs in (e.g. right after a reboot, unattended).
  Requires Task Scheduler to store a **password** for the account it
  runs as, in its own protected credential store — real, if modest,
  additional attack surface, and one more secret that has to be
  managed and rotated if it's ever suspected of being compromised.

**Default here: `AtLogOn`.** For a personal bot on a personal Windows
machine — exactly this project's actual situation — the practical
downside (the bot isn't running before you've logged in) is a small
price for not having a stored Windows credential anywhere. If this
project ever runs unattended on a machine nobody regularly logs into
(a dedicated home server, for instance), "whether user is logged on or
not" becomes the more appropriate choice — that's a deliberate,
separate decision to make at that point, not something to default into
here.

## Auto-restart after a crash

`install_task.ps1` configures Task Scheduler's own restart policy (up
to 3 attempts, 1 minute apart) — if the bot process exits unexpectedly,
Windows restarts it automatically without any custom supervisor script
needed. `run_bot.ps1` also sets an unlimited execution time limit
(`ExecutionTimeLimit`) — Task Scheduler's own default is 3 days, which
would otherwise silently kill a long-running bot process that Task
Scheduler assumes is a stuck batch job.

## Plain batch launcher (`scripts/start_bot.bat`)

For Task Scheduler setups that prefer a `.bat`, or a shortcut in the `shell:startup` folder: `python -m scripts.make_start_script`
generates `scripts/start_bot.bat` with ABSOLUTE paths (`cd /d` into the repository, then the interpreter that ran the generator with
`-m bot.discord_bot`). Regenerate it after moving the repository or changing the Python installation. Use either this or the scheduled
task from `install_task.ps1`, never both at once.
