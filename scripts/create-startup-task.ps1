param(
    [switch]$CreateOnly
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$TaskName = "MySecondBrain"
$StartScript = Join-Path $PSScriptRoot "start-background.ps1"
$Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$StartScript`" -Quiet"
$Trigger = New-ScheduledTaskTrigger -AtLogOn
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries

try {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
} catch {}

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Description "Start My Second Brain backend+frontend in background at login" | Out-Null

Write-Host "Created scheduled task '$TaskName'."
Write-Host "You can now open http://127.0.0.1:8000 after login without rerunning .bat."

if (-not $CreateOnly) {
    & $StartScript
}

