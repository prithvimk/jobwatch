# Registers the laptop detector: detector.sync every 5 min while the laptop is awake, for the current user.
# Re-run to update; remove with:  Unregister-ScheduledTask -TaskName jobwatch-detector -Confirm:$false
# Log: private\logs\sync.log
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$pyw = Join-Path $root ".venv\Scripts\pythonw.exe"   # pythonw: no console window every 5 min
if (-not (Test-Path $pyw)) { throw "Run 'uv sync' first: $pyw not found" }

$action = New-ScheduledTaskAction -Execute $pyw -Argument "-m detector.sync" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 4)
Register-ScheduledTask -TaskName "jobwatch-detector" -Action $action -Trigger $trigger -Settings $settings `
    -Description "jobwatch: poll job boards every 5 min (primary detector; GitHub Actions is the fallback)" -Force | Out-Null
Get-ScheduledTask -TaskName "jobwatch-detector" | Select-Object TaskName, State
