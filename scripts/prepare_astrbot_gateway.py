"""Prepare a NEW synthetic gateway lab; never change existing account profiles."""
import argparse
import json
import secrets
import shutil
import sys
from gateway_common import ROOT,isolated


def prepare(destination,backend_port=8010,onebot_port=6199):
    folder=isolated(destination)
    if folder.exists():raise ValueError('Destination must not exist')
    folder.mkdir(parents=True)
    sys.path.insert(0,str(ROOT/'backend'))
    from app.database import setup_database
    from app.maintenance.backup import upgrade_copy
    engine,_=setup_database('sqlite:///'+(folder/'source.db').as_posix());engine.dispose()
    upgrade_copy(folder/'source.db',ROOT/'data/persona',folder/'v7',folder/'backups',target_version=7,allow_plaintext=True)
    values=dict(CONTINUUM_GATEWAY_ENABLED='true',CONTINUUM_GATEWAY_ACCOUNT_ID='900000001',
        CONTINUUM_GATEWAY_SENDER_ID='900000002',CONTINUUM_BRIDGE_TOKEN=secrets.token_urlsafe(48),
        CONTINUUM_GATEWAY_ADMIN_TOKEN=secrets.token_urlsafe(48),CONTINUUM_GATEWAY_MODEL_CONFIG='mock',
        CONTINUUM_BRIDGE_URL=f'http://127.0.0.1:{backend_port}',ASTRBOT_BRIDGE_PLATFORM_ID='continuum-onebot')
    (folder/'.env.gateway').write_text('\n'.join(k+'='+v for k,v in values.items())+'\n',encoding='utf-8')
    configure_transport(folder,onebot_port)
    return folder


def configure_transport(folder,onebot_port=6199):
    """Fresh gateway-only profile; no upstream session or AI history copied."""
    onebot_token=secrets.token_urlsafe(48)
    astrbot=folder/'astrbot';data=astrbot/'data';data.mkdir(parents=True)
    shutil.copytree(ROOT/'integrations/astrbot_plugin_continuum_bridge',data/'plugins/astrbot_plugin_continuum_bridge',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    config=dict(config_version=5,provider=[],provider_sources=[],provider_settings=dict(enable=False,file_extract=dict(enable=False)),
        agent_runner=dict(runner_type='local',config=dict(auto_save_history=False)),
        provider_stt_settings=dict(enable=False),provider_tts_settings=dict(enable=False),
        provider_ltm_settings=dict(group_icl_enable=False,group_message_history_enable=False,image_caption=False,active_reply=dict(enable=False)),
        subagent_orchestrator=dict(main_enable=False,agents=[]),kb_names=[],persona=[],
        plugin_set=['astrbot_plugin_continuum_bridge'],disable_builtin_commands=True,disable_metrics=True,
        log_level='WARNING',log_file_enable=False,trace_enable=False,dashboard=dict(enable=False,host='127.0.0.1'),
        platform_settings=dict(enable_id_white_list=False,no_permission_reply=False,ignore_bot_self_message=True,segmented_reply=dict(enable=False)),
        platform=[dict(id='continuum-onebot',type='continuum_onebot_gateway',enable=True,ws_reverse_host='127.0.0.1',ws_reverse_port=onebot_port,ws_reverse_token=onebot_token)])
    (data/'cmd_config.json').write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding='utf-8')
    snow=folder/'snowluma';(snow/'config').mkdir(parents=True)
    (snow/'config/runtime.json').write_text(json.dumps(dict(webuiPort=5099,webuiHost='127.0.0.1',hookAutoLoad=False,trustProxy=False)),encoding='utf-8')
    (snow/'config/onebot.json').write_text(json.dumps(dict(networks=dict(httpServers=[],httpClients=[],wsServers=[],
        wsClients=[dict(name='astrbot-gateway',enabled=True,url=f'ws://127.0.0.1:{onebot_port}/ws',role='Universal',
                        accessToken=onebot_token,messageFormat='array',reportSelfMessage=False,reconnectIntervalMs=3000)]),
        statusCommand=dict(enabled=False),historySync=dict(enabled=False),notifications=dict(channelIds=[])),indent=2),encoding='utf-8')
    return folder


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--destination',required=True)
    p.add_argument('--backend-port',type=int,default=8010);p.add_argument('--onebot-port',type=int,default=6199)
    args=p.parse_args();prepare(args.destination,args.backend_port,args.onebot_port)
    print('Synthetic lab prepared; credentials stay local. No QQ login, hook, cloud call or production switch.')
