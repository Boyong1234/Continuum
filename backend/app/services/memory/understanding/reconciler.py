"""Conservative reconciliation, called only inside the durable queue transaction."""
import json
import re
from difflib import SequenceMatcher

from sqlalchemy import text, select

from app.core.config import AppError
from app.models import Memory, Message, uid, utcnow
from app.services.memory import MemoryManager
from app.services.memory.lifecycle import MemoryLifecycle
from app.services.memory.understanding.schemas import ExtractionResult
from app.services.memory.understanding.segmenter import SourceMessage
from app.services.memory.understanding.evidence import EvidenceValidator, normalize, EvidenceDecision
from app.services.dialogue import project_current,normalized_topic


def dump(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)


def equivalent(value):
    # Small explicit equivalence set, not a claim of general semantic similarity.
    return normalize(value).replace('长期偏爱','长期偏好').replace('python语言','python')


def known_slot(value):
    """Only identify clearly exclusive preference domains; topic_key is untrusted."""
    if not re.search(r'偏好|偏爱|更喜欢',value):return None
    domains=[('drink',('咖啡','茶')),('programming',('python','rust','java')),('explanation',('简洁解释','详细解释'))]
    found=[domain for domain,words in domains if any(word in value.casefold() for word in words)]
    return found[0] if len(found)==1 else None


class MemoryReconciler:
    def __init__(self,session,*,validator=None):
        self.session=session; self.manager=MemoryManager(session); self.lifecycle=MemoryLifecycle(session)
        self.validator=validator or EvidenceValidator()
        if session.execute(text('SELECT version FROM schema_info')).scalar_one()<5:
            raise AppError('migration_required','B1 写入仅支持显式迁移的隔离 v5 数据集。',409)

    def release_version_alignment(self,job_id):
        """Explicit B1 entry; successful predecessor + this policy required.

        This does not bypass candidate alignment: every result still passes apply.
        The ordinary P2-A queue remains closed to unreviewed version changes.
        """
        self.session.execute(text('UPDATE memory_extraction_jobs SET updated_at=updated_at WHERE 0'))
        job=self.session.execute(text('SELECT * FROM memory_extraction_jobs WHERE id=:id'),dict(id=job_id)).mappings().one()
        existing=self.session.execute(text('SELECT job_id FROM version_alignments WHERE job_id=:id'),dict(id=job_id)).first()
        if existing:return
        if job['status']!='alignment_required':raise AppError('alignment_required','任务不处于版本对齐门禁。',409)
        previous=self.session.execute(text("SELECT id FROM memory_extraction_jobs WHERE source_fingerprint=:fp AND status='succeeded' AND id<>:id ORDER BY created_at,id LIMIT 1"),dict(fp=job['source_fingerprint'],id=job_id)).scalar_one_or_none()
        if not previous:raise AppError('alignment_required','须先完成同来源旧版本任务，再执行显式候选对齐。',409)
        # Only B1 receipts qualify; a generic P2-A callback is not semantic review.
        old=self.session.execute(text('SELECT result_json FROM memory_extraction_jobs WHERE id=:id'),dict(id=previous)).scalar_one()
        try:old_result=ExtractionResult.model_validate(json.loads(old)['payload'])
        except (ValueError,KeyError,TypeError):raise AppError('alignment_required','旧任务没有 B1 结构化候选，不能自动释放。',409) from None
        applied=set(self.session.execute(text('SELECT candidate_id FROM extraction_decisions WHERE job_id=:id'),dict(id=previous)).scalars())
        if applied!={c.candidate_id for c in old_result.semantic+old_result.episodic}:
            raise AppError('alignment_required','旧任务未完成全部 B1 候选证据审核。',409)
        self.session.execute(text('INSERT INTO version_alignments VALUES (:id,:previous,\'reconciler-v1\',:now)'),dict(id=job_id,previous=previous,now=utcnow()))
        self.session.execute(text("UPDATE memory_extraction_jobs SET status='queued',updated_at=:now WHERE id=:id"),dict(id=job_id,now=utcnow()))

    def _sources(self,job_id):
        rows=self.session.execute(text('SELECT m.id,m.conversation_id,m.role,m.content,m.created_at,m.metadata FROM messages m JOIN memory_extraction_job_sources s ON s.message_id=m.id WHERE s.job_id=:id ORDER BY m.created_at,m.id'),dict(id=job_id)).all()
        return [SourceMessage(*r[:5],metadata=json.loads(r[5])) for r in rows]

    def _attach_sources(self,memory,ids):
        if set(ids)-set(memory['source_message_ids']):
            return self.manager.update_memory(memory['id'],dict(source_message_ids=sorted(set(ids)|set(memory['source_message_ids'])),expected_version=memory['version']),commit=False)
        return memory

    def reconcile_completed_exams(self,messages):
        """Correct an existing stage claim; never admit arbitrary short-term facts."""
        facts=project_current([m for m in messages if m.id not in getattr(self.validator,'excluded',())]);originals={m.id:m for m in messages}
        active=self.manager.search_memories(memory_types=('user_statement','user_confirmed'))
        for old in active:
            pending=re.fullmatch(r'(?:我|用户)(?:最近|目前|现在)?(?:正在)?(?:准备|备考)(.{1,24}?)(?:考试)?[。.!！]?',normalized_topic(old['content']))
            if pending is None:continue
            completed=next((f for f in facts if f.topic==pending[1] and f.state=='exam_completed'),None)
            if completed is None:continue
            source=originals[completed.source_id]
            old_times=[self.session.get(Message,id).created_at for id in old['source_message_ids']]
            if source.created_at<=max(old_times):continue
            if not old['topic_key']:
                old=self.lifecycle.update(old['id'],dict(topic_key='exam.'+completed.topic),expected_version=old['version'],reason='topic_update',commit=False)
            replacement=self.manager.create_memory(dict(memory_type='user_statement',content=source.content,
                source_message_ids=[source.id],confidence=1.0),commit=False)
            replacement=self.lifecycle.update(replacement['id'],dict(topic_key=old['topic_key']),expected_version=replacement['version'],reason='topic_update',commit=False)
            old=self._attach_sources(old,[source.id])
            self.lifecycle.update(old['id'],dict(state='superseded',superseded_by=replacement['id']),expected_version=old['version'],reason='supersede',commit=False)

    def _create(self,c,decision):
        ids=sorted({e.message_id for e in c.evidence})
        kind=('user_confirmed' if c.evidence_level=='user_confirmed' else 'user_statement') if decision.disposition=='active' else 'model_inference'
        value=self.manager.create_memory(dict(memory_type=kind,content=c.content,source_message_ids=ids,confidence=c.confidence),commit=False)
        return self.lifecycle.update(value['id'],dict(topic_key=c.topic_key,valid_from=c.valid_from,valid_to=c.valid_to),expected_version=value['version'],reason='topic_update',commit=False)

    def _semantic(self,c,decision,messages):
        values=[self.manager.serialize(m) for m in self.session.scalars(select(Memory).order_by(Memory.created_at,Memory.id))]
        sources={e.message_id for e in c.evidence}
        target=next((m for m in values if m['id']==c.target_memory_id),None)
        if c.effect=='correct' and target and target['status']=='active' and target['topic_key']==c.topic_key:
            ordered=sorted(messages,key=lambda m:(m.created_at,m.id))
            for i,m in enumerate(ordered[:-1]):
                following=ordered[i+1]
                quoted={normalize(target['content']),normalize('你'+target['content'][1:]) if target['content'].startswith('我') else ''}
                anchored=m.id in sources and following.id in sources and m.role=='assistant' and normalize(m.content) in quoted
                denial=following.role=='user' and re.fullmatch(r'你记错了[，, ]*我(从来)?没有说过这个[。.!！]?',following.content)
                if anchored and denial and normalize(c.content) in normalize(following.content):
                    target=self._attach_sources(target,sources)
                    self.lifecycle.update(target['id'],dict(state='archived'),expected_version=target['version'],reason='retract',commit=False)
                    return 'retracted',target['id'],EvidenceDecision('tentative','explicit_retraction')
        same=[m for m in values if equivalent(m['content'])==equivalent(c.content) and c.effect not in ('correct','replace') and (m['status'] in ('active','tentative') or sources<=set(m['source_message_ids']))]
        if same:
            m=same[0]
            if decision.disposition!='active':
                return 'aligned',m['id'],EvidenceDecision('tentative','unconfirmed_duplicate_no_evidence_added')
            if decision.disposition=='active' and m['status']=='tentative' and c.evidence_level=='user_confirmed':
                confirmation=next((e.message_id for e in c.evidence if e.role=='user' and e.message_id not in m['source_message_ids']),None)
                if confirmation:m=self.lifecycle.update(m['id'],dict(state='active'),expected_version=m['version'],reason='confirm',confirmation_message_id=confirmation,commit=False)
            m=self._attach_sources(m,sources)
            return 'duplicate',m['id'],decision
        # Align changed extractor wording on the exact same evidence before any
        # insert, even if a model changed its topic_key. Never silently edit text.
        shared=[m for m in values if sources & set(m['source_message_ids']) and SequenceMatcher(None,equivalent(m['content']),equivalent(c.content)).ratio()>=.65]
        if shared and c.effect not in ('correct','replace'):
            return 'aligned',shared[0]['id'],EvidenceDecision('tentative','same_evidence_wording_deferred')
        related=[m for m in values if m['topic_key']==c.topic_key and m['status']=='active']
        if c.effect=='supplement' and decision.disposition=='active':
            supported=[m for m in related if normalize(m['content']) in normalize(c.content)]
            if len(supported)==1 and sources-set(supported[0]['source_message_ids']):
                old=supported[0]
                updated=self.manager.update_memory(old['id'],dict(content=c.content,source_message_ids=sorted(sources|set(old['source_message_ids'])),expected_version=old['version']),commit=False)
                return 'supplement',updated['id'],decision
        if c.effect in ('correct','replace'):
            user='\n'.join(m.content for m in messages if m.role=='user' and m.id in sources)
            explicit=bool(re.search(r'以前|原来|不是|记错|改正|改变|现在已经|现在我|不再',user))
            # Both old and new propositions must be located in the correction;
            # supplied target IDs/topic keys never suffice by themselves.
            valid=target and target['status']=='active' and target['topic_key']==c.topic_key and normalize(target['content']) in normalize(user) and normalize(c.content) in normalize(user)
            old_times=[self.session.get(Message,id).created_at for id in target['source_message_ids']] if target else []
            new_times=[m.created_at for m in messages if m.role=='user' and m.id in sources]
            if not (valid and explicit and decision.disposition=='active' and new_times and max(new_times)>max(old_times)):
                return 'deferred',None,EvidenceDecision('tentative','correction_not_identified')
            replacement=self._create(c,decision)
            target=self._attach_sources(target,sources)
            self.lifecycle.update(target['id'],dict(state='superseded',superseded_by=replacement['id']),expected_version=target['version'],reason='supersede',commit=False)
            return 'superseded',replacement['id'],decision
        conflict=[m for m in related if known_slot(m['content']) is not None and known_slot(m['content'])==known_slot(c.content)]
        if conflict:
            # Conflicting evidence remains visible but neither is current fact.
            if decision.disposition=='active':
                for m in conflict:
                    m=self._attach_sources(m,sources)
                    self.lifecycle.update(m['id'],dict(state='tentative'),expected_version=m['version'],reason='conflict',commit=False)
            decision=EvidenceDecision('tentative','unresolved_conflict')
            value=self._create(c,decision)
            return 'conflict',value['id'],decision
        if related and c.effect!='supplement':
            decision=EvidenceDecision('tentative','topic_collision_unresolved')
        value=self._create(c,decision)
        return ('supplement' if c.effect=='supplement' else 'created'),value['id'],decision

    def _episode(self,c,decision,messages,semantic_ids):
        ids={e.message_id for e in c.evidence}; originals={m.id:m for m in messages}
        previous=self.session.execute(text('SELECT * FROM episodes WHERE conversation_id=:cid ORDER BY created_at,id'),dict(cid=messages[0].conversation_id)).mappings().all()
        supersedes=None
        for old in previous:
            old_ids=set(self.session.execute(text('SELECT message_id FROM episode_sources WHERE episode_id=:id'),dict(id=old['id'])).scalars())
            if not ids&old_ids:continue
            if equivalent(old['content'])==equivalent(c.content):return 'duplicate',old['id'],decision
            if old['title']==c.title and SequenceMatcher(None,equivalent(old['content']),equivalent(c.content)).ratio()>=.65:
                return 'aligned',old['id'],EvidenceDecision('tentative','episode_wording_deferred')
            if old_ids<=ids and normalize(old['content']) in normalize(c.content):supersedes=old['id']
        eid=uid(); times=[originals[id].created_at for id in ids]
        self.session.execute(text('INSERT INTO episodes VALUES (:id,:cid,:title,:content,:state,:importance,:start,:end,:candidate,:supersedes,:now)'),dict(id=eid,cid=messages[0].conversation_id,title=c.title,content=c.content,state=decision.disposition,importance=c.importance,start=min(times),end=max(times),candidate=dump(c.model_dump()),supersedes=supersedes,now=utcnow()))
        for id in sorted(ids):self.session.execute(text('INSERT INTO episode_sources VALUES (:episode,:message)'),dict(episode=eid,message=id))
        for mid in sorted({semantic_ids[id] for id in c.linked_candidate_ids if semantic_ids.get(id)}):
            self.session.execute(text('INSERT INTO episode_memory_links VALUES (:episode,:memory)'),dict(episode=eid,memory=mid))
        return 'created',eid,decision

    def apply(self,job_id,result):
        result=ExtractionResult.model_validate(result)
        job=self.session.execute(text('SELECT * FROM memory_extraction_jobs WHERE id=:id'),dict(id=job_id)).mappings().one()
        if job['status']!='leased' or not job['result_json'] or result.extractor_version!=job['extractor_version'] or json.loads(job['result_json'])['payload']!=result.model_dump():
            raise AppError('invalid_output','候选必须匹配当前任务已暂存的完整结果。',422)
        peers=self.session.execute(text('SELECT id FROM memory_extraction_jobs WHERE source_fingerprint=:fp AND status=\'succeeded\' AND id<>:id'),dict(fp=job['source_fingerprint'],id=job_id)).all()
        if peers and not self.session.execute(text('SELECT job_id FROM version_alignments WHERE job_id=:id'),dict(id=job_id)).first():raise AppError('alignment_required','新版本结果须经显式对齐入口。',409)
        messages=self._sources(job_id); semantic_ids={}; decisions=[]
        self.reconcile_completed_exams(messages)
        for kind,candidates in [('semantic',result.semantic),('episodic',result.episodic)]:
            for c in candidates:
                cached=self.session.execute(text('SELECT * FROM extraction_decisions WHERE job_id=:job AND candidate_id=:cid'),dict(job=job_id,cid=c.candidate_id)).mappings().first()
                if cached:
                    if kind=='semantic':semantic_ids[c.candidate_id]=cached['memory_id']
                    decisions.append(dict(cached));continue
                decision=self.validator.validate(c,messages)
                target=None; action='rejected'
                if decision.disposition!='reject':
                    if kind=='semantic':action,target,decision=self._semantic(c,decision,messages)
                    else:action,target,decision=self._episode(c,decision,messages,semantic_ids)
                row=dict(id=uid(),job_id=job_id,candidate_id=c.candidate_id,kind=kind,candidate_json=dump(c.model_dump()),disposition=decision.disposition,reason=decision.reason,action=action,memory_id=target if kind=='semantic' else None,episode_id=target if kind=='episodic' else None,created_at=utcnow())
                self.session.execute(text('INSERT INTO extraction_decisions VALUES (:id,:job_id,:candidate_id,:kind,:candidate_json,:disposition,:reason,:action,:memory_id,:episode_id,:created_at)'),row)
                if kind=='semantic':semantic_ids[c.candidate_id]=row['memory_id']
                decisions.append(row)
        return decisions
