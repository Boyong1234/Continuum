"""Low-frequency admission into the existing scheduler, queue and Worker."""
import asyncio
import json
from datetime import datetime,timezone
from sqlalchemy import text
from app.core.config import AppError
from app.services.memory.runtime.config import MemoryConfig,accepts_source
from app.services.memory.runtime.worker import MemoryWorker


class MemorySync:
    def __init__(self,control,*,extractor_factory=None):
        self.control=control
        self.worker=MemoryWorker(control,extractor_factory=extractor_factory)

    def pending(self):
        with self.control.sessions() as s:
            runtime=self.control.read(s)
            config=MemoryConfig.model_validate_json(runtime['config_json']) if runtime['config_hash'] else MemoryConfig()
            candidates=s.execute(text("SELECT m.role,m.metadata FROM messages m WHERE m.rowid>:start AND NOT EXISTS(SELECT 1 FROM memory_segment_sources a WHERE a.message_id=m.id AND a.is_primary=1) AND NOT EXISTS(SELECT 1 FROM memory_exclusions x WHERE x.message_id=m.id)"),dict(start=runtime['start_rowid'])).mappings()
            new=sum(accepts_source(row['role'],row['metadata'],config) for row in candidates)
            queued=s.execute(text("SELECT count(*) FROM memory_segments WHERE state IN ('open','sealed','queued')")).scalar_one()
            last=s.execute(text("SELECT max(updated_at) FROM memory_extraction_jobs WHERE status='succeeded'")).scalar_one()
            episodes=s.execute(text('SELECT count(*) FROM episodes')).scalar_one()
            core=s.execute(text("SELECT count(*) FROM memory_lifecycle WHERE state='active' AND evidence_level IN ('user_explicit','user_confirmed')")).scalar_one()
        return dict(new_messages=new,pending_segments=queued,last_processed_at=last,episode_count=episodes,active_core_count=core)

    def due(self,interval_hours=24,min_new_messages=30):
        state=self.pending()
        if state['new_messages']<min_new_messages and not state['pending_segments']:return False
        last=state['last_processed_at']
        return not last or (datetime.now(timezone.utc)-datetime.fromisoformat(last.replace('Z','+00:00'))).total_seconds()>=interval_hours*3600

    async def run(self,config,*,include_history=False,authorize_cloud=False,max_batches=1,auto_mode=False):
        if not 1<=max_batches<=10:raise ValueError('max_batches must be between 1 and 10')
        old=self.control.read()
        if old['enabled']:raise AppError('memory_already_running','请先关闭旧自动提取，再运行低频 sync。',409)
        self.control.configure(config,enabled=True,authorize_cloud=authorize_cloud,include_history=include_history)
        results=[]
        try:
            for _ in range(max_batches):
                result=await self.worker.run_once(force_seal=True)
                if result is None:break
                results.append({k:v for k,v in result.items() if k in ('job_id','status','error_code')})
                if result['status']!='succeeded':break
            return dict(results=results,error_code=self.worker.last_error,**self.pending(),today=self.worker.ledger.summary())
        finally:
            # No always-on extraction is left behind, including failed runs.
            paused=self.control.read()['paused']
            self.control.configure(config,enabled=False)
            if auto_mode and not paused:self.control.pause(False)


async def run_command(args,control,stopping):
    if args.watch and not args.auto_memory_sync:
        print(json.dumps(dict(auto_memory_sync=False,status='disabled')));return
    if not 1<=args.interval_hours<=168 or not 1<=args.min_new_messages<=10000:raise ValueError('Invalid sync interval or threshold')
    state=control.read()
    config=MemoryConfig.model_validate_json(args.config.read_text(encoding='utf-8')) if args.config else MemoryConfig.model_validate_json(state['config_json']) if state['config_hash'] else MemoryConfig()
    config=config.model_copy(update=dict(selective_sync=True,extractor_version='m1-v1'))
    sync=MemorySync(control)
    while not stopping() and not(args.stop_file and args.stop_file.exists()):
        if not args.watch or (not control.read()['paused'] and sync.due(args.interval_hours,args.min_new_messages)):
            result=await sync.run(config,include_history=args.include_history,authorize_cloud=args.authorize_cloud,max_batches=args.max_batches,auto_mode=args.watch)
            print(json.dumps(result),flush=True)
        if not args.watch:break
        await asyncio.sleep(30)
