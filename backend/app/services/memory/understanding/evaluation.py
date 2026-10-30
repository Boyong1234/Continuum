"""Score frozen synthetic candidate proposals, independently of extractor mocks."""
import hashlib
import json
from datetime import datetime,timezone,timedelta

from app.services.memory.understanding.schemas import SemanticCandidate,EpisodicCandidate
from app.services.memory.understanding.segmenter import SourceMessage,ConversationSegmenter
from app.services.memory.understanding.evidence import EvidenceValidator


def materialize(case):
    base=datetime(2026,1,1,tzinfo=timezone.utc)
    messages=[SourceMessage(f'{case["id"]}-{i}',case['id'],role,content,(base+timedelta(hours=i*case.get('gap_hours',1))).isoformat(timespec='microseconds').replace('+00:00','Z')) for i,(role,content) in enumerate(case['messages'])]
    indices=case.get('source_indices',[0]);spans=[]
    for index in indices:
        m=messages[index];quote=case.get('quote',m.content) if index==indices[0] else m.content
        start=m.content.index(quote)
        spans.append(dict(message_id='outside' if case.get('missing_source') else m.id,role=case.get('role_override',m.role),start=start+int(case.get('wrong_offset',False)),end=start+len(quote),quote=quote))
    values=dict(candidate_id=case['id'],content=case.get('content',messages[indices[0]].content),evidence=spans,evidence_level=case.get('evidence_level','user_explicit'),confidence=.9,uncertainty=case.get('uncertainty','none'))
    if case.get('kind')=='episodic':
        c=EpisodicCandidate(**values,title=case['category'],importance=.6,continuation=case.get('continuation'))
    else:c=SemanticCandidate(**values,topic_key='evaluation.'+case['id'])
    return messages,c


def evaluate(path):
    raw=path.read_bytes();dataset=json.loads(raw)
    details=[];retained=active=supported=unsupported_active=correct_sources=references=deterministic=gold_positive=retained_positive=0
    for case in dataset['cases']:
        # No gold fields are supplied to the inference/validation path.
        messages,c=materialize(case);decision=EvidenceValidator().validate(c,messages)
        keep=decision.disposition!='reject';is_active=decision.disposition=='active'
        retained+=keep;active+=is_active;supported+=keep and case['supported'];unsupported_active+=is_active and not case['supported']
        gold_positive+=case['supported'];retained_positive+=keep and case['supported']
        if keep:
            expected={messages[i].id for i in case['gold_sources']}
            for e in c.evidence:
                references+=1
                correct_sources+=e.message_id in expected and any(m.id==e.message_id and m.role==e.role and m.content[e.start:e.end]==e.quote for m in messages)
        segmenter=ConversationSegmenter()
        deterministic+=segmenter.build(messages)==segmenter.build(list(reversed(messages)))
        details.append(dict(id=case['id'],category=case['category'],disposition=decision.disposition,reason=decision.reason,gold_supported=case['supported']))
    def metric(n,d):return dict(numerator=n,denominator=d,value=n/d if d else None)
    return dict(dataset_version=dataset['dataset_version'],dataset_sha256=hashlib.sha256(raw).hexdigest(),cases=len(details),scope='local_evidence_gate_on_hand_authored_candidates_not_real_model_quality',
        candidate_precision=metric(supported,retained),retained_unsupported_rate=metric(retained-supported,retained),
        active_unsupported_rate=metric(unsupported_active,active),source_attribution_accuracy=metric(correct_sources,references),
        supported_candidate_retention=metric(retained_positive,gold_positive),segment_reconstruction_determinism=metric(deterministic,len(details)),
        active=active,tentative=retained-active,rejected=len(details)-retained,details=details,real_model_calls=0,api_cost=0)
