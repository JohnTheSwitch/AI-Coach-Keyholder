# Restarts the bot WITHOUT a console window (owner, 2026-11-14: a visible "AI Coach Keyholder bot" window kept appearing, and closing it
# stopped the bot). Stops any running `python -m bot.discord_bot` and its start_bot.bat, then starts scripts\start_bot.bat detached hidden
# (Start-Process -WindowStyle Hidden), so no console window is shown. Output still goes to logs\bot.log.
# Usage:  powershell -ExecutionPolicy Bypass -File windows\restart_bot_hidden.ps1

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$bat = Join-Path $repo "scripts\start_bot.bat"
if (-not (Test-Path $bat)) { throw "scripts\start_bot.bat is missing: run python -m scripts.make_start_script first." }

Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'bot\.discord_bot|start_bot\.bat' -and $_.ProcessId -ne $PID } |
    ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -Confirm:$false -ErrorAction Stop } catch {} }
Start-Sleep -Seconds 3

# Owner, 2026-11-16: with the boot task (windows\install_boot_task.ps1) the bot is started through it, so it keeps running after a logoff.
$task = Get-ScheduledTask -TaskName "AI Coach Bot" -ErrorAction SilentlyContinue
if ($task) {
    try { Start-ScheduledTask -TaskName "AI Coach Bot"; Write-Output "Bot started through the boot task; see logs\bot.log."; return } catch {
        Write-Output "The boot task could not be started ($($_.Exception.Message)); starting the bot directly." }
}
# Start-Process -WindowStyle Hidden (2026-11-14: Win32_Process.Create with a Win32_ProcessStartup object returned 21, invalid parameter).
$proc = Start-Process -FilePath "cmd.exe" -ArgumentList "/c `"$bat`"" -WorkingDirectory $repo -WindowStyle Hidden -PassThru
Write-Output "Bot started without a window (process $($proc.Id)); see logs\bot.log."
