param([Parameter(ValueFromRemainingArguments=$true)][string[]]$MaintenanceArgs)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'python_runtime.ps1')
$projectRoot = Split-Path $PSScriptRoot -Parent
Push-Location -LiteralPath $projectRoot
$previousPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = Join-Path $projectRoot 'backend'
    & (Get-ContinuumPython) -B -m app.maintenance @MaintenanceArgs
    $maintenanceExitCode = $LASTEXITCODE
} finally {
    $env:PYTHONPATH = $previousPythonPath
    Pop-Location
}
exit $maintenanceExitCode
