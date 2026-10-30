"""Independent bounded worker; no coroutine is launched by FastAPI startup."""
import asyncio
from contextlib import suppress
from dataclasses import replace
from datetime import datetime,timezone
import json
import time
import os
import re
from sqlalchemy import text,select

from app.core.config import AppError
from app.models import Memory
from app.services.providers import GenerationMetadata,OpenAICompatibleProvider
from app.services.memory import MemoryManager
from app.services.memory.jobs import MemoryExtractionQueue,ERROR_CODES,stamp
from app.services.memory.understanding.extractors import DeepSeekExtractor,Extracted,ExtractionInput
from app.services.memory.understanding.schemas import ExtractionResult,SemanticCandidate
from app.services.memory.understanding.evidence import normalize
from app.services.memory.understanding.reconciler import MemoryReconciler
from app.services.memory.runtime.config import MemoryConfig,strategy,encoded
from app.services.memory.runtime.budget import BudgetLedger
from app.services.memory.runtime.policy import EvidencePolicyV2
from app.services.memory.runtime.scheduler import ConversationSegmentScheduler


class OfflineExtractor:
    """Visible offline rule simulator, not evidence of LLM extraction quality."""
    config_id='mock'
    async def extract(self,value):
        await asyncio.sleep(max(0,min(2000,int(os.getenv('MEMORY_MOCK_DELAY_MS','0'))))/1000)
        candidates=[]
        for m in value.messages:
            if m.role=='user' and m.content.startswith('我') and len(m.content)<=300:
                candidates.append(SemanticCandidate(candidate_id='m'+str(len(candidates)),content=m.content,topic_key='offline.'+m.id,evidence=[dict(message_id=m.id,role='user',start=0,end=len(m.content),quote=m.content)],evidence_level='user_explicit',confidence=.9,uncertainty='none'))
            if len(candidates)==12:break
        return Extracted(ExtractionResult(extractor_version=value.extractor_version,semantic=candidates),GenerationMetadata(model='offline-rule-simulator',finish_reason='stop'),'mock')


def repair_spans(result,messages,*,selective=False):
    raw=result.model_dump();normalized=result.model_copy(deep=True);by_id={m.id:m for m in messages};repairs=[]
    for c in normalized.semantic+normalized.episodic:
        if isinstance(c,SemanticCandidate) and c.content.startswith('现在我'):
            # Canonicalize ONLY a sourced current assertion, never strip past,
            # negation, hypothetical or quoted prefixes. Original text survives
            # both in the source and raw normalization receipt.
            asserted=any(m.role=='user' and m.id in {e.message_id for e in c.evidence} and any(normalize(clause)==normalize(c.content) for clause in re.split(r'[，。；\n]',m.content)) for m in messages)
            if asserted:
                previous=c.content;c.content=c.content[2:]
                repairs.append(dict(candidate_id=c.candidate_id,type='current_assertion_prefix',old=previous,new=c.content))
        spans=list(c.evidence)+[e for p in getattr(c,'discussion',[]) for e in p.evidence]
        for e in spans:
            m=by_id.get(e.message_id)
            if m is None or m.role!=e.role:continue
            if 0<=e.start<e.end<=len(m.content) and m.content[e.start:e.end]==e.quote:continue
            first=m.content.find(e.quote)
            if first>=0 and m.content.find(e.quote,first+1)<0:
                repairs.append(dict(candidate_id=c.candidate_id,message_id=m.id,old=[e.start,e.end],new=[first,first+len(e.quote)]))
                e.start,e.end=first,first+len(e.quote)
        if selective:
            for field in ('valid_from','valid_to'):
                value=getattr(c,field)
                if value and not any(value in e.quote or value.replace('.000000Z','Z') in e.quote for e in c.evidence):
                    # Message time is the Episode source range, not an asserted
                    # claim validity. An invented validity must stay unknown.
                    repairs.append(dict(candidate_id=c.candidate_id,type='unasserted_validity_omitted',field=field,old=value))
                    setattr(c,field,None)
            for point in getattr(c,'discussion',[]):
                if point.perspective=='agreed_conclusion' or len(point.evidence)!=1:continue
                e=point.evidence[0];m=by_id.get(e.message_id)
                if m and m.role==e.role and m.content[e.start:e.end]==e.quote and point.content!=e.quote:
                    # Keep the located utterance, not a model paraphrase of it.
                    # Agreement still requires the unchanged confirmation gate.
                    repairs.append(dict(candidate_id=c.candidate_id,type='discussion_quote_only',old=point.content,new=e.quote))
                    point.content=e.quote
        # Discussion citations already supplied by the model may be missing
        # from the episode's source union. Add ONLY exact, allowed citations;
        # never invent a message, quote, role or interpretation.
        existing={(e.message_id,e.role,e.start,e.end,e.quote) for e in c.evidence}
        for p in getattr(c,'discussion',[]):
            for e in p.evidence:
                m=by_id.get(e.message_id);key=(e.message_id,e.role,e.start,e.end,e.quote)
                if m and m.role==e.role and 0<=e.start<e.end<=len(m.content) and m.content[e.start:e.end]==e.quote and key not in existing:
                    c.evidence.append(e.model_copy());existing.add(key)
                    repairs.append(dict(candidate_id=c.candidate_id,message_id=m.id,type='discussion_source_union'))
    try:normalized=ExtractionResult.model_validate(normalized.model_dump())
    except ValueError:raise AppError('invalid_output','兼容校验后的候选超出结构限制；未写入记忆。',422) from None
    return raw,normalized,repairs


class MemoryWorker:
    def __init__(self,control,*,extractor_factory=None,clock=None,lease_seconds=120):
        control.require();self.control=control;self.sessions=control.sessions
        self.clock=clock or (lambda:datetime.now(timezone.utc));self.queue=MemoryExtractionQueue(self.sessions,clock=self.clock,lease_seconds=lease_seconds)
        self.ledger=BudgetLedger(self.sessions,clock=self.clock);self.scheduler=ConversationSegmentScheduler(control,clock=self.clock)
        self.extractor_factory=extractor_factory;self.last_error=None

    def _extractor(self,config):
        if self.extractor_factory:return self.extractor_factory(config)
        if config.provider=='mock':return OfflineExtractor()
        selected=self.control.providers.get(config.provider)
        provider=OpenAICompatibleProvider(replace(selected.config,json_object=True))
        return DeepSeekExtractor(provider,cloud_authorized=True,max_calls=1,max_input_bytes=config.input_bytes,max_output_tokens=config.output_tokens,timeout_seconds=config.timeout_seconds,
            policy_prompt='优先保留有长期价值的原文命题。content 精确摘录原文完整命题，不改写成“用户如何”。不用改写来推断人格。事件用原文和 discussion 保存讨论。明确纠错/观点变化时，evidence 引用包含新旧命题的整条用户消息；content 仅摘录最新命题（可去掉“现在”）；effect 用 correct/replace，target_memory_id 与 topic_key 必须复用已有目标。严禁把未经认可的助手分析当用户事实。没有价值的闲聊返回空列表。Schema 字符索引按 Unicode 计算；不确定的时间使用 null。'+
                ('本次为低频选择性整理：semantic 至多3条，只选稳定背景、长期目标、明确偏好、持续项目或用户强调的重要事实；临时情绪、自我评价和第三方解释不进入Core。episodic 至多2条，简短title；content选最能代表讨论的简短原文，逐字引用，不改写心理画像。discussion保留必要原文观点和未解决问题。允许全部为空。' if config.selective_sync else ''))

    async def _bounded_extract(self,extractor,value,config,lease):
        started=time.monotonic();task=asyncio.create_task(extractor.extract(value))
        interval=max(.2,min(10,self.queue.lease_seconds/3))
        try:
            while not task.done():
                remaining=config.timeout_seconds-(time.monotonic()-started)
                if remaining<=0:raise TimeoutError()
                done,_=await asyncio.wait([task],timeout=min(interval,remaining))
                if done:break
                # Finite renewals, bounded by request deadline above.
                await asyncio.to_thread(self.queue.renew,lease)
            return await task
        finally:
            if not task.done():
                task.cancel();await asyncio.gather(task,return_exceptions=True)

    async def run_once(self,*,force_seal=False):
        self.last_error=None
        state=self.control.read()
        if not state['enabled'] or state['paused']:return None
        config=MemoryConfig.model_validate(json.loads(state['config_json']))
        if strategy(config,self.control.providers.get(config.provider))[0]!=state['config_hash']:
            self.last_error='configuration_changed';return None
        self.scheduler.tick(force=force_seal)
        self.queue.reclaim_expired();self.ledger.recover_unknown()
        with self.sessions.begin() as s:
            # Serialize candidate selection, claim and budget reservation.
            s.execute(text('UPDATE memory_runtime SET updated_at=updated_at WHERE id=1'))
            candidates=s.execute(text("SELECT j.id,j.status FROM memory_extraction_jobs j JOIN memory_segments g ON g.job_id=j.id WHERE g.config_hash=:hash AND g.state='queued' AND j.status IN ('queued','retry','alignment_required') AND NOT EXISTS(SELECT 1 FROM generation_attempts a WHERE a.conversation_id=g.conversation_id AND a.status='running') ORDER BY j.created_at,j.id"),dict(hash=state['config_hash'])).all()
            for job in candidates:
                excluded=s.execute(text('SELECT x.message_id FROM memory_exclusions x JOIN memory_extraction_job_sources a ON a.message_id=x.message_id WHERE a.job_id=:id LIMIT 1'),dict(id=job.id)).first()
                if excluded:
                    s.execute(text("UPDATE memory_segments SET state='blocked',error_code='source_excluded' WHERE job_id=:id"),dict(id=job.id))
                    s.execute(text("UPDATE memory_extraction_jobs SET status='dead',last_error='configuration_error',next_retry_at=NULL,updated_at=:now WHERE id=:id"),dict(id=job.id,now=stamp(self.clock())))
                    continue
                if job.status=='alignment_required':
                    try:
                        with s.begin_nested():MemoryReconciler(s).release_version_alignment(job.id)
                    except AppError:continue
            selected=s.execute(text("SELECT j.id FROM memory_extraction_jobs j JOIN memory_segments g ON g.job_id=j.id WHERE g.config_hash=:hash AND g.state='queued' AND j.status IN ('queued','retry') AND (j.next_retry_at IS NULL OR j.next_retry_at<=:now) AND NOT EXISTS(SELECT 1 FROM generation_attempts a WHERE a.conversation_id=g.conversation_id AND a.status='running') ORDER BY j.created_at,j.id LIMIT 1"),dict(hash=state['config_hash'],now=stamp(self.clock()))).scalar_one_or_none()
        if selected is None:return None
        reservation=None;lease=None;local_empty=False
        try:
            with self.sessions.begin() as s:
                lease=self.queue.claim('memory-worker',job_id=selected,session=s)
                if not lease:return None
                excluded=s.execute(text('SELECT x.message_id FROM memory_exclusions x JOIN memory_extraction_job_sources a ON a.message_id=x.message_id WHERE a.job_id=:id'),dict(id=selected)).first()
                if excluded:raise AppError('configuration_error','来源已禁止处理。',409)
                primary_users=list(s.execute(text("SELECT m.content FROM messages m JOIN memory_segment_sources a ON a.message_id=m.id JOIN memory_segments g ON g.id=a.segment_id WHERE g.job_id=:job AND a.is_primary=1 AND m.role='user'"),dict(job=selected)).scalars())
                trivial={'嗯','嗯嗯','哈哈','你好','早上好','晚上好','晚安','谢谢','好的','好','收到','哦','噢'}
                local_empty=lease['result'] is None and bool(primary_users) and all(normalize(value) in trivial for value in primary_users)
                if lease['result'] is None and not local_empty:reservation=self.ledger.reserve(s,lease,config,state['config_hash'])
        except AppError as e:
            self.last_error=e.code;return None  # claim rolled back; no API or retries consumed
        try:
            if local_empty:
                self.queue.checkpoint(lease,dict(schema_version=1,payload=ExtractionResult(extractor_version=lease['extractor_version']).model_dump()),provider='local-filter',model='trivial-input-filter')
                lease=self.queue.get(lease['id'])
            if lease['result'] is None:
                with self.sessions() as s:
                    messages=tuple(MemoryReconciler(s)._sources(lease['id']));manager=MemoryManager(s)
                    # Only already-authorized NEW sources, no old private archive.
                    eligible=text("EXISTS(SELECT 1 FROM memory_sources ms JOIN messages m ON m.id=ms.message_id WHERE ms.memory_id=memories.id) AND NOT EXISTS(SELECT 1 FROM memory_sources ms JOIN messages m ON m.id=ms.message_id WHERE ms.memory_id=memories.id AND (m.rowid<=:start OR EXISTS(SELECT 1 FROM memory_exclusions x WHERE x.message_id=m.id)))")
                    existing=tuple(manager.serialize(m) for m in s.scalars(select(Memory).where(eligible).params(start=state['start_rowid']).order_by(Memory.updated_at.desc(),Memory.id).limit(10)))
                extractor=self._extractor(config)
                if extractor.config_id!=lease['config_id']:raise AppError('configuration_error','提取器配置与任务不匹配。',409)
                extracted=await self._bounded_extract(extractor,ExtractionInput(messages,lease['extractor_version'],existing),config,lease)
                self.ledger.settle(reservation,extracted.metadata.usage)
                raw,normalized,repairs=repair_spans(extracted.result,messages,selective=config.selective_sync)
                with self.sessions.begin() as s:
                    self.queue._fence(s,lease)
                    aid=s.execute(text('SELECT id FROM memory_extraction_attempts WHERE lease_token=:token'),dict(token=lease['lease_token'])).scalar_one()
                    s.execute(text('INSERT INTO memory_extraction_payloads VALUES (:id,:raw,:normalized,:repairs,:now)'),dict(id=aid,raw=encoded(raw),normalized=encoded(normalized.model_dump()),repairs=encoded(repairs),now=stamp(self.clock())))
                self.queue.checkpoint(lease,dict(schema_version=1,payload=normalized.model_dump()),provider=extracted.provider,model=extracted.metadata.model,usage=extracted.metadata.usage)
            decisions=[]
            def apply(s,envelope):
                excluded=set(s.execute(text('SELECT message_id FROM memory_exclusions')).scalars())
                decisions.extend(MemoryReconciler(s,validator=EvidencePolicyV2(excluded,selective=config.selective_sync)).apply(lease['id'],envelope['payload']))
            self.queue.complete(lease,apply)
            with self.sessions.begin() as s:s.execute(text("UPDATE memory_segments SET state='processed' WHERE job_id=:id"),dict(id=lease['id']))
            return dict(job_id=lease['id'],status='succeeded',decisions=decisions)
        except (Exception,asyncio.CancelledError) as e:
            aliases={'incomplete_response':'invalid_output','context_length':'invalid_output','model_not_found':'configuration_error','provider_error':'invalid_output'}
            code=(e.code if e.code in ERROR_CODES else aliases.get(e.code,'internal_error')) if isinstance(e,AppError) else 'timeout' if isinstance(e,TimeoutError) else 'internal_error'
            if reservation:
                with suppress(Exception):self.ledger.settle(reservation,getattr(getattr(e,'metadata',None),'usage',None))
            if self.queue.get(lease['id'])['status']=='leased':
                with suppress(Exception):self.queue.fail(lease,code)
            self.last_error=code
            if isinstance(e,asyncio.CancelledError):raise
            return dict(job_id=lease['id'],status='failed',error_code=code)
