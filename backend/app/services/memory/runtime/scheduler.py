"""Arrival cursor + immutable sealed source sets; no remote call on this path."""
import json
from datetime import datetime,timezone
from sqlalchemy import text
from app.models import uid
from app.services.memory.jobs import MemoryExtractionQueue,stamp
from app.services.memory.runtime.config import MemoryConfig,encoded,accepts_source


class ConversationSegmentScheduler:
    def __init__(self,control,*,clock=None):
        self.control=control;self.sessions=control.sessions;self.clock=clock or (lambda:datetime.now(timezone.utc))
        self.queue=MemoryExtractionQueue(self.sessions,clock=self.clock)

    def _sources(self,s,id):
        return s.execute(text('SELECT a.*,m.content,m.role,m.created_at FROM memory_segment_sources a JOIN messages m ON m.id=a.message_id WHERE segment_id=:id ORDER BY position'),dict(id=id)).mappings().all()

    def _seal(self,s,segment,reason):
        rows=self._sources(s,segment['id']);has_user=any(r['role']=='user' for r in rows)
        s.execute(text('UPDATE memory_segments SET state=:state,sealed_at=:now,seal_reason=:reason,error_code=:error WHERE id=:id'),dict(id=segment['id'],state='sealed' if has_user else 'blocked',now=stamp(self.clock()),reason=reason,error=None if has_user else 'no_user_source'))

    def tick(self,*,force=False,scan_limit=500):
        now=stamp(self.clock());self.control.require()
        with self.sessions.begin() as s:
            s.execute(text('UPDATE memory_schedule_cursor SET last_rowid=last_rowid WHERE id=1'))
            state=self.control.read(s)
            if not state['enabled'] or state['paused']:return 0
            config=MemoryConfig.model_validate(json.loads(state['config_json']))
            cursor=s.execute(text('SELECT last_rowid FROM memory_schedule_cursor WHERE id=1')).scalar_one()
            incoming=s.execute(text('SELECT rowid AS arrival_rowid,id,conversation_id,role,content,metadata FROM messages WHERE rowid>:n ORDER BY rowid LIMIT :limit'),dict(n=cursor,limit=scan_limit)).mappings().all()
            for m in incoming:
                cursor=m['arrival_rowid']
                if not accepts_source(m['role'],m['metadata'],config) or s.execute(text('SELECT message_id FROM memory_exclusions WHERE message_id=:id'),dict(id=m['id'])).first():continue
                # Explicit historical admission can rewind the cursor; primary
                # sources already assigned to a segment keep their original job.
                if s.execute(text('SELECT 1 FROM memory_segment_sources WHERE message_id=:id AND is_primary=1 LIMIT 1'),dict(id=m['id'])).first():continue
                segment=s.execute(text("SELECT * FROM memory_segments WHERE conversation_id=:cid AND state='open'"),dict(cid=m['conversation_id'])).mappings().first()
                if segment:
                    rows=self._sources(s,segment['id']);size=sum(len(r['content'].encode())+32 for r in rows)
                    switch=m['role']=='user' and m['content'].lstrip().startswith(('换个话题','另外一件事','另一个问题'))
                    if segment['config_hash']!=state['config_hash'] or switch or len(rows)>=config.max_messages or size+len(m['content'].encode())+32>config.max_segment_tokens:
                        self._seal(s,segment,'policy_or_boundary');segment=None
                if segment is None:
                    sequence=s.execute(text('SELECT COALESCE(max(sequence),0)+1 FROM memory_segments WHERE conversation_id=:cid'),dict(cid=m['conversation_id'])).scalar_one()
                    id=uid();s.execute(text("INSERT INTO memory_segments VALUES (:id,:cid,:seq,'open',:hash,:config,NULL,:now,:now,NULL,NULL,NULL)"),dict(id=id,cid=m['conversation_id'],seq=sequence,hash=state['config_hash'],config=state['config_json'],now=now))
                    previous=s.execute(text('SELECT a.message_id,a.arrival_rowid,m.content FROM memory_segment_sources a JOIN memory_segments g ON g.id=a.segment_id JOIN messages m ON m.id=a.message_id WHERE g.conversation_id=:cid AND a.is_primary=1 AND NOT EXISTS(SELECT 1 FROM memory_exclusions x WHERE x.message_id=a.message_id) ORDER BY a.arrival_rowid DESC LIMIT :n'),dict(cid=m['conversation_id'],n=config.overlap_messages)).mappings().all()
                    context=list(reversed(previous))
                    while context and (len(context)+1>config.max_messages or sum(len(r['content'].encode())+32 for r in context)+len(m['content'].encode())+32>config.max_segment_tokens):context.pop(0)
                    for i,r in enumerate(context):s.execute(text('INSERT INTO memory_segment_sources VALUES (:segment,:message,:arrival,:position,0)'),dict(segment=id,message=r['message_id'],arrival=r['arrival_rowid'],position=i))
                    segment=dict(id=id,created_at=now)
                position=len(self._sources(s,segment['id']))
                s.execute(text('INSERT INTO memory_segment_sources VALUES (:segment,:message,:arrival,:position,1)'),dict(segment=segment['id'],message=m['id'],arrival=m['arrival_rowid'],position=position))
                s.execute(text('UPDATE memory_segments SET last_arrived_at=:now WHERE id=:id'),dict(id=segment['id'],now=now))
                if len(m['content'].encode())+32>config.max_segment_tokens:
                    s.execute(text("UPDATE memory_segments SET state='blocked',sealed_at=:now,seal_reason='oversized_message',error_code='segment_budget' WHERE id=:id"),dict(id=segment['id'],now=now))
            s.execute(text('UPDATE memory_schedule_cursor SET last_rowid=:n WHERE id=1'),dict(n=cursor))
            opened=s.execute(text("SELECT * FROM memory_segments WHERE state='open'")).mappings().all()
            for segment in opened:
                busy=s.execute(text("SELECT id FROM generation_attempts WHERE conversation_id=:cid AND status='running' LIMIT 1"),dict(cid=segment['conversation_id'])).first()
                if busy:continue
                rows=self._sources(s,segment['id']);idle=(self.clock()-datetime.fromisoformat(segment['last_arrived_at'].replace('Z','+00:00'))).total_seconds();age=(self.clock()-datetime.fromisoformat(segment['created_at'].replace('Z','+00:00'))).total_seconds()
                if force or idle>=config.idle_seconds or age>=config.max_wait_seconds or len(rows)>=config.message_threshold:
                    self._seal(s,segment,'explicit' if force else 'idle_or_threshold')
            sealed=s.execute(text("SELECT * FROM memory_segments WHERE state='sealed' ORDER BY created_at,id")).mappings().all();queued=0
            for segment in sealed:
                cfg=MemoryConfig.model_validate(json.loads(segment['config_json']))
                rows=self._sources(s,segment['id'])
                if any(s.execute(text('SELECT message_id FROM memory_exclusions WHERE message_id=:id'),dict(id=r['message_id'])).first() for r in rows):
                    s.execute(text("UPDATE memory_segments SET state='blocked',error_code='source_excluded' WHERE id=:id"),dict(id=segment['id']));continue
                job=self.queue.enqueue([r['message_id'] for r in rows],cfg.extractor_version+'-'+segment['config_hash'][:12],config_id=cfg.provider,session=s)
                s.execute(text("UPDATE memory_segments SET state='queued',job_id=:job WHERE id=:id"),dict(id=segment['id'],job=job['id']));queued+=1
                s.execute(text('INSERT OR IGNORE INTO memory_segment_job_history VALUES (:job,:segment,:hash,:config,:now)'),dict(job=job['id'],segment=segment['id'],hash=segment['config_hash'],config=segment['config_json'],now=now))
            s.execute(text("UPDATE memory_segments SET state='processed' WHERE state='queued' AND job_id IN (SELECT id FROM memory_extraction_jobs WHERE status='succeeded')"))
            s.execute(text('DELETE FROM memory_conversation_dirty'))
            return queued

    def replay(self,segment_id):
        """Explicit source-scoped version replay; never bulk-replays history."""
        from app.core.config import AppError
        with self.sessions.begin() as s:
            s.execute(text('UPDATE memory_runtime SET updated_at=updated_at WHERE id=1'))
            runtime=self.control.read(s)
            if not runtime['enabled'] or runtime['paused']:raise AppError('memory_paused','请先启用并继续当前配置。',409)
            segment=s.execute(text('SELECT * FROM memory_segments WHERE id=:id'),dict(id=segment_id)).mappings().one()
            old=s.execute(text('SELECT status FROM memory_extraction_jobs WHERE id=:id'),dict(id=segment['job_id'])).scalar_one_or_none()
            if old!='succeeded':raise AppError('alignment_required','只能显式重提取已成功应用的片段。',409)
            rows=self._sources(s,segment_id)
            if any(s.execute(text('SELECT message_id FROM memory_exclusions WHERE message_id=:id'),dict(id=r['message_id'])).first() for r in rows):raise AppError('configuration_error','片段存在已禁止处理的来源。',409)
            cfg=MemoryConfig.model_validate(json.loads(runtime['config_json']))
            job=self.queue.enqueue([r['message_id'] for r in rows],cfg.extractor_version+'-'+runtime['config_hash'][:12],config_id=cfg.provider,session=s)
            s.execute(text('INSERT OR IGNORE INTO memory_segment_job_history VALUES (:job,:segment,:hash,:config,:now)'),dict(job=job['id'],segment=segment_id,hash=runtime['config_hash'],config=runtime['config_json'],now=stamp(self.clock())))
            s.execute(text('UPDATE memory_segments SET state=:state,job_id=:job,config_hash=:hash,config_json=:config WHERE id=:id'),dict(id=segment_id,state='processed' if job['status']=='succeeded' else 'queued',job=job['id'],hash=runtime['config_hash'],config=runtime['config_json']))
            return job['id']
