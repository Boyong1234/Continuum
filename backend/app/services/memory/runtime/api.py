import json
from fastapi import APIRouter,Request
from pydantic import BaseModel,ConfigDict
from sqlalchemy import text
from app.core.config import AppError
from app.models import utcnow,Message,uid
from app.services.memory import MemoryManager
from app.services.memory.understanding.continuity import EpisodeStore
from app.services.memory.runtime.config import MemoryConfig
from app.services.memory.runtime.policy import EvidencePolicyV2
from app.services.memory.understanding.schemas import EpisodicCandidate
from app.services.memory.understanding.segmenter import SourceMessage


router=APIRouter(prefix='/memory-runtime')


class Configure(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    config: MemoryConfig
    enabled: bool=False
    authorize_cloud: bool=False


class Pause(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    paused: bool


class EpisodeCorrection(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    source_message_id: str


@router.get('')
def status(request:Request):return request.app.state.memory_control.status()


@router.post('/configure')
def configure(body:Configure,request:Request):return request.app.state.memory_control.configure(body.config,enabled=body.enabled,authorize_cloud=body.authorize_cloud)


@router.post('/pause')
def pause(body:Pause,request:Request):return request.app.state.memory_control.pause(body.paused)


@router.get('/episodes')
def episodes(request:Request):
    request.app.state.memory_control.require()
    with request.app.state.sessions() as s:
        store=EpisodeStore(s)
        return [store.get(id) for id in s.execute(text('SELECT id FROM episodes ORDER BY created_at DESC,id LIMIT 100')).scalars()]


@router.post('/episodes/{id}/correct')
def correct_episode(id:str,body:EpisodeCorrection,request:Request):
    request.app.state.memory_control.require()
    with request.app.state.sessions.begin() as s:
        old=EpisodeStore(s).get(id);m=s.get(Message,body.source_message_id)
        if m is None or m.role!='user' or m.id in old['source_message_ids'] or m.created_at<=old['ended_at']:
            raise AppError('invalid_sources','事件纠正需要较新的用户原始消息；原事件保留。',422)
        if len(m.content)>2000:
            raise AppError('invalid_input','该原文超过单条事件摘要的长度上限；请选择较短的纠正原文。',422)
        candidate=EpisodicCandidate(candidate_id='manual-'+uid(),title=old['title'],content=m.content,evidence=[dict(message_id=m.id,role='user',start=0,end=len(m.content),quote=m.content)],evidence_level='user_explicit',confidence=1.0,uncertainty='none',importance=old['importance'],effect='correct')
        source=SourceMessage(m.id,m.conversation_id,m.role,m.content,m.created_at,m.metadata_json)
        decision=EvidencePolicyV2().validate(candidate,[source])
        if decision.disposition=='reject':raise AppError('invalid_input','新的原文不满足事件记忆证据要求；原事件没有修改。',422)
        new_id=uid()
        s.execute(text('INSERT INTO episodes VALUES (:id,:cid,:title,:content,:state,:importance,:start,:end,:candidate,:previous,:now)'),dict(id=new_id,cid=m.conversation_id,title=candidate.title,content=m.content,state=decision.disposition,importance=candidate.importance,start=m.created_at,end=m.created_at,candidate=json.dumps(candidate.model_dump(),ensure_ascii=False),previous=id,now=utcnow()))
        s.execute(text('INSERT INTO episode_sources VALUES (:episode,:message)'),dict(episode=new_id,message=m.id))
        return EpisodeStore(s).get(new_id)


@router.post('/suppress/{kind}/{id}')
def suppress(kind:str,id:str,request:Request):
    request.app.state.memory_control.require()
    if kind not in ('semantic','episodic'):raise AppError('invalid_input','对象类型不合法。',422)
    with request.app.state.sessions.begin() as s:
        if kind=='semantic':
            manager=MemoryManager(s);memory=manager.serialize(manager.get_memory(id))
            if memory['status']!='archived':manager.update_memory(id,dict(status='archived',expected_version=memory['version']),commit=False)
            ids=memory['source_message_ids']
        else:ids=EpisodeStore(s).get(id)['source_message_ids']
        s.execute(text('INSERT OR IGNORE INTO memory_suppressions VALUES (:kind,:id,:now)'),dict(kind=kind,id=id,now=utcnow()))
        for source in ids:s.execute(text('INSERT OR IGNORE INTO memory_exclusions VALUES (:id,:now)'),dict(id=source,now=utcnow()))
    return dict(suppressed=True,physically_deleted=False,audit_and_backups_retained=True,source_reprocessing_blocked=True)
