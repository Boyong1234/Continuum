$continuumScriptRoot=Split-Path $PSScriptRoot -Parent
function Get-ContinuumPython([string]$Environment='backend') {
    $python=Join-Path $continuumScriptRoot "runtime/envs/$Environment/Scripts/python.exe"
    if(-not(Test-Path -LiteralPath $python)){throw "Missing $Environment environment. Run scripts/prepare_source.ps1 using your installed official Python 3.12 x64."}
    return $python
}
