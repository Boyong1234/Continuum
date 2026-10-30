"""P2-A durable queue, deliberately not a background extractor or scheduler."""
import hashlib
import json
import re
from contextlib import nullcontext
from datetime import datetime, timezone, timedelta

from pydantic import BaseModel, ConfigDict, Field
from typing import Literal
from sqlalchemy import text, event

from app.core.config import AppError
from app.models import uid
from app.services.providers import GenerationMetadata

ERROR_CODES = frozenset(('timeout','network_error','authentication_error','rate_limit','service_unavailable','invalid_output','write_failure','lease_expired','internal_error','configuration_error'))


def canonical_result(value):
    class Envelope(BaseModel):
        model_config = ConfigDict(extra='forbid', strict=True)
        schema_version: Literal[1]
        payload: dict = Field(default_factory=dict)
    if not isinstance(value,dict) or type(value.get('schema_version')) is not int:
        raise ValueError('Invalid envelope version type')
    data = Envelope.model_validate(value).model_dump()
    encoded = json.dumps(data,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
    if len(encoded.encode()) > 200000:
        raise ValueError('Result envelope exceeds size limit')
    return encoded


def source_fingerprint(rows):
    # rows: [(id, conversation_id, role, content, created_at)], canonical order.
    return hashlib.sha256(json.dumps(rows,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def stamp(value):
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError('Clock must use UTC')
    return value.isoformat(timespec='microseconds').replace('+00:00','Z')


class MemoryExtractionQueue:
    def __init__(self,sessions,*,clock=None,lease_seconds=120,retry_seconds=5):
        if not 1 <= lease_seconds <= 900 or not 1 <= retry_seconds <= 300:
            raise ValueError('Invalid lease/retry policy')
        self.sessions,self.clock=sessions,clock or (lambda:datetime.now(timezone.utc))
        self.lease_seconds,self.retry_seconds=lease_seconds,retry_seconds
        with sessions() as session:
            if session.execute(text('SELECT version FROM schema_info')).scalar_one() < 4:
                raise AppError('migration_required','持久化记忆任务需要显式迁移的 v4 副本。',409)

    def _row(self,session,id):
        value=session.execute(text('SELECT * FROM memory_extraction_jobs WHERE id=:id'),{'id':id}).mappings().first()
        if value is None: raise AppError('not_found','记忆任务不存在。',404)
        result=dict(value)
        encoded=result.pop('result_json')
        result['result']=json.loads(encoded) if encoded is not None else None
        result['source_message_ids']=list(session.execute(text('SELECT message_id FROM memory_extraction_job_sources WHERE job_id=:id ORDER BY position'),{'id':id}).scalars())
        return result

    def get(self,id):
        with self.sessions() as session: return self._row(session,id)

    def enqueue(self,source_ids,extractor_version,*,config_id,max_attempts=3,session=None):
        if not 1 <= len(source_ids) <= 100 or len(set(source_ids)) != len(source_ids):
            raise AppError('invalid_sources','任务需要 1–100 个不重复的原始消息 ID。',422)
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}',extractor_version) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}',config_id) or type(max_attempts) is not int or not 1<=max_attempts<=5:
            raise AppError('invalid_input','提取版本、配置或重试上限无效。',422)
        # First statement reserves the writer slot; concurrent enqueues cannot both
        # observe no previous extractor version and bypass the alignment barrier.
        with (self.sessions.begin() if session is None else nullcontext(session)) as session:
            session.execute(text('UPDATE memory_extraction_jobs SET updated_at=updated_at WHERE 0'))
            rows=[]
            for id in source_ids:
                row=session.execute(text('SELECT id,conversation_id,role,content,created_at FROM messages WHERE id=:id'),{'id':id}).first()
                if row is None: raise AppError('invalid_sources','任务来源消息不存在。',422)
                rows.append(tuple(row))
            rows.sort(key=lambda row:(row[4],row[0]))
            if len({row[1] for row in rows})!=1 or not any(row[2]=='user' for row in rows):
                raise AppError('invalid_sources','任务来源须属于同一会话并包含用户消息。',422)
            fingerprint=source_fingerprint(rows)
            existing=session.execute(text('SELECT id FROM memory_extraction_jobs WHERE source_fingerprint=:fp AND extractor_version=:version'),{'fp':fingerprint,'version':extractor_version}).scalar_one_or_none()
            if existing: return self._row(session,existing)
            previous=session.execute(text('SELECT id FROM memory_extraction_jobs WHERE source_fingerprint=:fp LIMIT 1'),{'fp':fingerprint}).first()
            now=stamp(self.clock()); id=uid()
            session.execute(text('INSERT INTO memory_extraction_jobs(id,source_fingerprint,extractor_version,config_id,status,max_attempts,created_at,updated_at) VALUES (:id,:fp,:version,:config,:status,:max,:now,:now)'),dict(id=id,fp=fingerprint,version=extractor_version,config=config_id,status='alignment_required' if previous else 'queued',max=max_attempts,now=now))
            for position,row in enumerate(rows):
                session.execute(text('INSERT INTO memory_extraction_job_sources VALUES (:job,:message,:position)'),dict(job=id,message=row[0],position=position))
            return self._row(session,id)

    def _backoff(self,attempt):
        return min(300,self.retry_seconds*2**(attempt-1))

    def reclaim_expired(self):
        now=self.clock(); date=stamp(now)
        with self.sessions.begin() as session:
            # UPDATE first avoids upgrading a stale read snapshot in WAL mode.
            expired=session.execute(text("UPDATE memory_extraction_jobs SET status=CASE WHEN attempts>=max_attempts THEN 'dead' ELSE 'retry' END,last_error='lease_expired',updated_at=:now,lease_token=NULL,lease_owner=NULL,lease_expires_at=NULL WHERE status='leased' AND lease_expires_at<=:now RETURNING id,attempts,status"),{'now':date}).mappings().all()
            for row in expired:
                retry=stamp(now+timedelta(seconds=self._backoff(row['attempts']))) if row['status']=='retry' else None
                session.execute(text('UPDATE memory_extraction_jobs SET next_retry_at=:retry WHERE id=:id'),{'id':row['id'],'retry':retry})
                session.execute(text("UPDATE memory_extraction_attempts SET status='expired',error_code='lease_expired',finished_at=:now WHERE job_id=:id AND attempt_number=:attempt"),dict(id=row['id'],attempt=row['attempts'],now=date))
            return len(expired)

    def claim(self,owner,*,job_id=None,session=None):
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}',owner): raise ValueError('Invalid lease owner')
        if session is None:self.reclaim_expired()
        now=self.clock(); token=uid()
        with (self.sessions.begin() if session is None else nullcontext(session)) as session:
            row=session.execute(text("UPDATE memory_extraction_jobs SET status='leased',attempts=attempts+1,lease_token=:token,lease_owner=:owner,lease_expires_at=:expires,updated_at=:now,next_retry_at=NULL WHERE id=(SELECT id FROM memory_extraction_jobs WHERE status IN ('queued','retry') AND attempts<max_attempts AND (next_retry_at IS NULL OR next_retry_at<=:now) AND (:selected IS NULL OR id=:selected) ORDER BY created_at,id LIMIT 1) RETURNING id,attempts"),dict(token=token,owner=owner,expires=stamp(now+timedelta(seconds=self.lease_seconds)),now=stamp(now),selected=job_id)).mappings().first()
            if row is None:return None
            session.execute(text("INSERT INTO memory_extraction_attempts(id,job_id,attempt_number,lease_token,lease_owner,status,usage_json,started_at) VALUES (:id,:job,:attempt,:token,:owner,'leased',:usage,:now)"),dict(id=uid(),job=row['id'],attempt=row['attempts'],token=token,owner=owner,usage=json.dumps(GenerationMetadata().usage),now=stamp(now)))
            return self._row(session,row['id'])

    def _fence(self,session,lease):
        now=stamp(self.clock())
        result=session.execute(text("UPDATE memory_extraction_jobs SET updated_at=:now WHERE id=:id AND status='leased' AND lease_token=:token AND lease_expires_at>:now RETURNING id"),dict(id=lease['id'],token=lease['lease_token'],now=now)).first()
        if result is None: raise AppError('lease_lost','任务租约已失效或任务已经结束。',409)
        return self._row(session,lease['id'])

    def renew(self,lease):
        with self.sessions.begin() as session:
            self._fence(session,lease)
            session.execute(text('UPDATE memory_extraction_jobs SET lease_expires_at=:date WHERE id=:id'),dict(id=lease['id'],date=stamp(self.clock()+timedelta(seconds=self.lease_seconds))))
            return self._row(session,lease['id'])

    def checkpoint(self,lease,result,*,provider=None,model=None,usage=None):
        try:
            encoded=canonical_result(result)
            metadata=GenerationMetadata()
            if usage and usage.get('source')=='api': metadata.read_usage(usage)
            elif usage and usage.get('source')!='unknown': raise ValueError('Unverified usage')
            if any(value is not None and (not isinstance(value,str) or len(value)>200) for value in (provider,model)): raise ValueError('Invalid model identity')
        except (ValueError,TypeError,AttributeError):
            raise AppError('invalid_output','提取结果暂存格式或用量元数据无效。',422) from None
        digest=hashlib.sha256(encoded.encode()).hexdigest()
        with self.sessions.begin() as session:
            job=self._fence(session,lease)
            if job['result'] is not None:
                if job['result_sha256']!=digest: raise AppError('result_conflict','已暂存的结果不能被不同内容覆盖。',409)
                return job
            attempt=session.execute(text('SELECT id FROM memory_extraction_attempts WHERE lease_token=:token'),{'token':lease['lease_token']}).scalar_one()
            session.execute(text('UPDATE memory_extraction_jobs SET result_json=:result,result_sha256=:digest,result_attempt_id=:attempt WHERE id=:id'),dict(id=lease['id'],result=encoded,digest=digest,attempt=attempt))
            session.execute(text("UPDATE memory_extraction_attempts SET status='api_succeeded',provider=:provider,model=:model,usage_json=:usage WHERE id=:id"),dict(id=attempt,provider=provider,model=model,usage=json.dumps(metadata.usage)))
            return self._row(session,lease['id'])

    def fail(self,lease,code):
        if code not in ERROR_CODES: raise ValueError('Use a sanitized error code, never an exception body')
        with self.sessions.begin() as session:
            job=self._fence(session,lease)
            dead=job['attempts']>=job['max_attempts'] or code in ('authentication_error','configuration_error')
            retry=None if dead else stamp(self.clock()+timedelta(seconds=self._backoff(job['attempts'])))
            now=stamp(self.clock())
            session.execute(text('UPDATE memory_extraction_jobs SET status=:status,last_error=:code,next_retry_at=:retry,lease_token=NULL,lease_owner=NULL,lease_expires_at=NULL,updated_at=:now WHERE id=:id'),dict(id=lease['id'],status='dead' if dead else 'retry',code=code,retry=retry,now=now))
            session.execute(text("UPDATE memory_extraction_attempts SET status='failed',error_code=:code,finished_at=:now WHERE lease_token=:token"),dict(code=code,now=now,token=lease['lease_token']))
            return self._row(session,lease['id'])

    def complete(self,lease,apply_result):
        try:
            with self.sessions.begin() as session:
                job=self._fence(session,lease)
                if job['result'] is None: raise AppError('result_required','任务必须先暂存结果再应用。',409)
                def prohibit_commit(session):
                    if not session.in_nested_transaction(): raise RuntimeError('Queue owns the transaction')
                event.listen(session,'before_commit',prohibit_commit)
                try: apply_result(session,job['result'])
                finally: event.remove(session,'before_commit',prohibit_commit)
                # Renewals cannot interleave with this write transaction. Check time
                # once more so an overlong callback never commits after its lease.
                self._fence(session,lease)
                now=stamp(self.clock())
                session.execute(text("UPDATE memory_extraction_jobs SET status='succeeded',last_error=NULL,lease_token=NULL,lease_owner=NULL,lease_expires_at=NULL,updated_at=:now WHERE id=:id"),dict(id=lease['id'],now=now))
                session.execute(text("UPDATE memory_extraction_attempts SET status='succeeded',finished_at=:now WHERE lease_token=:token"),dict(token=lease['lease_token'],now=now))
                return self._row(session,lease['id'])
        except AppError as exc:
            if exc.code in ('lease_lost','result_required'):
                raise
            try:
                self.fail(lease,'invalid_output')
                message='结果应用被校验拒绝，业务事务已回滚；暂存结果保留，按有限重试处理。'
            except Exception:
                message='结果应用被拒绝且重试状态未确认；恢复数据库后按租约到期回收。'
            raise AppError('invalid_output',message,422) from None
        except Exception:
            try:
                self.fail(lease,'write_failure')
                message='记忆结果应用失败，事务已回滚；暂存结果保留，按有限重试处理。'
            except Exception:
                message='记忆结果应用失败且重试状态未确认；已提交的暂存结果保留，恢复数据库后按租约到期回收。'
            raise AppError('write_failure',message,503) from None
