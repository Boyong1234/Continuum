"""Create an EMPTY v7 through the released backup/migration chain, never a seed DB."""
import argparse
import json
from pathlib import Path
import secrets
import shutil
import sys
import tempfile
from gateway_common import ROOT,isolated
from prepare_astrbot_gateway import configure_transport

sys.path.insert(0,str(ROOT/'backend'))
from app.database import setup_database
from app.maintenance.backup import upgrade_copy
from app.maintenance.validation import inspect_database
from app.services.memory.runtime.config import MemoryConfig


def configure_ports(backend_port,onebot_port):
    """Keep this installation's launcher, Bridge and OneBot sample consistent."""
    if not (1024<=backend_port<=65535 and 1024<=onebot_port<=65535 and backend_port!=onebot_port):
        raise ValueError('Choose two distinct local ports between 1024 and 65535')
    path=ROOT/'data/portable/config.json';config=json.loads(path.read_text('utf8'))
    gateway=isolated(ROOT/config['GatewayEnv'])
    values=dict(line.split('=',1) for line in gateway.read_text('utf8').splitlines() if line and not line.startswith('#'))
    values['CONTINUUM_BRIDGE_URL']=f'http://127.0.0.1:{backend_port}'
    astrbot=isolated(ROOT/config['AstrBotRoot'])/'data/cmd_config.json'
    transport=json.loads(astrbot.read_text('utf8'))
    transport['platform'][0]['ws_reverse_port']=onebot_port
    transport['platform'][0]['ws_reverse_host']='127.0.0.1'
    config.update(BackendPort=backend_port,OneBotPort=onebot_port)
    gateway.write_text('\n'.join(k+'='+v for k,v in values.items())+'\n',encoding='utf8')
    astrbot.write_text(json.dumps(transport,ensure_ascii=False,indent=2),encoding='utf8')
    path.write_text(json.dumps(config,indent=2),encoding='utf8')
    sample=isolated(ROOT/config['Dataset']).parent/'snowluma/config/onebot.json'
    if sample.exists():
        value=json.loads(sample.read_text('utf8'))
        value['networks']['wsClients'][0]['url']=f'ws://127.0.0.1:{onebot_port}/ws'
        sample.write_text(json.dumps(value,indent=2),encoding='utf8')


def initialize(dataset,*,mock=False,backend_port=8010,onebot_port=6199):
    if not (1024<=backend_port<=65535 and 1024<=onebot_port<=65535 and backend_port!=onebot_port):raise ValueError('Invalid local ports')
    dataset=isolated(dataset)
    if (dataset/'continuum.db').exists():
        if inspect_database(dataset/'continuum.db')['database_schema_version']!=7:raise ValueError('Existing archive needs explicit copy migration')
        return dataset
    if dataset.exists():raise ValueError('Nonempty preparation path; choose a new dataset')
    private=ROOT/'data/portable';private.mkdir(parents=True,exist_ok=True)
    stage=Path(tempfile.mkdtemp(prefix='empty-v1-',dir=private))
    db,_=setup_database('sqlite:///'+(stage/'source.db').as_posix());db.dispose()
    upgrade_copy(stage/'source.db',ROOT/'templates/persona',dataset,ROOT/'data/backups',allow_plaintext=True,target_version=7)
    pilot='CONTINUUM_DIALOGUE_CAPABILITIES=1\nCONTINUUM_CHAT_HISTORY_PROVIDER=deepseek\nCONTINUUM_NATURAL_RECALL=1\n'
    (dataset/'memory-pilot.env').write_text(pilot,encoding='utf8')
    astrbot=dataset.parent/'astrbot'
    if not astrbot.exists():configure_transport(dataset.parent,onebot_port)
    env=dataset.parent/'gateway.env'
    values=dict(CONTINUUM_GATEWAY_ENABLED='true',CONTINUUM_GATEWAY_ACCOUNT_ID='900000001' if mock else '',
        CONTINUUM_GATEWAY_SENDER_ID='900000002' if mock else '',CONTINUUM_BRIDGE_TOKEN=secrets.token_urlsafe(48),
        CONTINUUM_GATEWAY_ADMIN_TOKEN=secrets.token_urlsafe(48),CONTINUUM_GATEWAY_MODEL_CONFIG='mock' if mock else 'deepseek',
        CONTINUUM_BRIDGE_URL=f'http://127.0.0.1:{backend_port}',ASTRBOT_BRIDGE_PLATFORM_ID='continuum-onebot')
    env.write_text('\n'.join(k+'='+v for k,v in values.items())+'\n',encoding='utf8')
    config=dict(Dataset=dataset.relative_to(ROOT).as_posix(),AstrBotRoot=astrbot.relative_to(ROOT).as_posix(),
        GatewayEnv=env.relative_to(ROOT).as_posix(),MemoryConfig='data/portable/memory.json',BackendPort=backend_port,
        OneBotPort=onebot_port,SnowLumaUrl='http://127.0.0.1:5099',ModelConfig=values['CONTINUUM_GATEWAY_MODEL_CONFIG'],AutoMemorySync=False)
    if not (private/'memory.json').exists():(private/'memory.json').write_text(MemoryConfig(provider='deepseek',selective_sync=True,daily_requests=3,daily_tokens=15000).model_dump_json(indent=2),encoding='utf8')
    if not (private/'config.json').exists():(private/'config.json').write_text(json.dumps(config,indent=2),encoding='utf8')
    return dataset


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--dataset',default='data/user/v7');p.add_argument('--mock',action='store_true');p.add_argument('--backend-port',type=int,default=8010);p.add_argument('--onebot-port',type=int,default=6199);p.add_argument('--update-ports',action='store_true')
    args=p.parse_args()
    if args.update_ports:
        configure_ports(args.backend_port,args.onebot_port)
        print('Local ports updated. Restart this installation; manually match SnowLuma reverse WS to the generated sample.')
    else:
        initialize(ROOT/args.dataset,mock=args.mock,backend_port=args.backend_port,onebot_port=args.onebot_port)
        print('Empty v7 initialized. No imported history; cloud Memory Sync OFF. Configure accounts/key before real use.')
