param([Parameter(ValueFromRemainingArguments=$true)][string[]]$MemoryArgs)
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'python_runtime.ps1')
$projectRoot=Split-Path $PSScriptRoot -Parent
Push-Location -LiteralPath $projectRoot
$previousPythonPath=$env:PYTHONPATH
try {
    $env:PYTHONPATH=Join-Path $projectRoot 'backend'
    & (Get-ContinuumPython) -B -m app.services.memory.runtime @MemoryArgs
    $memoryExitCode=$LASTEXITCODE
} finally {
    $env:PYTHONPATH=$previousPythonPath
    Pop-Location
}
exit $memoryExitCode
