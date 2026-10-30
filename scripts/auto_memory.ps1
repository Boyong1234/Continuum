param([ValidateSet('Status','Start','Stop','Pause','Resume','Once')][string]$Action='Status',[string]$Dataset,[string]$ConfigPath)
$ErrorActionPreference='Stop'
$r=Split-Path $PSScriptRoot -Parent
. (Join-Path $PSScriptRoot 'python_runtime.ps1')
$portableConfig=Join-Path $r 'data/portable/config.json'
if(Test-Path -LiteralPath $portableConfig){
 $portable=Get-Content $portableConfig -Raw -Encoding UTF8|ConvertFrom-Json
 if(-not $Dataset){$Dataset=Join-Path $r $portable.Dataset}
 if(-not $ConfigPath){$ConfigPath=Join-Path $r $portable.MemoryConfig}
 foreach($line in [IO.File]::ReadAllLines((Join-Path $r 'data/portable/secrets.env'))){if($line -match '^DEEPSEEK_API_KEY=(.*)$'){$env:DEEPSEEK_API_KEY=$matches[1]}}
}
$w="$r/data/portable"
if(Test-Path -LiteralPath $portableConfig){$w=Split-Path $ConfigPath -Parent}
if(-not $Dataset){$Dataset="$r/data/user/v7"}
if(-not $ConfigPath){$ConfigPath="$w/memory.json"}
$db=[IO.Path]::GetFullPath((Join-Path $Dataset 'continuum.db'))
$stop="$w/watch.stop"
function Get-Watch { @(Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*app.services.memory.runtime*' -and $_.CommandLine -like '*--watch*' -and $_.CommandLine.Replace('/','\').ToLower().Contains($db.Replace('/','\').ToLower()) }) }
function Start-Watch {
 if((Get-Watch).Count -gt 0){Write-Output 'Memory watch already running';return}
 New-Item -ItemType Directory -Path $w -Force|Out-Null
 if(Test-Path -LiteralPath $stop){Remove-Item -LiteralPath $stop}
 $old=$env:PYTHONPATH
 try{
  $env:PYTHONPATH="$r/backend"
  $watchArgs=@('-B','-m','app.services.memory.runtime','--database',$db,'sync','--config',$ConfigPath,'--watch','--auto-memory-sync','--interval-hours','24','--min-new-messages','30','--max-batches','1','--stop-file',$stop)
  $p=Start-Process -FilePath (Get-ContinuumPython) -ArgumentList (($watchArgs|ForEach-Object{if($_ -match '\s'){'"'+$_+'"'}else{$_}}) -join ' ') -WorkingDirectory $r -WindowStyle Hidden -RedirectStandardOutput "$w/watch.stdout" -RedirectStandardError "$w/watch.stderr" -PassThru
  Write-Output "Memory watch launcher $($p.Id)"
 }finally{$env:PYTHONPATH=$old}
}
function Stop-Watch {
 & "$r/scripts/memory.ps1" --database $db pause
 foreach($watch in (Get-Watch)){
  if($watch.CommandLine -match '--stop-file\s+(?:"([^"]+)"|(\S+))'){
   $watchStop=if($matches[1]){$matches[1]}else{$matches[2]}
   $watchStop=[IO.Path]::GetFullPath($watchStop)
   if(-not $watchStop.StartsWith($r+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)){throw 'Watch stop file outside project'}
   Set-Content -LiteralPath $watchStop -Value 'Explicit automatic memory stop'
  }
 }
 for($i=0;$i -lt 36;$i++){if((Get-Watch).Count -eq 0){return};Start-Sleep -Seconds 1}
 throw 'Watch still finishing; do not launch another instance'
}
if($Action -eq 'Status'){
 Get-Watch | Select-Object ProcessId,ParentProcessId,CommandLine
 & "$r/scripts/memory.ps1" --database $db status
}elseif($Action -eq 'Start'){Start-Watch}
elseif($Action -eq 'Stop'){Stop-Watch}
elseif($Action -eq 'Pause'){& "$r/scripts/memory.ps1" --database $db pause}
elseif($Action -eq 'Resume'){
 & "$r/scripts/memory.ps1" --database $db resume
 if($LASTEXITCODE -ne 0){throw 'Resume rejected by quality gate'}
 Start-Watch
}else{
 $wasRunning=(Get-Watch).Count -gt 0
 Stop-Watch
 & "$r/scripts/memory.ps1" --database $db sync --config $ConfigPath --max-batches 1
 if($LASTEXITCODE -eq 0 -and $wasRunning){& "$r/scripts/memory.ps1" --database $db resume;Start-Watch}
}
