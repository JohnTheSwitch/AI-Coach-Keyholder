# Restarts the bot WITHOUT a console window (owner, 2026-11-14: a visible "AI Coach Keyholder bot" window kept appearing, and closing it
# stopped the bot). Stops any running `python -m bot.discord_bot` and its start_bot.bat, then starts scripts\start_bot.bat detached with
# CREATE_NO_WINDOW, so no console (and no Windows Terminal tab) is ever created. Output still goes to logs\bot.log.
# Usage:  powershell -ExecutionPolicy Bypass -File windows\restart_bot_hidden.ps1

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$bat = Join-Path $repo "scripts\start_bot.bat"
if (-not (Test-Path $bat)) { throw "scripts\start_bot.bat is missing: run python -m scripts.make_start_script first." }

Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'bot\.discord_bot|start_bot\.bat' -and $_.ProcessId -ne $PID } |
    ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -Confirm:$false -ErrorAction Stop } catch {} }
Start-Sleep -Seconds 3

$CREATE_NO_WINDOW = 0x08000000
$startup = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ ShowWindow = [uint16]0; CreateFlags = [uint32]$CREATE_NO_WINDOW }
$result = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
    CommandLine = "cmd.exe /c `"$bat`""; CurrentDirectory = $repo; ProcessStartupInformation = $startup }
if ($result.ReturnValue -ne 0) { throw "Starting the bot failed (Win32_Process.Create returned $($result.ReturnValue))." }
Write-Output "Bot started without a window (process $($result.ProcessId)); see logs\bot.log."
