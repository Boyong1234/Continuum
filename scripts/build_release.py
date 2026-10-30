"""Explicit source allowlist; private continuity uses the existing verified backup."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'backend'),str(ROOT/'scripts')]


def copy_file(source,target,manifest):
    target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,target)
    manifest.append(dict(path=target.relative_to(OUTPUT).as_posix(),sha256=hashlib.sha256(target.read_bytes()).hexdigest()))


def private_tree(source,target,manifest):
    from app.maintenance.backup import _copy_database
    excluded={'__pycache__','node_modules','.venv','upstream','snowluma','astrbot','astrbot-env'}
    for path in sorted(source.rglob('*')):
        relative=path.relative_to(source)
        if not path.is_file() or path.is_symlink() or excluded.intersection(relative.parts):continue
        if path.suffix in ('.pyc','.dll','.node','.exe','.stop','.stdout','.stderr') or path.name.endswith(('-wal','-shm')) or path.name in ('processes.json','prepared-root.txt'):continue
        if path.suffix=='.zip' and source.name!='imports':
            manifest.append(dict(excluded=path.relative_to(ROOT).as_posix(),reason='Archived distribution needs separate license review; not part of the active Archive snapshot'));continue
        dest=target/relative
        if path.suffix in ('.db','.sqlite','.sqlite3') and path.name!='database.sqlite3':
            dest.parent.mkdir(parents=True,exist_ok=True);_copy_database(path,dest)
            manifest.append(dict(path=dest.relative_to(OUTPUT).as_posix(),consistency='existing-sqlite-backup'))
        else:copy_file(path,dest,manifest)


def build(destination,kind,*,dataset=None,include_runtime=False):
    global OUTPUT
    OUTPUT=Path(destination).resolve()
    if OUTPUT.exists():raise ValueError('Destination must be new; never overwrite a private or public release')
    OUTPUT.mkdir(parents=True)
    policy=json.loads((ROOT/'release/public-whitelist.json').read_text('utf8'));manifest=[]
    for pattern in policy['patterns']:
        matches=sorted(ROOT.glob(pattern))
        if not matches:raise ValueError('Missing allowlist input: '+pattern)
        for path in matches:
            if path.is_file() and not set(path.parts).intersection(policy['excluded_parts']):copy_file(path,OUTPUT/path.relative_to(ROOT),manifest)
    if kind=='public':
        # Project a clean source profile without changing the private launcher.
        for name in ('python_runtime','portable_setup'):
            shutil.copy2(ROOT/f'release/templates/{name}.public.ps1',OUTPUT/f'scripts/{name}.ps1')
        shutil.copy2(ROOT/'release/templates/vite.public.config.ts',OUTPUT/'frontend/vite.config.ts')
        launcher=OUTPUT/'scripts/portable.ps1';value=launcher.read_text('utf8')
        if '  $live=' in value:
            start=value.index('  $live=');end=value.index('\n }\n foreach',start)
            value=value[:start]+(ROOT/'release/templates/portable_defaults.public.txt').read_text('utf8').rstrip()+value[end:]
        value=value.replace(" $old=ReadEnv (LocalPath 'data/user/gateway.env')"," $old=@{}")
        value='\n'.join(line for line in value.splitlines() if "if(-not $gateway['CONTINUUM_BRIDGE_TOKEN']){$gateway=ReadEnv" not in line)+'\n'
        value=value.replace("$config.Dataset -eq 'data/user/v7'", "([string]$task.Actions.Arguments).Contains($root)")
        value=value.replace("$legacy.State -ne 'Disabled' -and ([string]$task.Actions.Arguments)","$legacy.State -ne 'Disabled' -and ([string]$legacy.Actions.Arguments)")
        value=value.replace('--dataset $config.Dataset\n','--dataset $config.Dataset --backend-port "$($config.BackendPort)" --onebot-port "$($config.OneBotPort)"\n')
        launcher.write_text(value,encoding='utf8')
        own_builder=OUTPUT/'scripts/build_release.py';value=own_builder.read_text('utf8')
        for old,new in [('data/user/v7','data/user/v7'),('data/portable/memory.json','data/portable/memory.json'),('data/user/gateway.env','data/user/gateway.env')]:value=value.replace(old,new)
        own_builder.write_text(value,encoding='utf8')
        memory=OUTPUT/'scripts/auto_memory.ps1';value=memory.read_text('utf8')
        value=value.replace('data/rehearsals/m1-4-auto-20261029','data/portable').replace('data/user/v7','data/user/v7').replace('$ConfigPath="$w/config.json"','$ConfigPath="$w/memory.json"')
        memory.write_text(value,encoding='utf8')
        for row in manifest:
            row['sha256']=hashlib.sha256((OUTPUT/row['path']).read_bytes()).hexdigest()
    shutil.copy2(ROOT/f'release/README_{kind.upper()}.md',OUTPUT/'README.md')
    (OUTPUT/'.env.example').write_text('DEEPSEEK_API_KEY=\nDEEPSEEK_MODEL=deepseek-flash\nDEEPSEEK_BASE_URL=https://api.deepseek.com\nCONTINUUM_STYLE_REFERENCES_ENABLED=0\nCONTINUUM_CORRECTION_REFERENCES_ENABLED=0\n',encoding='utf8')
    (OUTPUT/'.gitignore').write_text('/data/\n/runtime/\n/deliveries/\n.env\n.env.*\n!.env.example\n.venv/\nnode_modules/\n__pycache__/\n*.pyc\n*.log\n*.db*\n',encoding='utf8')
    lineage=None
    if kind=='private':
        from app.maintenance.backup import create_backup,restore_backup
        from prepare_astrbot_gateway import configure_transport
        source_config=json.loads((ROOT/'data/portable/config.json').read_text('utf8')) if (ROOT/'data/portable/config.json').exists() else {}
        dataset=Path(dataset or ROOT/source_config.get('Dataset','data/user/v7')).resolve()
        relative=dataset.relative_to(ROOT)
        backup=OUTPUT/'data/backups'/('release-snapshot-'+uuid4().hex)
        package=create_backup(dataset/'continuum.db',dataset/'persona',backup,allow_plaintext=True)
        restore_backup(backup,OUTPUT/relative)
        lineage=dict(active_dataset=relative.as_posix(),snapshot_id=package['backup_id'],snapshot_at=package['snapshot_at'],entities=package['entities'],other_lineages='Private historical materials only; never auto-merged')
        for name in ('imports','backups','active-p1','exports','cleanup'):
            source=ROOT/'data'/name
            if source.exists():private_tree(source,OUTPUT/'data'/name,manifest)
        if (ROOT/'data/continuum.db').exists():
            from app.maintenance.backup import _copy_database
            _copy_database(ROOT/'data/continuum.db',OUTPUT/'data/continuum.db')
        for source in (dataset/'memory-pilot.env',dataset.parent/'.env.gateway',dataset.parent/'portable.gateway.env',ROOT/'data/portable/memory.json'):
            if source.exists():copy_file(source,OUTPUT/source.relative_to(ROOT),manifest)
        private=OUTPUT/'data/portable';private.mkdir(parents=True,exist_ok=True)
        gateway_source=ROOT/source_config['GatewayEnv'] if source_config.get('GatewayEnv') else dataset.parent/'portable.gateway.env'
        copy_file(gateway_source,OUTPUT/relative.parent/'gateway.env',manifest)
        memory_source=ROOT/source_config.get('MemoryConfig','data/portable/memory.json')
        copy_file(memory_source,private/'memory.json',manifest)
        for name in ('secrets.env',):
            if (ROOT/'data/portable'/name).exists():copy_file(ROOT/'data/portable'/name,private/name,manifest)
        # Preserve useful provider settings; machine-specific paths are not active config.
        if (ROOT/'.env').exists():
            from dotenv import dotenv_values
            values=dotenv_values(ROOT/'.env')
            keep={k:v for k,v in values.items() if v is not None and (k.startswith(('DEEPSEEK_','OPENAI_','LOCAL_','CONTINUUM_STYLE_','CONTINUUM_CORRECTION_')) or k=='CONTINUUM_CONTEXT_BUDGET')}
            for k,v in list(keep.items()):
                if ROOT.as_posix() in v or str(ROOT) in v:keep[k]=v.replace(ROOT.as_posix()+'/', '').replace(str(ROOT)+'\\','')
            (OUTPUT/'.env').write_text('\n'.join(k+'='+v for k,v in keep.items())+'\n',encoding='utf8')
            copy_file(ROOT/'.env',OUTPUT/'data/private-original-config/root.env',manifest)
        configure_transport(OUTPUT/relative.parent,6199)
        config=dict(Dataset=relative.as_posix(),AstrBotRoot=(relative.parent/'astrbot').as_posix(),GatewayEnv=(relative.parent/'gateway.env').as_posix(),MemoryConfig='data/portable/memory.json',BackendPort=8010,OneBotPort=6199,SnowLumaUrl='http://127.0.0.1:5099',ModelConfig=source_config.get('ModelConfig','deepseek'),AutoMemorySync=source_config.get('AutoMemorySync',True))
        (private/'config.json').write_text(json.dumps(config,indent=2),encoding='utf8')
        if (ROOT/'frontend/dist').exists():shutil.copytree(ROOT/'frontend/dist',OUTPUT/'frontend/dist')
        if include_runtime:
            shutil.copytree(ROOT/'runtime',OUTPUT/'runtime',ignore=shutil.ignore_patterns('__pycache__','*.pyc','prepared-root.txt'))
            (OUTPUT/'runtime/prepared-root.txt').write_text(str(ROOT),encoding='utf8')
        (OUTPUT/'PRIVATE_SENSITIVE.txt').write_text('PRIVATE: contains unencrypted keys, personal chats, sources and backups. Never upload this folder as the public release. QQ/SnowLuma sessions/binaries are NOT included.\n',encoding='utf8')
    else:
        # Refuse known real credentials/identities even when their filenames passed.
        from dotenv import dotenv_values
        secrets=set()
        for source in [ROOT/'.env',ROOT/'data/user/gateway.env',ROOT/'data/portable/secrets.env']:
            if source.exists():secrets.update(v for k,v in dotenv_values(source).items() if v and len(v)>=6 and any(x in k for x in ('KEY','TOKEN','ACCOUNT_ID','SENDER_ID')))
        for path in OUTPUT.rglob('*'):
            if path.is_file():
                content=path.read_text('utf8')
                if any(secret in content for secret in secrets):raise ValueError('Known private value found in public output; release incomplete')
                if str(ROOT) in content:raise ValueError('Machine root found in public output; release incomplete')
    (OUTPUT/'release-manifest.json').write_text(json.dumps(dict(kind=kind,files=manifest,generated=['README.md','.env.example','.gitignore','release-manifest.json'],lineage=lineage,third_party='See release/THIRD_PARTY.md; QQ/SnowLuma not included',not_included=['QQ session','SnowLuma binary','old process/PID/task instances','data/archive dev archives','unselected rehearsal/test outputs']),ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(dict(kind=kind,destination=str(OUTPUT),source_files=len(manifest),cloud_calls=0),ensure_ascii=False))
    return OUTPUT


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--kind',choices=['private','public'],required=True);p.add_argument('--destination',required=True);p.add_argument('--dataset');p.add_argument('--include-runtime',action='store_true')
    a=p.parse_args()
    if a.kind=='public' and a.include_runtime:p.error('Unreviewed current runtime is not redistributed in public builds')
    build(a.destination,a.kind,dataset=a.dataset,include_runtime=a.include_runtime)
