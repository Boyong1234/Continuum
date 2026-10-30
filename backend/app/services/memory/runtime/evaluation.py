"""Independent proposal/validator/persistence measurements, not mock LLM scores."""
import hashlib
import json
from datetime import datetime,timezone,timedelta
from sqlalchemy import text
from app.models import Conversation,Message
from app.services.memory.jobs import MemoryExtractionQueue
from app.services.memory.understanding.schemas import ExtractionResult,SemanticCandidate,EpisodicCandidate
from app.services.memory.understanding.segmenter import SourceMessage
from app.services.memory.understanding.reconciler import MemoryReconciler
from app.services.memory.runtime.policy import EvidencePolicyV2


FROZEN_SHA256='e7c92d60f3fb2985d13f287ba16659565120ddb43ed9ddabe4ece46e815a3da0'


def fraction(n,d):return dict(numerator=n,denominator=d,value=n/d if d else None)


def evaluate_independent(path,sessions):
    raw=path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=FROZEN_SHA256:raise ValueError('Frozen independent gold changed')
    cases=json.loads(raw)['cases'];details=[];queue=MemoryExtractionQueue(sessions)
    kept=correct=active=unsafe=references=valid_references=created=bad_created=replays=duplicates=0;expected=sum(c['supported'] for c in cases)
    for case in cases:
        with sessions.begin() as s:
            conv=Conversation(title='Independent '+case['id']);s.add(conv);s.flush();messages=[]
            for i,(role,content) in enumerate(case['messages']):
                row=Message(conversation_id=conv.id,role=role,content=content,created_at=(datetime(2026,1,1,tzinfo=timezone.utc)+timedelta(days=i)).isoformat(timespec='microseconds').replace('+00:00','Z'));s.add(row);s.flush()
                messages.append(SourceMessage(row.id,row.conversation_id,row.role,row.content,row.created_at))
            if not any(m.role=='user' for m in messages):
                row=Message(conversation_id=conv.id,role='user',content='继续讨论。',created_at='2026-01-10T00:00:00.000000Z');s.add(row);s.flush();messages.append(SourceMessage(row.id,row.conversation_id,row.role,row.content,row.created_at))
        original_count=len(case['messages'])
        evidence=[dict(message_id=m.id,role=m.role,start=0,end=len(m.content),quote=m.content) for m in messages[:original_count]]
        values=dict(candidate_id=case['id'],content=case['content'],evidence=evidence,evidence_level=case['evidence_level'],confidence=.9,uncertainty=case['uncertainty'])
        candidate=EpisodicCandidate(**values,title=case['category'],importance=.6) if case['kind']=='episodic' else SemanticCandidate(**values,topic_key='heldout.'+case['id'])
        # Gold is consumed only BELOW validation, never in extractor/reconciler.
        decision=EvidencePolicyV2().validate(candidate,messages)
        keep=decision.disposition!='reject';kept+=keep;correct+=keep and case['supported'];active+=decision.disposition=='active';unsafe+=decision.disposition=='active' and not case['supported']
        if keep:
            references+=len(evidence);valid_references+=sum(any(m.id==e['message_id'] and m.role==e['role'] and m.content[e['start']:e['end']]==e['quote'] for m in messages) for e in evidence)
        def apply(version,align=False):
            job=queue.enqueue([m.id for m in messages],version,config_id='mock')
            if align:
                with sessions.begin() as s:MemoryReconciler(s).release_version_alignment(job['id'])
            lease=queue.claim('independent-evaluation',job_id=job['id'])
            result=ExtractionResult(extractor_version=version,**{case['kind']:[candidate]})
            queue.checkpoint(lease,dict(schema_version=1,payload=result.model_dump()),provider='mock',model='injected-proposal-fixture')
            result_rows=[]
            queue.complete(lease,lambda s,r:result_rows.extend(MemoryReconciler(s,validator=EvidencePolicyV2()).apply(job['id'],r['payload'])))
            return result_rows
        result=apply('heldout-b2-v1')[0]
        wrote=bool(result['memory_id'] or result['episode_id']);created+=wrote;bad_created+=wrote and not case['supported']
        with sessions() as s:before=s.execute(text('SELECT (SELECT count(*) FROM memories)+(SELECT count(*) FROM episodes)')).scalar_one()
        apply('heldout-b2-v2',True);replays+=1
        with sessions() as s:after=s.execute(text('SELECT (SELECT count(*) FROM memories)+(SELECT count(*) FROM episodes)')).scalar_one()
        duplicates+=after-before
        details.append(dict(id=case['id'],category=case['category'],gold_supported=case['supported'],validator=decision.disposition,reason=decision.reason,action=result['action'],persisted=wrote))
    return dict(dataset_version='p2b2-independent-60-v1',sha256=FROZEN_SHA256,cases=len(cases),
        extractor_quality=dict(status='NOT_RUN_ON_60',reason='Injected adversarial proposals, not model-generated candidates; real extractor measured separately'),
        validator=dict(precision=fraction(correct,kept),supported_retention=fraction(correct,expected),unsupported_tentative=fraction(kept-correct,kept),unsupported_active=fraction(unsafe,active),source_attribution=fraction(valid_references,references)),
        reconciler=dict(replay_added_memories=fraction(duplicates,replays)),
        final_persistence=dict(supported_precision=fraction(created-bad_created,created),unsupported=fraction(bad_created,created)),
        release_gate=unsafe==0 and duplicates==0 and valid_references==references,
        details=details,real_model_calls=0)
