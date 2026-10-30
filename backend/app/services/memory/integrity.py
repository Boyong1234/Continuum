"""Strict semantic structure checks used by backups and v4 startup."""
import hashlib
import json
from datetime import datetime

from app.maintenance.validation import require, validate_utc
from app.maintenance.revisions.v0004_memory_foundation import baseline_id
from app.services.memory.jobs import canonical_result, source_fingerprint, ERROR_CODES


def parsed(value):
    validate_utc(value)
    return datetime.fromisoformat(value.replace('Z','+00:00'))


def validate_foundation(records):
    messages={row['id']:row for row in records['messages']}
    memories={row['id']:row for row in records['memories']}
    revisions={row['id']:row for row in records['memory_revisions']}
    states={row['memory_id']:row for row in records['memory_lifecycle']}
    require(set(states)==set(memories),'invalid_lifecycle','Missing lifecycle records')
    sources={mid:[] for mid in memories}
    for source in records['memory_sources']:sources[source['memory_id']].append(source['message_id'])
    audit={}
    for row in records['memory_revision_audit']:
        original=revisions.get(row['revision_id'])
        require(original is not None and original['memory_id']==row['memory_id'],'invalid_revision','Revision audit identity mismatch')
        audit.setdefault(row['memory_id'],[]).append(row['revision_number'])
        require(row['change_reason'] in ('legacy_revision','migration_baseline','created','confirm','archive','reopen','conflict','retract','supersede','manual_update','time_update','topic_update'),'invalid_revision','Unknown revision reason')
        if row['change_reason']=='migration_baseline':
            require(row['revision_id']==baseline_id(row['memory_id']),'invalid_revision','Invalid migration baseline identity')
    require({row['revision_id'] for row in records['memory_revision_audit']}==set(revisions),'invalid_revision','Untracked revision')
    for mid,state in states.items():
        require(sorted(audit.get(mid,[]))==list(range(1,state['version']+1)),'invalid_revision','Non-contiguous revision sequence')
        for key in ('valid_from','valid_to'):
            if state[key] is not None:parsed(state[key])
        if state['valid_from'] and state['valid_to']:
            require(parsed(state['valid_to'])>parsed(state['valid_from']),'invalid_time','Invalid validity interval')
        if state['state']=='active':
            require(state['evidence_level']!='model_inferred' and not state['superseded_by'],'invalid_lifecycle','Unconfirmed active memory')
            require(any(messages[id]['role']=='user' for id in sources[mid]),'invalid_sources','Active fact requires user source')
        visited={mid}; target=state['superseded_by']
        if target:
            require(state['topic_key'] is not None and states[target]['topic_key']==state['topic_key'],'invalid_lifecycle','Supersession topic mismatch')
        while target:
            require(target in states and target not in visited,'invalid_lifecycle','Invalid or cyclic supersession chain')
            visited.add(target); target=states[target]['superseded_by']
    jobs={row['id']:row for row in records['memory_extraction_jobs']}
    attempts={row['id']:row for row in records['memory_extraction_attempts']}
    job_sources={id:[] for id in jobs}
    for source in records['memory_extraction_job_sources']:job_sources[source['job_id']].append(source)
    for job in jobs.values():
        ordered=sorted(job_sources[job['id']],key=lambda row:row['position'])
        require(bool(ordered) and [r['position'] for r in ordered]==list(range(len(ordered))),'invalid_job','Invalid job source range')
        original=[messages[source['message_id']] for source in ordered]
        require(len({m['conversation_id'] for m in original})==1 and any(m['role']=='user' for m in original),'invalid_job','Invalid job conversation sources')
        require(original==sorted(original,key=lambda row:(row['created_at'],row['id'])),'invalid_job','Job sources are not canonical')
        rows=[tuple(m[k] for k in ('id','conversation_id','role','content','created_at')) for m in original]
        require(source_fingerprint(rows)==job['source_fingerprint'],'invalid_job','Job source fingerprint mismatch')
        for key in ('next_retry_at','lease_expires_at'):
            if job[key] is not None:parsed(job[key])
        require(job['last_error'] is None or job['last_error'] in ERROR_CODES,'invalid_job','Unsanitized job error')
        require((job['next_retry_at'] is not None)==(job['status']=='retry'),'invalid_job','Retry schedule mismatch')
        if job['status']=='alignment_required':
            require(job['attempts']==0 and job['result_json'] is None and any(other['source_fingerprint']==job['source_fingerprint'] and other['extractor_version']!=job['extractor_version'] for other in jobs.values()),'invalid_job','Invalid version alignment barrier')
        history=[a for a in attempts.values() if a['job_id']==job['id']]
        require(sorted(a['attempt_number'] for a in history)==list(range(1,job['attempts']+1)),'invalid_job','Attempt count mismatch')
        if job['status']=='leased':
            active=[a for a in history if a['lease_token']==job['lease_token']]
            require(len(active)==1 and active[0]['attempt_number']==job['attempts'] and active[0]['lease_owner']==job['lease_owner'] and active[0]['status'] in ('leased','api_succeeded'),'invalid_job','Lease/attempt mismatch')
        if job['result_json'] is not None:
            result=json.loads(job['result_json'])
            require(canonical_result(result)==job['result_json'] and hashlib.sha256(job['result_json'].encode()).hexdigest()==job['result_sha256'],'invalid_job','Staged result digest mismatch')
            receipt=attempts.get(job['result_attempt_id'])
            require(receipt is not None and receipt['job_id']==job['id'],'invalid_job','Result receipt attempt mismatch')
        if job['status']=='succeeded':
            require(any(a['attempt_number']==job['attempts'] and a['status']=='succeeded' for a in history),'invalid_job','Completed job has no successful attempt')
    for attempt in attempts.values():
        require(attempt['job_id'] in jobs,'invalid_job','Attempt has no job')
        parsed(attempt['started_at'])
        if attempt['finished_at'] is not None:parsed(attempt['finished_at'])
        if attempt['finished_at'] is not None:
            require(parsed(attempt['finished_at'])>=parsed(attempt['started_at']),'invalid_time','Attempt finished before start')
        require((attempt['finished_at'] is None)==(attempt['status'] in ('leased','api_succeeded')),'invalid_job','Attempt completion time mismatch')
        job=jobs[attempt['job_id']]
        if attempt['status'] in ('leased','api_succeeded'):
            require(job['status']=='leased' and job['lease_token']==attempt['lease_token'],'invalid_job','Orphan live attempt')
        if attempt['status']=='succeeded':
            require(job['status']=='succeeded' and job['attempts']==attempt['attempt_number'],'invalid_job','Orphan completed attempt')
        require(attempt['error_code'] is None or attempt['error_code'] in ERROR_CODES,'invalid_job','Unsanitized attempt error')
        usage=json.loads(attempt['usage_json'])
        require(isinstance(usage,dict) and usage.get('source') in ('api','unknown'),'invalid_job','Invalid extraction usage source')
        counts=[usage.get(k) for k in ('prompt_tokens','completion_tokens','total_tokens')]
        require(all(type(n) is int and n>=0 for n in counts) if usage['source']=='api' else all(n is None for n in counts),'invalid_job','Invalid extraction usage counts')
