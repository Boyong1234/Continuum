"""Pinned AstrBot lifecycle with gateway-only configuration and pre-sink logging.

Run with the SEPARATE AstrBot Python environment, not Continuum's .venv.
No upstream source patch. Dashboard disabled; loopback WS token required.
"""
import argparse
import asyncio
import importlib.metadata
import json
import logging
import os
import re
import sys
from gateway_common import local_env,isolated


def metadata_logging():
    original=logging.getLogRecordFactory()
    def factory(*args,**kwargs):
        record=original(*args,**kwargs)
        # Installed before AstrBot imports: applies BEFORE console/broker/file sinks.
        message=record.msg if isinstance(record.msg,str) else ''
        frames=re.findall(r'File "[^"]*[\\/]([^"\\/]+)", line (\d+)',message) if message.startswith('Traceback') else []
        safe=message if re.fullmatch(r'continuum_bridge code=[a-z_]+',message) else 'gateway_runtime event_redacted'
        if frames:safe+=' frames='+','.join(name+':'+line for name,line in frames[-5:])
        record.msg=safe;record.args=();record.exc_info=None;record.exc_text=None;record.stack_info=None
        return record
    logging.setLogRecordFactory(factory)


async def run(root,stop_file=None):
    os.environ['ASTRBOT_ROOT']=str(root)
    sys.path.insert(0,str(root))  # AstrBot imports installed plugins as data.plugins.*.
    os.environ['ASTRBOT_DISABLE_METRICS']='1'
    os.environ['OTEL_SDK_DISABLED']='true'
    metadata_logging()
    if importlib.metadata.version('astrbot')!='4.29.3':raise RuntimeError('Use pinned AstrBot 4.29.3')
    cfg=json.loads((root/'data/cmd_config.json').read_text(encoding='utf-8-sig'))
    if cfg.get('provider_settings',{}).get('enable') is not False or cfg.get('provider') or cfg.get('provider_sources') or cfg.get('dashboard',{}).get('enable'):
        raise RuntimeError('Not a gateway-only configuration')
    from astrbot.core import db_helper,LogBroker,LogManager,logger
    from astrbot.core.core_lifecycle import AstrBotCoreLifecycle
    broker=LogBroker();LogManager.set_queue_handler(logger,broker)
    lifecycle=AstrBotCoreLifecycle(broker,db_helper)
    await lifecycle.initialize()
    task=asyncio.create_task(lifecycle.start())
    try:
        if stop_file:
            while not stop_file.exists() and not task.done():await asyncio.sleep(.1)
        else:await task
    finally:
        await lifecycle.stop();task.cancel();await asyncio.gather(task,return_exceptions=True)
        # Only redacted cache is saved; useful for checking every sink offline.
        (root/'broker-metadata.json').write_text(json.dumps(list(broker.log_cache),ensure_ascii=False,default=str),encoding='utf-8')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',required=True);p.add_argument('--env-file',required=True);p.add_argument('--stop-file')
    args=p.parse_args();local_env(args.env_file)
    try:asyncio.run(run(isolated(args.root),isolated(args.stop_file) if args.stop_file else None))
    except Exception as exc:
        # Types only. Never dump exception values containing a payload or token.
        print('gateway_start_failed type='+type(exc).__name__,file=sys.stderr)
        raise SystemExit(1)
