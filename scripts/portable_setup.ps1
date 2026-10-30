param([string]$SourcePython,[switch]$Repair)
$ErrorActionPreference='Stop'
if($Repair){throw 'This source installation depends on installed Python. Rerun scripts/prepare_source.ps1 -Python YOUR_PYTHON after moving computers/directories.'}
& "$PSScriptRoot/prepare_source.ps1" -Python $SourcePython
exit $LASTEXITCODE
