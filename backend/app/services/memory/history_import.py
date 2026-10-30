"""Explicit, local-only import of the three verified DeepTalk text snapshots.

Archive originals remain immutable. Human-reviewed candidates use the existing
source-span validator, queue, reconciler and lifecycle, never a model extractor.
"""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import text
from app.models import Conversation, Message
from app.services.memory.jobs import MemoryExtractionQueue
from app.services.memory.understanding.evidence import EvidenceValidator, EvidenceDecision
from app.services.memory.understanding.reconciler import MemoryReconciler
from app.services.memory.understanding.schemas import ExtractionResult

CONTRACT = 'deeptalk-text-reviewed-v1'


def stable_id(value):
    return str(uuid5(NAMESPACE_URL, CONTRACT + ':' + value))


def load_sources(folder):
    folder = Path(folder)
    manifest = json.loads((folder / 'manifest.json').read_text('utf8'))
    sources = []
    for letter, expected_count, attachment_count in [('a',167,16),('b',14,1),('c',6,1)]:
        name = 'source-' + letter
        info = next(s for s in manifest['sources'] if s['source'] == name)
        assert hashlib.sha256((folder/(name+'.html')).read_bytes()).hexdigest() == info['raw_html_sha256']
        payload = json.loads((folder/(name+'.json')).read_text('utf8'))
        raw = json.loads((folder/(name+'-decoded.json')).read_text('utf8'))['serverResponse']['data']
        assert len(payload['messages']) == expected_count == info['messages']
        positions = {node['id']:i for i,node in enumerate(raw['linear_conversation'])}
        previous = -1
        missing = 0
        for row in payload['messages']:
            original = raw['mapping'][row['node_id']]['message']
            parts = original['content'].get('parts')
            content = '\n'.join(s for s in parts if isinstance(s,str)) if isinstance(parts,list) else original['content'].get('text','')
            assert row['role'] in ('user','assistant') and row['role'] == original['author']['role']
            assert row['source_message_id'] == original['id']
            assert row['sequence'] == positions[row['node_id']] and row['sequence'] > previous
            assert row['content'] == content and row['content_parts'] == parts
            assert row['timestamp_raw'] == original['create_time']
            assert datetime.fromisoformat(row['timestamp']) == datetime.fromtimestamp(row['timestamp_raw'],timezone.utc)
            previous = row['sequence']
            row['_original_metadata'] = original.get('metadata',{})
            missing += len(row['_original_metadata'].get('attachments',[]) or [])
        assert missing == attachment_count
        sources.append(payload)
    return sources


def reviewed_candidates(sources, understanding, folder):
    # Private review text stays with the ignored source package, not in code.
    approved = json.loads((Path(folder)/'reviewed_plan.json').read_text('utf8'))
    result = {key:ExtractionResult.model_validate(value) for key,value in approved.items()}
    assert set(result)=={source['source'][-1] for source in sources}
    expected_titles = {row['title'] for row in understanding['discussion_episodes']}
    assert {c.title for r in result.values() for c in r.episodic} == expected_titles
    assert all(r.extractor_version==CONTRACT for r in result.values())
    return result


class ReviewedHistoryEvidence(EvidenceValidator):
    """Origin Contract: a trusted manual review, restricted to this exact batch.

    Ordinary automated extraction still rejects forwarded/history facts. This
    entry accepts only the reviewed candidate + immutable, verified originals;
    it does not reinterpret an assistant quote or copied report as user evidence.
    """
    def __init__(self, result=None, originals=None):
        self.approved = {c.candidate_id:c.model_dump() for c in result.semantic+result.episodic} if result else None
        self.originals = originals

    def validate(self, candidate, messages):
        if self.approved is not None and self.approved.get(candidate.candidate_id) != candidate.model_dump():
            return EvidenceDecision('reject','unreviewed_history_candidate')
        digest = hashlib.sha256(json.dumps(candidate.model_dump(),ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        ids = {e.message_id for e in candidate.evidence}
        for message in messages:
            if message.id not in ids:continue
            meta = message.metadata
            expected = self.originals.get(message.id) if self.originals is not None else (message.role,message.content,message.created_at)
            if (expected != (message.role,message.content,message.created_at) or meta.get('import_contract') != CONTRACT
                or meta.get('role') != message.role or meta.get('origin') != 'historical_import'
                or meta.get('reviewed_candidates',{}).get(candidate.candidate_id) != digest
                or meta.get('original_content_sha256') != hashlib.sha256(message.content.encode()).hexdigest()):
                return EvidenceDecision('reject','unverified_historical_origin')
        error = self.structural(candidate.evidence,messages)
        if error:return EvidenceDecision('reject',error)
        if candidate.evidence_level != 'user_explicit' or candidate.uncertainty != 'none':
            return EvidenceDecision('reject','unreviewed_history_inference')
        for point in getattr(candidate,'discussion',[]):
            if self.structural(point.evidence,messages) or not any(point.content==e.quote for e in point.evidence):
                return EvidenceDecision('reject','historical_discussion_span')
            role = {'user_view':'user','assistant_interpretation':'assistant'}.get(point.perspective)
            if point.perspective=='agreed_conclusion' or role and not any(e.role==role for e in point.evidence):
                return EvidenceDecision('reject','historical_discussion_role')
        if not hasattr(candidate,'discussion') and not any(e.role=='user' and candidate.content==e.quote for e in candidate.evidence):
            return EvidenceDecision('reject','historical_core_not_user_quote')
        return EvidenceDecision('active','human_reviewed_historical_quote')


def import_history(sessions, folder):
    sources = load_sources(folder)
    understanding = json.loads((Path(folder)/'understanding.json').read_text('utf8'))
    reviewed = reviewed_candidates(sources,understanding,folder)
    approvals = {}
    for result in reviewed.values():
        for candidate in result.semantic+result.episodic:
            digest = hashlib.sha256(json.dumps(candidate.model_dump(),ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            for e in candidate.evidence:approvals.setdefault(e.message_id,{})[candidate.candidate_id]=digest
    originals = {}
    added = 0
    with sessions.begin() as session:
        assert session.execute(text('SELECT version FROM schema_info')).scalar_one() == 7
        assert not session.execute(text('SELECT enabled FROM memory_runtime')).scalar_one()
        for source in sources:
            cid = stable_id(source['source_conversation_id'])
            first,last = source['messages'][0],source['messages'][-1]
            if session.get(Conversation,cid) is None:
                session.add(Conversation(id=cid,title='DeepTalk · '+source['title'],created_at=first['timestamp'],updated_at=last['timestamp']))
                session.flush()
            for record in source['messages']:
                mid = stable_id(record['source_conversation_id']+':'+record['source_message_id'])
                created = datetime.fromisoformat(record['timestamp']).astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00','Z')
                originals[mid] = (record['role'],record['content'],created)
                existing = session.get(Message,mid)
                if existing is not None:
                    assert (existing.role,existing.content,existing.created_at) == originals[mid]
                    continue
                meta = {k:v for k,v in record.items() if k not in ('content','_original_metadata')}
                meta.update(origin='historical_import',source_kind='historical_import',source_system='chatgpt',
                    import_contract=CONTRACT,cloud_export_allowed=False,reviewed_candidates=approvals.get(mid,{}),
                    source_metadata=record['_original_metadata'],attachments_missing=bool(record['_original_metadata'].get('attachments')),snapshot_completeness=source['completeness'],
                    source_url=source['url'],original_content_sha256=hashlib.sha256(record['content'].encode()).hexdigest())
                session.add(Message(id=mid,conversation_id=cid,role=record['role'],content=record['content'],created_at=created,metadata_json=meta))
                added += 1
    queue = MemoryExtractionQueue(sessions)
    receipt = []
    for letter,result in reviewed.items():
        ids = sorted({e.message_id for c in result.semantic+result.episodic for e in c.evidence})
        job = queue.enqueue(ids,CONTRACT,config_id='local-human-review',max_attempts=1)
        if job['status']=='succeeded':continue
        lease = queue.claim('deeptalk-local-review',job_id=job['id'])
        queue.checkpoint(lease,dict(schema_version=1,payload=result.model_dump()),provider='local-human-review',model=None)
        def apply(session,envelope):
            receipt.extend(MemoryReconciler(session,validator=ReviewedHistoryEvidence(result,originals)).apply(lease['id'],envelope['payload']))
        queue.complete(lease,apply)
    return dict(original_messages=187,added_messages=added,missing_attachments=18,
                episodes=sum(1 for r in receipt if r['episode_id']),core=sum(1 for r in receipt if r['memory_id']),
                decisions=[dict(candidate=r['candidate_id'],action=r['action'],reason=r['reason']) for r in receipt],
                cloud_calls=0,contract=CONTRACT)
