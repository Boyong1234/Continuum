param([string]$Python,[switch]$SkipAstrBot)
$ErrorActionPreference='Stop'
$root=Split-Path $PSScriptRoot -Parent
if(-not $Python){$Python=(& py -3.12 -c 'import sys;print(sys.executable)')}
if(-not $Python -or -not(Test-Path -LiteralPath $Python)){throw 'Install official Windows x64 Python 3.12, or pass -Python C:\path\python.exe'}
& $Python -I -c "import sys,struct;assert sys.version_info[:2]==(3,12) and struct.calcsize('P')==8"
if($LASTEXITCODE -ne 0){throw 'Python 3.12 x64 required'}
foreach($name in @('backend','astrbot')){
 if($name -eq 'astrbot' -and $SkipAstrBot){continue}
 $target=Join-Path $root "runtime/envs/$name"
 & $Python -I -m venv $target
 if($LASTEXITCODE -ne 0){throw "Cannot create $name environment"}
 $p=Join-Path $target 'Scripts/python.exe'
 if($name -eq 'backend'){& $p -m pip install -r (Join-Path $root 'backend/requirements-lock.txt')}
 else{& $p -m pip install -r (Join-Path $root 'release/requirements-astrbot.txt')}
 if($LASTEXITCODE -ne 0){throw "$name dependency preparation failed"}
}
[IO.File]::WriteAllText((Join-Path $root 'runtime/prepared-root.txt'),$root)
Write-Host 'Source environments ready. They require this installed Python; rerun preparation after moving to a different computer.'
