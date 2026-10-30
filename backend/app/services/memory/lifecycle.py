"""Explicit lifecycle operations; no extraction or semantic conflict inference."""
from datetime import datetime, timezone, timedelta

from pydantic import BaseModel, ConfigDict, Field
from typing import Literal
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.core.config import AppError
from app.models import Memory, MemorySource, MemoryRevision, Message, uid, utcnow
from app.maintenance.revisions.v0004_memory_foundation import mapped_state


class LifecycleValues(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    state: Literal['tentative','active','superseded','archived'] | None=None
    evidence_level: Literal['user_explicit','user_confirmed','model_inferred'] | None=None
    topic_key: str | None=Field(default=None,min_length=1,max_length=200)
    valid_from: str | None=None
    valid_to: str | None=None
    superseded_by: str | None=None


def normalized_time(value):
    if value is None:return None
    try:
        result=datetime.fromisoformat(value.replace('Z','+00:00'))
        if result.utcoffset()!=timedelta(0):raise ValueError()
        return result.isoformat(timespec='microseconds').replace('+00:00','Z')
    except (ValueError,AttributeError):
        raise AppError('invalid_time','记忆有效时间必须使用明确的 UTC 时间。',422) from None


class MemoryLifecycle:
    def __init__(self,session):self.session=session

    def get(self,id):
        row=self.session.execute(text('SELECT * FROM memory_lifecycle WHERE memory_id=:id'),{'id':id}).mappings().first()
        if row is None:raise AppError('not_found','记忆生命周期不存在或数据库尚未迁移。',404)
        return dict(row)

    def _audit(self,memory,version,snapshot,reason):
        revision=MemoryRevision(id=uid(),memory_id=memory.id,snapshot=snapshot)
        self.session.add(revision); self.session.flush()
        self.session.execute(text('INSERT INTO memory_revision_audit VALUES (:rid,:mid,:version,:reason)'),dict(rid=revision.id,mid=memory.id,version=version,reason=reason))

    def initialize(self,memory):
        state,evidence=mapped_state(memory.memory_type,memory.status)
        self.session.execute(text('INSERT INTO memory_lifecycle(memory_id,state,evidence_level,version) VALUES (:id,:state,:evidence,1)'),dict(id=memory.id,state=state,evidence=evidence))
        from app.services.memory import MemoryManager
        self._audit(memory,1,MemoryManager(self.session).serialize(memory),'created')

    def update(self,id,values,*,expected_version,reason,confirmation_message_id=None,commit=True):
        from app.services.memory import MemoryManager
        if type(expected_version) is not int or expected_version<1:
            raise AppError('version_required','生命周期修改须提供有效 expected_version。',422)
        if reason not in ('confirm','archive','reopen','conflict','retract','supersede','manual_update','time_update','topic_update'):
            raise AppError('invalid_input','请使用明确的生命周期变更原因代码。',422)
        try:
            changes=LifecycleValues.model_validate(values).model_dump(exclude_unset=True)
        except ValueError:
            raise AppError('invalid_input','生命周期字段不合法。',422) from None
        if any(changes.get(k) is None for k in ('state','evidence_level') if k in changes):
            raise AppError('invalid_input','生命周期和证据等级不能清空。',422)
        try:
            with self.session.begin_nested():
                matched=self.session.execute(text('UPDATE memory_lifecycle SET version=version+1 WHERE memory_id=:id AND version=:version RETURNING memory_id'),dict(id=id,version=expected_version)).first()
                if matched is None:raise AppError('version_conflict','记忆已变化或不存在，请重新读取后再修改。',409)
                previous=self.get(id); previous['version']=expected_version
                manager=MemoryManager(self.session); memory=manager.get_memory(id)
                snapshot=manager.serialize(memory); snapshot['version']=expected_version
                state={**previous,**changes}
                old,new=previous['state'],state['state']
                allowed={'tentative':{'tentative','active','archived'},'active':{'active','tentative','superseded','archived'},'superseded':{'superseded','archived'},'archived':{'archived','tentative','active'}}
                if new not in allowed[old]:raise AppError('invalid_transition','不允许此生命周期转换。',422)
                if previous['superseded_by'] and new in ('active','tentative'):
                    raise AppError('invalid_transition','已被替代的记录保留历史关系；恢复观点请建立有新证据的记录。',422)
                if confirmation_message_id:
                    source=self.session.get(Message,confirmation_message_id)
                    if reason!='confirm' or source is None or source.role!='user' or source.id in snapshot['source_message_ids']:
                        raise AppError('confirmation_required','确认须关联一条新增的用户原始消息。',422)
                    self.session.add(MemorySource(memory_id=id,message_id=source.id)); self.session.flush()
                    state['evidence_level']='user_confirmed'
                elif (new=='active' and old!='active') or state['evidence_level']!=previous['evidence_level'] and state['evidence_level']!='model_inferred':
                    raise AppError('confirmation_required','事实确认不能只依赖置信度，需要新增用户确认来源。',422)
                if new=='active' and state['evidence_level']=='model_inferred':
                    raise AppError('confirmation_required','模型推测不得作为已确认事实。',422)
                if state['superseded_by'] != previous['superseded_by']:
                    if old!='active' or new!='superseded' or reason!='supersede' or not state['superseded_by'] or state['superseded_by']==id:
                        raise AppError('invalid_transition','替代关系只能通过明确的 supersede 操作建立，不能静默改写。',422)
                    replacement=self.get(state['superseded_by'])
                    now=utcnow()
                    if replacement['state']!='active' or not state['topic_key'] or replacement['topic_key']!=state['topic_key'] or replacement['valid_from'] and replacement['valid_from']>now or replacement['valid_to'] and replacement['valid_to']<=now:
                        raise AppError('invalid_replacement','替代目标须为同一非空 topic_key 下当前有效的事实。',422)
                    # Active targets cannot already be superseded; this also prevents cycles.
                    if replacement['superseded_by']:raise AppError('invalid_replacement','替代目标已有历史替代关系。',422)
                for field in ('valid_from','valid_to'):state[field]=normalized_time(state[field])
                if state['valid_from'] and state['valid_to'] and state['valid_to']<=state['valid_from']:
                    raise AppError('invalid_time','valid_to 必须晚于 valid_from。',422)
                if state['topic_key'] is not None and not state['topic_key'].strip():
                    raise AppError('invalid_input','topic_key 不能全部为空白。',422)
                if state['topic_key'] != previous['topic_key'] and self.session.execute(text('SELECT memory_id FROM memory_lifecycle WHERE superseded_by=:id LIMIT 1'), {'id':id}).first():
                    raise AppError('invalid_transition','被替代链引用的事实键不能改写，请建立新的有来源记录。',422)
                self._audit(memory,expected_version+1,snapshot,reason)
                self.session.execute(text('UPDATE memory_lifecycle SET state=:state,evidence_level=:evidence_level,topic_key=:topic_key,valid_from=:valid_from,valid_to=:valid_to,superseded_by=:superseded_by WHERE memory_id=:memory_id'),state)
                memory.updated_at=utcnow(); self.session.flush()
            if commit:self.session.commit()
            return MemoryManager(self.session).serialize(memory)
        except OperationalError as exc:
            if getattr(exc.orig,'sqlite_errorcode',None) in (5,6,517):
                raise AppError('version_conflict','记忆正在被其他事务修改，请重新读取后重试。',409) from None
            raise
