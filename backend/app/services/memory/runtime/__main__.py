"""python -m app.services.memory.runtime --database PATH <command>."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import signal
import sys

from app.database import setup_database
from app.core.config import AppError
from app.maintenance.validation import inspect_database,SafetyError
from app.services.providers import ProviderRegistry
from app.services.memory.runtime.config import MemoryConfig
from app.services.memory.runtime.control import MemoryControl
from app.services.memory.runtime.worker import MemoryWorker


def main():
    p=argparse.ArgumentParser();p.add_argument('--database',type=Path,required=True)
    sub=p.add_subparsers(dest='command',required=True)
    c=sub.add_parser('configure');c.add_argument('--config',type=Path);c.add_argument('--enabled',action=argparse.BooleanOptionalAction,default=False);c.add_argument('--authorize-cloud',action='store_true');c.add_argument('--from-env',action='store_true')
    for name in ('status','pause','resume'):sub.add_parser(name)
    replay=sub.add_parser('replay');replay.add_argument('--segment',required=True)
    certify=sub.add_parser('certify');certify.add_argument('--report',type=Path,required=True)
    sync=sub.add_parser('sync');sync.add_argument('--config',type=Path);sync.add_argument('--include-history',action='store_true');sync.add_argument('--authorize-cloud',action='store_true')
    sync.add_argument('--max-batches',type=int,default=1);sync.add_argument('--watch',action='store_true');sync.add_argument('--auto-memory-sync',action='store_true')
    sync.add_argument('--interval-hours',type=float,default=24);sync.add_argument('--min-new-messages',type=int,default=30);sync.add_argument('--stop-file',type=Path)
    for name in ('once','run'):
        cmd=sub.add_parser(name);cmd.add_argument('--force-seal',action='store_true');cmd.add_argument('--lease-seconds',type=int,default=120);cmd.add_argument('--poll-seconds',type=float,default=1);cmd.add_argument('--stop-file',type=Path)
    args=p.parse_args();engine=None
    try:
        if inspect_database(args.database)['database_schema_version']<6:raise AppError('migration_required','Worker 只支持显式迁移的 v6 数据集。',409)
        engine,sessions=setup_database('sqlite:///'+args.database.resolve().as_posix());control=MemoryControl(sessions,ProviderRegistry())
        if args.command=='configure':
            config=MemoryConfig.model_validate(json.loads(args.config.read_text(encoding='utf-8'))) if args.config else MemoryConfig.from_environment() if args.from_env else MemoryConfig()
            enabled=args.enabled or args.from_env and os.getenv('MEMORY_AUTO_EXTRACT_ENABLED','').lower()=='true'
            authorized=args.authorize_cloud or args.from_env and os.getenv('MEMORY_CLOUD_AUTHORIZED','').lower()=='true'
            print(json.dumps(control.configure(config,enabled=enabled,authorize_cloud=authorized)));return
        if args.command=='status':
            status=control.status()
            if status['supported']:
                from app.services.memory.runtime.sync import MemorySync
                status.update(MemorySync(control).pending())
            print(json.dumps(status));return
        if args.command in ('pause','resume'):print(json.dumps(control.pause(args.command=='pause')));return
        if args.command=='certify':print(json.dumps(dict(passed=control.certify(json.loads(args.report.read_text(encoding='utf-8'))))));return
        if args.command=='replay':
            from app.services.memory.runtime.scheduler import ConversationSegmentScheduler
            print(json.dumps(dict(job_id=ConversationSegmentScheduler(control).replay(args.segment))));return
        if args.command!='sync' and not .05<=args.poll_seconds<=60:raise ValueError('Poll outside allowed range')
        stop=False
        def handle(*_):
            nonlocal stop
            stop=True
        signal.signal(signal.SIGTERM,handle);signal.signal(signal.SIGINT,handle)
        if args.command=='sync':
            from app.services.memory.runtime.sync import run_command
            asyncio.run(run_command(args,control,lambda:stop));return
        worker=MemoryWorker(control,lease_seconds=args.lease_seconds)
        async def run():
            print(json.dumps(dict(event='worker_started',pid=os.getpid())),flush=True)
            while not stop and not(args.stop_file and args.stop_file.exists()):
                try:
                    result=await worker.run_once(force_seal=args.force_seal)
                    if result:print(json.dumps({k:v for k,v in result.items() if k in ('job_id','status','error_code')}),flush=True)
                except Exception:
                    # A transient DB error does not spill private payloads or
                    # terminate recovery. Existing lease fences the next attempt.
                    print('{"event":"worker_error","error_code":"internal_error"}',flush=True)
                if args.command=='once':break
                await asyncio.sleep(args.poll_seconds)
            print('{"event":"worker_stopped"}',flush=True)
        asyncio.run(run())
    except (AppError,SafetyError) as e:
        print(json.dumps(dict(error_code=e.code,message=str(e))),file=sys.stderr);sys.exit(2)
    except Exception:
        print('{"error_code":"configuration_error","message":"Worker configuration or storage invalid; private values omitted"}',file=sys.stderr);sys.exit(2)
    finally:
        if engine:engine.dispose()


if __name__=='__main__':main()
