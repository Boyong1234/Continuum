"""Transactional daily reservations, deliberately conservative after lost usage."""
import json
from datetime import datetime,timezone
from sqlalchemy import text
from app.models import uid
from app.core.config import AppError
from app.services.providers import GenerationMetadata
from app.services.memory.jobs import stamp
from app.services.memory.runtime.config import MemoryConfig,CURRENT_POLICY


class BudgetLedger:
    def __init__(self,sessions,*,clock=None):self.sessions,self.clock=sessions,clock or (lambda:datetime.now(timezone.utc))

    def summary(self,session=None):
        if session is None:
            with self.sessions() as s:return self.summary(s)
        day=self.clock().date().isoformat()
        rows=session.execute(text('SELECT * FROM memory_budget_reservations WHERE day=:day'),dict(day=day)).mappings().all()
        return dict(day_utc=day,requests=len(rows),api_tokens=sum(r['actual_tokens'] or 0 for r in rows),
                    budget_tokens=sum(r['actual_tokens'] if r['actual_tokens'] is not None else r['reserved_tokens'] for r in rows),
                    unknown_requests=sum(r['state']!='settled' for r in rows),
                    cost_upper_microusd=sum((r['actual_tokens'] if r['actual_tokens'] is not None else r['reserved_tokens'])*r['unit_microusd'] for r in rows))

    def reserve(self,session,lease,config,config_hash):
        # Must be called in the SAME write transaction as claim; rollback on
        # exhaustion undoes claim/attempt, so a midnight wait never burns retries.
        now=stamp(self.clock());state=session.execute(text('SELECT * FROM memory_runtime WHERE id=1')).mappings().one()
        if not state['enabled'] or state['paused'] or state['config_hash']!=config_hash:
            raise AppError('memory_paused','自动记忆已关闭、暂停或配置发生变化。',409)
        if config.provider!='mock' and state['authorized_hash']!=config_hash:
            raise AppError('cloud_authorization_required','当前提取配置尚未获得授权。',403)
        if config.provider!='mock' and not session.execute(text("SELECT 1 FROM memory_release_gate WHERE id=1 AND status='passed' AND policy_version=:policy"),dict(policy=CURRENT_POLICY)).first():raise AppError('quality_gate_required','当前策略被质量门禁阻断。',409)
        consumed=self.summary(session);tokens=config.input_bytes+config.output_tokens+256
        active=session.execute(text("SELECT count(*) FROM memory_budget_reservations b JOIN memory_extraction_attempts a ON a.id=b.attempt_id JOIN memory_extraction_jobs j ON j.id=a.job_id WHERE b.state='reserved' AND a.status IN ('leased','api_succeeded') AND j.status='leased' AND j.lease_expires_at>:now"),dict(now=now)).scalar_one()
        if active>=config.max_concurrent:raise AppError('memory_concurrency','后台并发额度已占用。',429)
        if consumed['requests']+1>config.daily_requests or consumed['budget_tokens']+tokens>config.daily_tokens or consumed['cost_upper_microusd']+tokens*config.unit_microusd>config.daily_microusd:
            raise AppError('memory_budget','今日提取预算已耗尽；任务保留，未发起新请求。',429)
        attempt=session.execute(text('SELECT id FROM memory_extraction_attempts WHERE lease_token=:token'),dict(token=lease['lease_token'])).scalar_one()
        id=uid()
        session.execute(text("INSERT INTO memory_budget_reservations VALUES (:id,:day,:job,:attempt,:hash,:provider,:tokens,NULL,:unit,'reserved',:usage,:now,NULL)"),dict(id=id,day=self.clock().date().isoformat(),job=lease['id'],attempt=attempt,hash=config_hash,provider=config.provider,tokens=tokens,unit=config.unit_microusd,usage=json.dumps(GenerationMetadata().usage),now=now))
        return id

    def settle(self,id,usage):
        metadata=GenerationMetadata()
        if usage and usage.get('source')=='api':metadata.read_usage(usage)
        actual=max(metadata.usage['total_tokens'],metadata.usage['prompt_tokens']+metadata.usage['completion_tokens']) if metadata.usage['source']=='api' else None
        with self.sessions.begin() as s:
            row=s.execute(text('SELECT * FROM memory_budget_reservations WHERE id=:id'),dict(id=id)).mappings().one()
            if row['state']!='reserved':return
            s.execute(text('UPDATE memory_budget_reservations SET state=:state,actual_tokens=:actual,usage_json=:usage,finished_at=:now WHERE id=:id'),dict(id=id,state='settled' if actual is not None else 'unknown',actual=actual,usage=json.dumps(metadata.usage),now=stamp(self.clock())))
            s.execute(text('UPDATE memory_extraction_attempts SET usage_json=:usage WHERE id=:id'),dict(id=row['attempt_id'],usage=json.dumps(metadata.usage)))
            if actual is not None and actual>row['reserved_tokens']:
                s.execute(text('UPDATE memory_runtime SET paused=1,updated_at=:now WHERE id=1'),dict(now=stamp(self.clock())))

    def recover_unknown(self):
        with self.sessions.begin() as s:
            s.execute(text("UPDATE memory_budget_reservations SET state='unknown',finished_at=:now WHERE state='reserved' AND attempt_id IN (SELECT a.id FROM memory_extraction_attempts a JOIN memory_extraction_jobs j ON j.id=a.job_id WHERE a.status NOT IN ('leased','api_succeeded') OR j.lease_expires_at<=:now)"),dict(now=stamp(self.clock())))
