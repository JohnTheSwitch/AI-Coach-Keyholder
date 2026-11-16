# Starts the bot (and Ollama) when Windows boots, not when the owner logs in (owner, 2026-11-16: a Windows Update reboot at 06:44 left the
# bot down until he logged in at 10:19).
#
# Registers two scheduled tasks that run as the current user at system startup, whether or not anyone is logged on ("S4U": no password is
# stored; outbound internet works, Windows file shares would not, and the bot needs none):
#   "AI Coach Ollama"  ollama serve                  (the model server the coach needs)
#   "AI Coach Bot"     scripts\start_bot.bat         (30 s after boot, so the network and Ollama are up)
# Both restart after a failure (3 times, 1 minute apart) and have no time limit. The Startup-folder script AI_Coach_Bot.vbs is moved to
# windows\AI_Coach_Bot.vbs.disabled so the bot does not start twice. The bot also refuses to run twice (single-instance guard).
#
# Run ONCE from an ADMINISTRATOR PowerShell:
#   powershell -ExecutionPolicy Bypass -File C:\Users\jirka\claude\AI-Coach-Keyholder\windows\install_boot_task.ps1
# Undo:
#   powershell -ExecutionPolicy Bypass -File C:\Users\jirka\claude\AI-Coach-Keyholder\windows\install_boot_task.ps1 -Uninstall

param([switch]$Uninstall)
$ErrorActionPreference = "Stop"

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { throw "Run this from an administrator PowerShell (right-click PowerShell, Run as administrator)." }

$repo = Split-Path -Parent $PSScriptRoot
$bat = Join-Path $repo "scripts\start_bot.bat"
$startupVbs = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup\AI_Coach_Bot.vbs"
$disabledVbs = Join-Path $PSScriptRoot "AI_Coach_Bot.vbs.disabled"
$user = "$env:USERDOMAIN\$env:USERNAME"

if ($Uninstall) {
    foreach ($name in "AI Coach Bot", "AI Coach Ollama") {
        if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) { Unregister-ScheduledTask -TaskName $name -Confirm:$false }
    }
    if ((Test-Path $disabledVbs) -and -not (Test-Path $startupVbs)) { Move-Item $disabledVbs $startupVbs }
    Write-Output "Removed the boot tasks; the bot starts at login again."
    return
}

if (-not (Test-Path $bat)) { throw "scripts\start_bot.bat is missing: run py -m scripts.make_start_script first." }
$ollama = (Get-Command ollama -ErrorAction SilentlyContinue).Source
if (-not $ollama) { $ollama = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe" }
if (-not (Test-Path $ollama)) { throw "ollama.exe not found." }

$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType S4U -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew

$ollamaAction = New-ScheduledTaskAction -Execute $ollama -Argument "serve"
Register-ScheduledTask -TaskName "AI Coach Ollama" -Action $ollamaAction -Trigger (New-ScheduledTaskTrigger -AtStartup) `
    -Principal $principal -Settings $settings -Description "Ollama model server for the AI Coach bot, at boot." -Force | Out-Null

$botTrigger = New-ScheduledTaskTrigger -AtStartup
$botTrigger.Delay = "PT30S"
$botAction = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$bat`"" -WorkingDirectory $repo
Register-ScheduledTask -TaskName "AI Coach Bot" -Action $botAction -Trigger $botTrigger `
    -Principal $principal -Settings $settings -Description "AI Coach Keyholder Discord bot, at boot (no login needed)." -Force | Out-Null

if (Test-Path $startupVbs) { Move-Item $startupVbs $disabledVbs -Force }
Write-Output "Done. On the next boot Ollama and the bot start without a login. To start the bot now under the task: Start-ScheduledTask 'AI Coach Bot'"
