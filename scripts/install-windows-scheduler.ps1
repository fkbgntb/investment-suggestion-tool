$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Runner = Join-Path $ProjectRoot "scripts\run_scheduler_once.py"
$TaskName = "InvestmentSuggestionTool-CrawlSources"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Project virtual environment is missing. Run scripts\bootstrap.ps1 first."
}

$Action = New-ScheduledTaskAction `
    -Execute $Python `
    -Argument ('"{0}"' -f $Runner) `
    -WorkingDirectory $ProjectRoot
$Trigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(2) `
    -RepetitionInterval (New-TimeSpan -Hours 3) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "Runs bounded ETF information-source collection every three hours." `
    -Force | Out-Null

Write-Host "Scheduled task installed: $TaskName"
