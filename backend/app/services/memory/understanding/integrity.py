"""B1 checks for startup/backup/restore; exact evidence is revalidated locally."""
import json

from app.maintenance.validation import require,validate_utc
from app.services.memory.understanding.schemas import SemanticCandidate,EpisodicCandidate,ExtractionResult
from app.services.memory.understanding.segmenter import SourceMessage
from app.services.memory.understanding.evidence import EvidenceValidator


def validate_understanding(records):
    messages={m['id']:SourceMessage(m['id'],m['conversation_id'],m['role'],m['content'],m['created_at'],json.loads(m['metadata'])) for m in records['messages']}
    jobs={j['id']:j for j in records['memory_extraction_jobs']}
    sources={id:[] for id in jobs}
    for s in records['memory_extraction_job_sources']:sources[s['job_id']].append(messages[s['message_id']])
    episodes={e['id']:e for e in records['episodes']}
    memories={m['id']:m for m in records['memories']}
    memory_sources={mid:set() for mid in memories}
    for s in records['memory_sources']:memory_sources[s['memory_id']].add(s['message_id'])
    episode_sources={id:set() for id in episodes}
    for s in records['episode_sources']:episode_sources[s['episode_id']].add(s['message_id'])
    for e in episodes.values():
        c=EpisodicCandidate.model_validate(json.loads(e['candidate_json']))
        ids={s.message_id for s in c.evidence}
        require(ids==episode_sources[e['id']] and ids and all(id in messages for id in ids),'invalid_episode','Episode source mismatch')
        require(all(messages[id].conversation_id==e['conversation_id'] for id in ids),'invalid_episode','Episode conversation mismatch')
        decision=EvidenceValidator().validate(c,list(messages.values()))
        require(decision.disposition!='reject' and (e['state']!='active' or decision.disposition=='active'),'invalid_episode','Unsupported episode')
        times=[messages[id].created_at for id in ids]
        require(e['started_at']==min(times) and e['ended_at']==max(times),'invalid_episode','Episode time mismatch')
        validate_utc(e['started_at']);validate_utc(e['ended_at'])
        require((e['title'],e['content'],e['importance'])==(c.title,c.content,c.importance),'invalid_episode','Episode candidate mismatch')
        visited={e['id']};target=e['supersedes']
        while target:
            require(target in episodes and target not in visited,'invalid_episode','Episode replacement cycle')
            visited.add(target);target=episodes[target]['supersedes']
    for link in records['episode_memory_links']:
        require(link['episode_id'] in episodes and link['memory_id'] in memories,'invalid_episode','Episode semantic link missing')
    decision_ids={}
    for d in records['extraction_decisions']:
        job=jobs[d['job_id']]
        require(job['status']=='succeeded','invalid_decision','Applied candidate has no completed job')
        c=(SemanticCandidate if d['kind']=='semantic' else EpisodicCandidate).model_validate(json.loads(d['candidate_json']))
        receipt=ExtractionResult.model_validate(json.loads(job['result_json'])['payload'])
        candidates=receipt.semantic if d['kind']=='semantic' else receipt.episodic
        require(receipt.extractor_version==job['extractor_version'] and any(x.model_dump()==c.model_dump() for x in candidates) and c.candidate_id==d['candidate_id'],'invalid_decision','Decision receipt mismatch')
        decision_ids.setdefault(job['id'],set()).add(c.candidate_id)
        validation=EvidenceValidator().validate(c,sources[job['id']])
        if d['disposition']!='reject':
            require(validation.disposition!='reject','invalid_decision','Retained unsupported structural evidence')
            require(d['disposition']!='active' or validation.disposition=='active','invalid_decision','Unsupported active decision')
        if d['memory_id']:
            # Aligned wording can refer to retained historical evidence rather
            # than adding an unsupported paraphrase to the original memory.
            require(d['memory_id'] in memories,'invalid_decision','Missing semantic target')
            if d['action'] not in ('aligned','deferred'):
                require({s.message_id for s in c.evidence}<=memory_sources[d['memory_id']],'invalid_decision','Memory evidence link missing')
        if d['episode_id']:require(d['episode_id'] in episodes,'invalid_decision','Missing episode target')
    for id,ids in decision_ids.items():
        receipt=ExtractionResult.model_validate(json.loads(jobs[id]['result_json'])['payload'])
        require(ids=={c.candidate_id for c in receipt.semantic+receipt.episodic},'invalid_decision','Partially applied candidates')
    for a in records['version_alignments']:
        job,previous=jobs[a['job_id']],jobs[a['previous_job_id']]
        require(previous['status']=='succeeded' and job['source_fingerprint']==previous['source_fingerprint'] and job['extractor_version']!=previous['extractor_version'],'invalid_alignment','Invalid predecessor')
        require(job['status']!='alignment_required','invalid_alignment','Released job still blocked')
        validate_utc(a['created_at'])
