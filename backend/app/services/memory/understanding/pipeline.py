"""One explicit B1 experiment at a time. No scheduler, loop or chat hooks."""
import json
from sqlalchemy import text,select

from app.core.config import AppError
from app.models import Memory
from app.services.memory import MemoryManager
from app.services.memory.jobs import MemoryExtractionQueue,ERROR_CODES
from app.services.memory.understanding.extractors import ExtractionInput
from app.services.memory.understanding.reconciler import MemoryReconciler


class B1Pipeline:
    def __init__(self,sessions,*,lease_seconds=120):
        self.sessions=sessions;self.queue=MemoryExtractionQueue(sessions,lease_seconds=lease_seconds)
        with sessions() as session:MemoryReconciler(session)  # Require v5 before any claim.

    def enqueue_snapshot(self,segment,extractor_version,*,config_id='mock'):
        return self.queue.enqueue([m.id for m in segment.messages],extractor_version,config_id=config_id)

    def align_version(self,job_id):
        with self.sessions.begin() as session:MemoryReconciler(session).release_version_alignment(job_id)

    async def process_next(self,extractor,*,owner='b1-manual'):
        lease=self.queue.claim(owner)
        if lease is None:return None
        try:
            if lease['result'] is None:
                if extractor.config_id!=lease['config_id']:
                    raise AppError('configuration_error','提取器与任务选定的模型配置不一致；未调用模型。',409)
                with self.sessions() as session:
                    reconciler=MemoryReconciler(session)
                    messages=tuple(reconciler._sources(lease['id']))
                    # Explicit bounded context only, no bulk history upload.
                    manager=MemoryManager(session)
                    existing=tuple(manager.serialize(m) for m in session.scalars(select(Memory).order_by(Memory.updated_at.desc(),Memory.id).limit(20)))
                extracted=await extractor.extract(ExtractionInput(messages,lease['extractor_version'],existing))
                self.queue.checkpoint(lease,dict(schema_version=1,payload=extracted.result.model_dump()),provider=extracted.provider,model=extracted.metadata.model,usage=extracted.metadata.usage)
            decisions=[]
            def apply(session,envelope):
                decisions.extend(MemoryReconciler(session).apply(lease['id'],envelope['payload']))
            self.queue.complete(lease,apply)
            return dict(job_id=lease['id'],decisions=decisions)
        except AppError as error:
            # complete already records its own failure. Failed extraction usage
            # belongs to this attempt too, even when structured JSON is invalid.
            if self.queue.get(lease['id'])['status']=='leased':
                metadata=getattr(error,'metadata',None)
                if metadata:
                    with self.sessions.begin() as session:
                        self.queue._fence(session,lease)
                        session.execute(text('UPDATE memory_extraction_attempts SET usage_json=:usage,model=:model WHERE lease_token=:token'),dict(usage=json.dumps(metadata.usage),model=metadata.model,token=lease['lease_token']))
                self.queue.fail(lease,error.code if error.code in ERROR_CODES else 'invalid_output')
            raise
