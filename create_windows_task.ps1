$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$taskName = "Movie Ticket Discord Monitor"
$scriptPath = Join-Path $PSScriptRoot "run_monitor_windows.ps1"
$powerShell = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"

$action = New-ScheduledTaskAction `
    -Execute $powerShell `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`""

$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 2)

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Checks configured Cineplex pages and sends Discord alerts." `
    -Force

Write-Host "Created scheduled task: $taskName"
Write-Host "It requests a run every 2 minutes while this computer is powered on; overlapping runs are skipped."
