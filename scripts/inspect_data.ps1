param([Parameter(ValueFromRemainingArguments=$true)][string[]]$InspectArgs)
$inspectRoot=Split-Path $PSScriptRoot -Parent
. (Join-Path $PSScriptRoot 'python_runtime.ps1')
& (Get-ContinuumPython) -B (Join-Path $PSScriptRoot 'inspect_data.py') @InspectArgs
exit $LASTEXITCODE
