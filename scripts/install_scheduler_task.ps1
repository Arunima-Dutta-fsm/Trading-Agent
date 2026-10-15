# Start the AABA scheduler automatically when you log in to Windows, and restart it if it stops.
#   powershell -ExecutionPolicy Bypass -File scripts\install_scheduler_task.ps1            # install
#   powershell -ExecutionPolicy Bypass -File scripts\install_scheduler_task.ps1 -Remove    # uninstall
#
# The scheduler can only trade while this PC is ON, AWAKE and ONLINE. Windows sleep stops it.
# To keep the PC awake on mains power (sleep on battery is unchanged):
#   powercfg /change standby-timeout-ac 0
# Undo with:  powercfg /change standby-timeout-ac 10
param([switch]$Remove)
$ErrorActionPreference = "Stop"
$name = "AABA Trading Scheduler"
$root = Split-Path -Parent $PSScriptRoot

if ($Remove) {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false
    Write-Host "Removed '$name'."
    exit 0
}

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Minimized -File `"$root\scripts\run_scheduler.ps1`"" `
    -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 5) `
    -ExecutionTimeLimit (New-TimeSpan -Days 0) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings `
    -Description "Runs 'python main.py scheduler' for the AABA trading agent ($root)." | Out-Null
Write-Host "Installed '$name'. It starts at your next log-in; start it now with:"
Write-Host "  Start-ScheduledTask -TaskName '$name'"
