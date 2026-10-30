param(
 [ValidateSet('Start','Stop','Status','Memory','Configure','PrepareMigration','TakeOverMemory','RestoreTask')][string]$Action='Start',
 [string]$ConfigFile,[switch]$ImportExisting,[string]$View,[int]$WaitSeconds=20,[switch]$CoreOnly
)
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'python_runtime.ps1')
$root=Split-Path $PSScriptRoot -Parent
if(-not $ConfigFile){$ConfigFile=Join-Path $root 'data/portable/config.json'}
elseif(-not [IO.Path]::IsPathRooted($ConfigFile)){$ConfigFile=Join-Path $root $ConfigFile}
$ConfigFile=[IO.Path]::GetFullPath($ConfigFile)
if(-not $ConfigFile.StartsWith($root+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)){throw 'Private configuration must stay inside the project folder'}
$private=Split-Path $ConfigFile -Parent
$stateFile=Join-Path $private 'processes.json'
$utf8=New-Object System.Text.UTF8Encoding($false)
function LocalPath([string]$Relative){
 $path=[IO.Path]::GetFullPath((Join-Path $root $Relative))
 if(-not $path.StartsWith($root+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)){throw 'Project data must stay inside this folder'}
 return $path
}
function ReadEnv([string]$Path){
 $values=@{}
 if(Test-Path -LiteralPath $Path){foreach($line in [IO.File]::ReadAllLines($Path)){if($line -match '^([A-Z][A-Z0-9_]*)=(.*)$'){$values[$matches[1]]=$matches[2]}}}
 return $values
}
function SaveJson($Path,$Value){[IO.File]::WriteAllText($Path,($Value|ConvertTo-Json -Depth 8),$utf8)}
function LoadConfig {
 if(Test-Path -LiteralPath $ConfigFile){$c=Get-Content -LiteralPath $ConfigFile -Raw -Encoding UTF8|ConvertFrom-Json}
 else{
  $c=[pscustomobject]@{Dataset='data/user/v7';AstrBotRoot='data/user/astrbot';GatewayEnv='data/user/gateway.env';MemoryConfig='data/portable/memory.json';BackendPort=8010;OneBotPort=6199;SnowLumaUrl='http://127.0.0.1:5099';ModelConfig='deepseek';AutoMemorySync=$false}
 }
 foreach($name in @('Dataset','AstrBotRoot','GatewayEnv','MemoryConfig')){[void](LocalPath $c.$name)}
 if($c.BackendPort -lt 1024 -or $c.BackendPort -gt 65535 -or $c.OneBotPort -lt 1024 -or $c.OneBotPort -gt 65535 -or $c.BackendPort -eq $c.OneBotPort){throw 'Invalid local ports'}
 return $c
}
function Configure($c){
 $old=@{}
 $secrets=ReadEnv (Join-Path $private 'secrets.env')
 $base=ReadEnv (Join-Path $root '.env')
 if(-not $secrets['DEEPSEEK_API_KEY']){$secrets['DEEPSEEK_API_KEY']=$base['DEEPSEEK_API_KEY']}
 $gateway=ReadEnv (LocalPath $c.GatewayEnv)
 foreach($key in $old.Keys){if(-not $gateway[$key]){$gateway[$key]=$old[$key]}}
 if(-not $ImportExisting){
  $bot=Read-Host 'Bot QQ (small account; blank keeps existing)'
  $owner=Read-Host 'Owner QQ (allowed sender; blank keeps existing)'
  if($bot){$gateway['CONTINUUM_GATEWAY_ACCOUNT_ID']=$bot}
  if($owner){$gateway['CONTINUUM_GATEWAY_SENDER_ID']=$owner}
  $secure=Read-Host 'DeepSeek API Key (hidden; blank keeps existing)' -AsSecureString
  if($secure.Length){$ptr=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure);try{$secrets['DEEPSEEK_API_KEY']=[Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)}finally{[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)}}
 }
 $bot=$gateway['CONTINUUM_GATEWAY_ACCOUNT_ID'];$owner=$gateway['CONTINUUM_GATEWAY_SENDER_ID']
 if($bot -notmatch '^[1-9][0-9]{4,19}$' -or $owner -notmatch '^[1-9][0-9]{4,19}$' -or $bot -eq $owner){throw 'Two distinct QQ identities are required'}
 if($c.ModelConfig -ne 'mock' -and (-not $secrets['DEEPSEEK_API_KEY'] -or $secrets['DEEPSEEK_API_KEY'] -match '[\r\n]')){throw 'DeepSeek key is required'}
 foreach($key in @('CONTINUUM_BRIDGE_TOKEN','CONTINUUM_GATEWAY_ADMIN_TOKEN')){
  if(-not $gateway[$key]){$bytes=New-Object byte[] 48;$rng=[Security.Cryptography.RandomNumberGenerator]::Create();$rng.GetBytes($bytes);$rng.Dispose();$gateway[$key]=([Convert]::ToBase64String($bytes)).TrimEnd('=').Replace('+','-').Replace('/','_')}
 }
 $gateway['CONTINUUM_GATEWAY_ENABLED']='true';$gateway['CONTINUUM_GATEWAY_MODEL_CONFIG']=$c.ModelConfig
 $gateway['CONTINUUM_BRIDGE_URL']="http://127.0.0.1:$($c.BackendPort)";$gateway['ASTRBOT_BRIDGE_PLATFORM_ID']='continuum-onebot'
 New-Item -ItemType Directory -Path $private -Force|Out-Null
 New-Item -ItemType Directory -Path (Split-Path (LocalPath $c.GatewayEnv) -Parent) -Force|Out-Null
 [IO.File]::WriteAllLines((Join-Path $private 'secrets.env'),@($secrets.GetEnumerator()|ForEach-Object{"$($_.Key)=$($_.Value)"}),$utf8)
 [IO.File]::WriteAllLines((LocalPath $c.GatewayEnv),@($gateway.GetEnumerator()|ForEach-Object{"$($_.Key)=$($_.Value)"}),$utf8)
 SaveJson $ConfigFile $c
 Write-Host 'Private configuration saved. Anyone holding this folder may read its key and private history.'
 Write-Host 'Key/account changes apply after normal stop/start. Existing external processes are not restarted.'
}
function FindComponent($Name,$c){
 $marker=switch($Name){backend{'start_continuum_gateway.py'} astrbot{'start_astrbot_gateway.py'} memory{'app.services.memory.runtime'}}
 $path=switch($Name){backend{LocalPath $c.Dataset} astrbot{LocalPath $c.AstrBotRoot} memory{Join-Path (LocalPath $c.Dataset) 'continuum.db'}}
 $rows=@(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {$_.CommandLine -and $_.CommandLine.Contains($marker) -and $_.CommandLine.Replace('/','\').ToLower().Contains($path.Replace('/','\').ToLower()) -and ($Name -ne 'memory' -or $_.CommandLine.Contains('--watch'))})
 $parents=@($rows|ForEach-Object{$_.ParentProcessId})
 return @($rows|Where-Object{$_.ProcessId -notin $parents})
}
function Health($c){
 $gateway=ReadEnv (LocalPath $c.GatewayEnv)
 try {return Invoke-RestMethod -Uri "http://127.0.0.1:$($c.BackendPort)/api/channels/bridge/health" -Headers @{Authorization=('Bearer '+$gateway['CONTINUUM_BRIDGE_TOKEN'])} -TimeoutSec 2}
 catch {return $null}
}
function LoadState {
 if(Test-Path -LiteralPath $stateFile){$s=Get-Content -LiteralPath $stateFile -Raw -Encoding UTF8|ConvertFrom-Json;if($s.Root -eq $root){return $s}}
 return [pscustomobject]@{Root=$root;Processes=@()}
}
function StartComponent($Name,$Python,$Arguments,$Stop,$c){
 $existing=@(FindComponent $Name $c)
 if($existing.Count){Write-Host "$Name already running; no duplicate created";return}
 if(Test-Path -LiteralPath $Stop){Remove-Item -LiteralPath $Stop}
 $quoted=($Arguments|ForEach-Object{if($_ -match '\s'){'"'+$_+'"'}else{$_}}) -join ' '
 $p=Start-Process -FilePath $Python -ArgumentList $quoted -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput "$private/$Name.stdout" -RedirectStandardError "$private/$Name.stderr" -PassThru
 $script:state.Processes=@($script:state.Processes|Where-Object{$_.Name -ne $Name})+@([pscustomobject]@{Name=$Name;Pid=$p.Id;Started=$p.StartTime.ToUniversalTime().ToString('o');Stop=$Stop})
 SaveJson $stateFile $script:state
 Write-Host "$Name started (launcher $($p.Id))"
}
function StopOwned($s){
 foreach($name in @('memory','backend','astrbot')){
  foreach($entry in @($s.Processes|Where-Object{$_.Name -eq $name})){
   $p=Get-Process -Id $entry.Pid -ErrorAction SilentlyContinue
   if(-not $p -or $p.StartTime.ToUniversalTime().ToString('o') -ne $entry.Started){continue}
   if($name -eq 'backend'){
    $dbPath=Join-Path (LocalPath $config.Dataset) 'continuum.db'
    $check="from pathlib import Path;import sqlite3,sys;c=sqlite3.connect(Path(sys.argv[1]).as_uri()+'?mode=ro',uri=True);print(c.execute('SELECT count(*) FROM channel_turns WHERE state IN (?,?,?)',('open','queued','generating')).fetchone()[0])"
    $deadline=(Get-Date).AddSeconds(30)
    do{$busy=& (Get-ContinuumPython) -B -c $check $dbPath;if($LASTEXITCODE -ne 0){throw 'Cannot confirm chat is idle'};if([int]$busy -eq 0){break};Start-Sleep -Milliseconds 250}while((Get-Date) -lt $deadline)
    if([int]$busy -gt 0){throw 'Chat is still in progress; backend left running. Stop again after the reply.'}
   }
   if(-not $entry.Stop.StartsWith($root+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)){throw 'Stop file outside current folder'}
   [IO.File]::WriteAllText($entry.Stop,'Portable normal stop',$utf8)
   $deadline=(Get-Date).AddSeconds(40)
   while((Get-Process -Id $entry.Pid -ErrorAction SilentlyContinue) -and (Get-Date) -lt $deadline){Start-Sleep -Milliseconds 250}
   if(Get-Process -Id $entry.Pid -ErrorAction SilentlyContinue){throw "$name still finishing; no forced termination performed"}
   Write-Host "$name stopped"
  }
 }
}
function ShowStatus($c){
 Write-Host "Project: $root";Write-Host ('Database: '+(Join-Path (LocalPath $c.Dataset) 'continuum.db'))
 Write-Host ('Official QQ process present: '+(@(Get-Process QQ -ErrorAction SilentlyContinue).Count -gt 0))
 foreach($name in @('backend','astrbot','memory')){Write-Host ("{0} process: {1}" -f $name,(@(FindComponent $name $c).Count -gt 0))}
 $health=Health $c
 if($health){foreach($layer in $health.layers.PSObject.Properties){Write-Host ("{0}: {1}" -f $layer.Name,$layer.Value.status)}}else{Write-Host 'Backend health unavailable (not running, port or authentication mismatch).'}
 $raw=& (Get-ContinuumPython) -B (Join-Path $PSScriptRoot 'inspect_data.py') --db (Join-Path (LocalPath $c.Dataset) 'continuum.db') memory-status
 if($LASTEXITCODE -ne 0){throw 'Read-only memory status failed'}
 $memory=($raw -join "`n")|ConvertFrom-Json
 [pscustomobject]@{MemoryWatch=$memory.watch_running;Paused=$memory.paused;Batch=$memory.batch_state;LastProcessedUTC=$memory.last_processed_at;PendingMessages=$memory.new_messages;PendingSegments=$memory.pending_segments;TodayExtractionTokens=$memory.today.api_tokens;LatestJobStatus=$memory.latest_job.status;LatestJobError=$memory.latest_job.last_error}|Format-List
}
try {
 $config=LoadConfig
 $preparedRoot=Join-Path $root 'runtime/prepared-root.txt'
 if($Action -in @('Start','Status','Memory') -and (Test-Path -LiteralPath $preparedRoot) -and [IO.File]::ReadAllText($preparedRoot) -ne $root){
  if(-not(Test-Path (Join-Path $root 'runtime/python/python.exe'))){throw 'Source environment moved: rerun scripts/prepare_source.ps1 with the installed Python on this computer.'}
  & "$PSScriptRoot/portable_setup.ps1" -Repair
  if($LASTEXITCODE -ne 0){throw 'Runtime relocation repair failed'}
 }
 if($Action -eq 'Configure'){Configure $config;exit 0}
 if($Action -eq 'Status'){ShowStatus $config;exit $LASTEXITCODE}
 if($Action -eq 'Memory'){
  if(-not $View){Write-Host '1 overview / 2 recent / 3 episodes / 4 core / 5 memory-status / 6 search';$choice=Read-Host 'Choose';$View=@{'1'='overview';'2'='recent';'3'='episodes';'4'='core';'5'='memory-status';'6'='search'}[$choice]}
  if($View -notin @('overview','recent','episodes','core','memory-status','search')){throw 'Unknown memory view'}
  $argsList=@('--db',(Join-Path (LocalPath $config.Dataset) 'continuum.db'),$View)
  if($View -eq 'search'){$argsList+=@('--query',(Read-Host 'Local search text'))}
  & (Get-ContinuumPython) -B (Join-Path $PSScriptRoot 'inspect_data.py') @argsList;exit $LASTEXITCODE
 }
 $script:state=LoadState
 if($Action -in @('TakeOverMemory','RestoreTask')){
  $task=Get-ScheduledTask -TaskName 'Continuum-MemorySync' -ErrorAction SilentlyContinue
  New-Item -ItemType Directory -Path $private -Force|Out-Null
  if($Action -eq 'RestoreTask'){
   $xml=Get-Content "$private/memory-task.xml" -Raw -Encoding UTF8
   Register-ScheduledTask -TaskName 'Continuum-MemorySync' -Xml $xml -Force|Out-Null
   Write-Host 'Original login task restored. Do not run another portable watch.';exit 0
  }
  if($task){
   $expectedTaskScript=Join-Path $root 'scripts/auto_memory.ps1'
   if(-not ([string]$task.Actions.Arguments).Replace('/','\').ToLower().Contains($expectedTaskScript.Replace('/','\').ToLower())){throw 'The login task belongs to another folder; review it before switching'}
   if(-not(Test-Path "$private/memory-task.xml")){[IO.File]::WriteAllText("$private/memory-task.xml",(Export-ScheduledTask -TaskName $task.TaskName),$utf8)}
   Disable-ScheduledTask -TaskName $task.TaskName|Out-Null
  }
  $watch=@(FindComponent memory $config)
  if($watch.Count -gt 1){throw 'More than one watch; review before taking over'}
  if($watch.Count){
   if($watch[0].CommandLine -notmatch '--stop-file\s+(?:"([^"]+)"|(\S+))'){throw 'Watch has no controllable stop file'}
   $stop=if($matches[1]){$matches[1]}else{$matches[2]}
   $p=Get-Process -Id $watch[0].ProcessId
   $script:state.Processes=@($script:state.Processes|Where-Object{$_.Name -ne 'memory'})+@([pscustomobject]@{Name='memory';Pid=$p.Id;Started=$p.StartTime.ToUniversalTime().ToString('o');Stop=$stop.Replace('/','\')})
   SaveJson $stateFile $script:state
  }
  Write-Host 'Memory management transferred explicitly; task XML preserved. No process restarted.';exit 0
 }
 if($Action -in @('Stop','PrepareMigration')){
  StopOwned $script:state
  $left=@();foreach($name in @('backend','astrbot','memory')){$left+=@(FindComponent $name $config)}
  if($left.Count){throw 'Externally managed components remain. They were not stopped; use the existing operations procedure before copying data.'}
  if($Action -eq 'PrepareMigration'){
   $legacy=Get-ScheduledTask -TaskName 'Continuum-MemorySync' -ErrorAction SilentlyContinue
   if($legacy -and $legacy.State -ne 'Disabled' -and ([string]$legacy.Actions.Arguments).Contains($root)){throw 'Transfer/disable the legacy login task explicitly before preparing this dataset for migration'}
   $destination=Join-Path $root ('data/backups/portable-'+[guid]::NewGuid().ToString())
   & "$PSScriptRoot/maintenance.ps1" backup --database (Join-Path (LocalPath $config.Dataset) 'continuum.db') --persona (Join-Path (LocalPath $config.Dataset) 'persona') --destination $destination --allow-plaintext
   if($LASTEXITCODE -ne 0){throw 'Migration backup failed'}
   & "$PSScriptRoot/maintenance.ps1" verify --backup $destination
   if($LASTEXITCODE -ne 0){throw 'Migration backup verification failed'}
   Write-Host 'All application writers stopped. Preserve the entire private folder, imports, Persona and all existing WAL sidecars. Do not copy external QQ accounts.'
   Write-Host "Verified migration backup: $destination . See portable operations documentation."
  }
  exit 0
 }
 if(-not(Test-Path (Join-Path (LocalPath $config.Dataset) 'continuum.db')) -and $config.Dataset -eq 'data/user/v7'){
  & (Get-ContinuumPython) -B "$PSScriptRoot/initialize_release.py" --dataset $config.Dataset --backend-port "$($config.BackendPort)" --onebot-port "$($config.OneBotPort)"
  if($LASTEXITCODE -ne 0){throw 'Empty initialization failed'}
  $config=LoadConfig
 }
 if(-not(Test-Path -LiteralPath $ConfigFile) -or -not (ReadEnv (LocalPath $config.GatewayEnv))['CONTINUUM_GATEWAY_SENDER_ID']){Configure $config}
 foreach($key in (ReadEnv (Join-Path $private 'secrets.env')).GetEnumerator()){[Environment]::SetEnvironmentVariable($key.Key,$key.Value,'Process')}
 $prepared=Join-Path $root 'runtime/prepared-root.txt'
 if(-not(Test-Path -LiteralPath $prepared)){throw 'Prepare dependencies first: scripts/prepare_source.ps1 for source installs, or portable_setup.ps1 for an existing bundled runtime.'}
 if(([IO.File]::ReadAllText($prepared)) -ne $root){& "$PSScriptRoot/portable_setup.ps1" -Repair}
 if(-not(Test-Path (Join-Path (LocalPath $config.Dataset) 'continuum.db'))){throw 'Private database missing; refusing to create an empty archive'}
 if(-not(Test-Path (Join-Path $root 'frontend/dist/index.html'))){Write-Host 'Optional Web UI not built; QQ and backend API remain available. See README frontend build step.'}
 if(-not $CoreOnly){
  $astr=Get-Content (Join-Path (LocalPath $config.AstrBotRoot) 'data/cmd_config.json') -Raw -Encoding UTF8|ConvertFrom-Json
  if($astr.platform[0].ws_reverse_port -ne $config.OneBotPort){throw 'AstrBot OneBot port differs from private configuration'}
 }
 New-Item -ItemType Directory -Path $private -Force|Out-Null
 $stopRoot=LocalPath $config.Dataset
 foreach($port in @($config.BackendPort,$config.OneBotPort)){
  $name=if($port -eq $config.BackendPort){'backend'}else{'astrbot'}
  if((Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue) -and -not @(FindComponent $name $config).Count){throw "Port $port belongs to another process; nothing replaced"}
 }
 StartComponent backend (Get-ContinuumPython) @('-B',"$root/scripts/start_continuum_gateway.py",'--dataset',(LocalPath $config.Dataset),'--env-file',(LocalPath $config.GatewayEnv),'--port',"$($config.BackendPort)",'--stop-file',"$stopRoot/portable-backend.stop") "$stopRoot/portable-backend.stop" $config
 $deadline=(Get-Date).AddSeconds($WaitSeconds)
 while(-not(Health $config) -and (Get-Date) -lt $deadline){Start-Sleep -Milliseconds 250}
 if(-not(Health $config)){throw "Backend failed readiness; inspect $private/backend.stderr"}
 if($CoreOnly){Write-Host 'Core-only mode: no AstrBot/QQ/Memory watch started.';ShowStatus $config;exit 0}
 StartComponent astrbot (Get-ContinuumPython astrbot) @('-B',"$root/scripts/start_astrbot_gateway.py",'--root',(LocalPath $config.AstrBotRoot),'--env-file',(LocalPath $config.GatewayEnv),'--stop-file',"$stopRoot/portable-astrbot.stop") "$stopRoot/portable-astrbot.stop" $config
 $deadline=(Get-Date).AddSeconds($WaitSeconds)
 while(-not(Get-NetTCPConnection -State Listen -LocalPort $config.OneBotPort -ErrorAction SilentlyContinue) -and (Get-Date) -lt $deadline){Start-Sleep -Milliseconds 250}
 if(-not(Get-NetTCPConnection -State Listen -LocalPort $config.OneBotPort -ErrorAction SilentlyContinue)){throw "AstrBot failed readiness; inspect $private/astrbot.stderr"}
 $env:PYTHONPATH=Join-Path $root 'backend'
 $task=Get-ScheduledTask -TaskName 'Continuum-MemorySync' -ErrorAction SilentlyContinue
 if(($config.PSObject.Properties.Name -notcontains 'AutoMemorySync' -or $config.AutoMemorySync) -and -not @(FindComponent memory $config).Count){
  if($task -and $task.State -ne 'Disabled' -and ([string]$task.Actions.Arguments).Contains($root)){Write-Host 'Login task still enabled. Use -Action TakeOverMemory explicitly before starting a portable watch.'}
  else{StartComponent memory (Get-ContinuumPython) @('-B','-m','app.services.memory.runtime','--database',"$stopRoot/continuum.db",'sync','--config',(LocalPath $config.MemoryConfig),'--watch','--auto-memory-sync','--interval-hours','24','--min-new-messages','30','--max-batches','1','--stop-file',"$stopRoot/portable-memory.stop") "$stopRoot/portable-memory.stop" $config}
 }
 $health=Health $config
 if($health.layers.qq_session.status -ne 'online'){
  $qqPaths=@($config.QQExecutable,"$env:ProgramFiles/Tencent/QQNT/QQ.exe","${env:ProgramFiles(x86)}/Tencent/QQNT/QQ.exe","$env:LOCALAPPDATA/Programs/Tencent/QQNT/QQ.exe")
  $qq=@($qqPaths|Where-Object{$_ -and (Test-Path -LiteralPath $_)})
  if(-not $qq.Count){Write-Host 'Official NTQQ not found. Install it from https://im.qq.com/ or set QQExecutable in the private config.'}
  elseif(-not @(Get-Process QQ -ErrorAction SilentlyContinue).Count){Start-Process -FilePath $qq[0] -WindowStyle Normal|Out-Null}
  Write-Host "QQ is not confirmed online. Open official NTQQ, scan its login QR, then start/load the permitted official SnowLuma instance. WebUI: $($config.SnowLumaUrl)"
  Write-Host "SnowLuma reverse WS must point to ws://127.0.0.1:$($config.OneBotPort)/ws with the existing AstrBot token. No SnowLuma deployment or injection was automated."
 }
 ShowStatus $config
} catch {
 Write-Host ('Portable action failed: '+$_.Exception.Message)
 exit 1
}
