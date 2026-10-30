import json
from datetime import datetime
from app.maintenance.validation import require,validate_utc
from app.services.memory.runtime.config import MemoryConfig,CURRENT_POLICY,accepts_source
from app.services.memory.understanding.schemas import ExtractionResult


def validate_runtime(records,connection):
    require(len(records['memory_runtime'])==1 and len(records['memory_schedule_cursor'])==1,'invalid_runtime','Missing runtime/cursor')
    runtime=records['memory_runtime'][0];cursor=records['memory_schedule_cursor'][0]['last_rowid']
    require(len(records['memory_release_gate'])==1,'invalid_runtime','Missing quality gate')
    gate=records['memory_release_gate'][0]
    require(gate['policy_version'] in ('b2-evidence-v1','b2-evidence-v2','b2-evidence-v3',CURRENT_POLICY),'invalid_runtime','Unsupported evidence policy')
    if runtime['config_hash']:
        MemoryConfig.model_validate(json.loads(runtime['config_json']))
        require(len(runtime['config_hash'])==64,'invalid_runtime','Invalid strategy digest')
    else:require(not runtime['enabled'] and not runtime['initialized'],'invalid_runtime','Unconfigured memory cannot run')
    if runtime['enabled']:
        cfg=MemoryConfig.model_validate(json.loads(runtime['config_json']))
        require(cfg.provider=='mock' or runtime['authorized_hash']==runtime['config_hash'],'invalid_runtime','Missing cloud authorization')
        require(cfg.provider=='mock' or runtime['paused'] or gate['status']=='passed','invalid_runtime','Unreleased cloud policy')
    messages={m['id']:m for m in records['messages']}
    arrivals={id:n for n,id in connection.execute('SELECT rowid,id FROM messages')}
    require(runtime['start_rowid']<=cursor<=max(arrivals.values(),default=0),'invalid_runtime','Arrival cursor outside immutable messages')
    jobs={j['id']:j for j in records['memory_extraction_jobs']};attempts={a['id']:a for a in records['memory_extraction_attempts']}
    segments={g['id']:g for g in records['memory_segments']};sources={id:[] for id in segments};primary={}
    exclusions={e['message_id'] for e in records['memory_exclusions']}
    for source in records['memory_segment_sources']:
        sources[source['segment_id']].append(source)
        require(source['arrival_rowid']==arrivals[source['message_id']] and source['arrival_rowid']<=cursor,'invalid_segment','Arrival source mismatch')
        require(messages[source['message_id']]['conversation_id']==segments[source['segment_id']]['conversation_id'],'invalid_segment','Cross-conversation segment')
        if source['is_primary']:
            require(source['message_id'] not in primary,'invalid_segment','Repeated primary source');primary[source['message_id']]=source['segment_id']
    for g in segments.values():
        rows=sorted(sources[g['id']],key=lambda r:r['position']);cfg=MemoryConfig.model_validate(json.loads(g['config_json']))
        require(rows and any(r['is_primary'] for r in rows) and [r['position'] for r in rows]==list(range(len(rows))),'invalid_segment','Missing/noncontiguous segment sources')
        validate_utc(g['last_arrived_at'])
        if g['sealed_at']:validate_utc(g['sealed_at'])
        require((g['state']=='open')==(g['sealed_at'] is None),'invalid_segment','Seal state mismatch')
        if g['job_id']:
            job=jobs[g['job_id']];actual={r['message_id'] for r in records['memory_extraction_job_sources'] if r['job_id']==job['id']}
            require(actual=={r['message_id'] for r in rows},'invalid_segment','Job source set changed')
            require(job['config_id']==cfg.provider and job['extractor_version']==cfg.extractor_version+'-'+g['config_hash'][:12],'invalid_segment','Job policy mismatch')
            if g['state']=='processed':require(job['status']=='succeeded','invalid_segment','Processed segment lacks result')
    if runtime['initialized']:
        for id,arrival in arrivals.items():
            if runtime['start_rowid']<arrival<=cursor and accepts_source(messages[id]['role'],messages[id]['metadata'],MemoryConfig.model_validate(json.loads(runtime['config_json']))) and id not in exclusions:
                require(id in primary,'invalid_segment','Permanently omitted authorized source')
    for b in records['memory_budget_reservations']:
        a=attempts[b['attempt_id']]
        require(a['job_id']==b['job_id'],'invalid_budget','Budget/attempt mismatch')
        require(b['day']==datetime.fromisoformat(b['created_at'].replace('Z','+00:00')).date().isoformat(),'invalid_budget','Budget day mismatch')
        if b['finished_at']:validate_utc(b['finished_at'])
        usage=json.loads(b['usage_json'])
        require(usage.get('source') in ('unknown','api'),'invalid_budget','Invalid usage source')
        if b['state']=='settled':
            values=[usage.get(k) for k in ('prompt_tokens','completion_tokens','total_tokens')]
            require(usage['source']=='api' and all(type(n) is int and n>=0 for n in values) and b['actual_tokens']==max(values[2],values[0]+values[1]),'invalid_budget','Invalid settled usage')
        else:require(usage['source']=='unknown' and b['actual_tokens'] is None,'invalid_budget','Unknown budget cannot invent usage')
    for payload in records['memory_extraction_payloads']:
        a=attempts[payload['attempt_id']]
        raw=ExtractionResult.model_validate(json.loads(payload['raw_json']));norm=ExtractionResult.model_validate(json.loads(payload['normalized_json']))
        require(raw.extractor_version==norm.extractor_version==jobs[a['job_id']]['extractor_version'],'invalid_payload','Payload policy mismatch')
        require(isinstance(json.loads(payload['repairs_json']),list),'invalid_payload','Invalid normalization receipt')
    for block in records['memory_suppressions']:
        table='memories' if block['kind']=='semantic' else 'episodes'
        require(block['target_id'] in {r['id'] for r in records[table]},'invalid_suppression','Missing suppressed object')
    history={h['job_id']:h for h in records['memory_segment_job_history']}
    for g in segments.values():
        if g['job_id']:require(g['job_id'] in history and history[g['job_id']]['segment_id']==g['id'],'invalid_segment','Missing segment job audit')
    for h in history.values():
        cfg=MemoryConfig.model_validate(json.loads(h['config_json']));job=jobs[h['job_id']]
        require(job['config_id']==cfg.provider and job['extractor_version']==cfg.extractor_version+'-'+h['config_hash'][:12],'invalid_segment','Invalid historical policy')
