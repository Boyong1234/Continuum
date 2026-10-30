import json
import hashlib
from sqlalchemy import text
from app.core.config import AppError
from app.models import utcnow
from app.services.memory.runtime.config import MemoryConfig,strategy,encoded,CURRENT_POLICY


class MemoryControl:
    def __init__(self,sessions,providers):
        self.sessions,self.providers=sessions,providers
        with sessions() as s:self.supported=s.execute(text('SELECT version FROM schema_info')).scalar_one()>=6

    def require(self):
        if not self.supported:raise AppError('migration_required','自动记忆需要显式迁移的 v6 隔离数据集。',409)

    def read(self,session=None):
        self.require()
        if session is None:
            with self.sessions() as s:return self.read(s)
        return dict(session.execute(text('SELECT * FROM memory_runtime WHERE id=1')).mappings().one())

    def configure(self,config,*,enabled,authorize_cloud=False,include_history=False):
        self.require();config=MemoryConfig.model_validate(config)
        provider=self.providers.get(config.provider)
        digest,_=strategy(config,provider)
        with self.sessions.begin() as s:
            s.execute(text('UPDATE memory_runtime SET updated_at=updated_at WHERE id=1'))
            old=self.read(s)
            authorized=digest if authorize_cloud else old['authorized_hash']
            if enabled and provider.config.provider!='mock' and authorized!=digest:
                raise AppError('cloud_authorization_required','请一次明确授权当前提取模型、端点和预算配置；不会上传授权前历史。',403)
            if enabled and provider.config.provider!='mock' and not s.execute(text("SELECT 1 FROM memory_release_gate WHERE id=1 AND status='passed' AND policy_version=:policy"),dict(policy=CURRENT_POLICY)).first():
                raise AppError('quality_gate_required','当前策略尚未通过离线发布门禁，不能启用云提取。',409)
            start=old['start_rowid']
            if enabled and not old['initialized']:
                start=s.execute(text('SELECT COALESCE(max(rowid),0) FROM messages')).scalar_one()
                s.execute(text('UPDATE memory_schedule_cursor SET last_rowid=:n WHERE id=1'),dict(n=start))
            if include_history and (not old['initialized'] or start>0):
                start=0
                s.execute(text('UPDATE memory_schedule_cursor SET last_rowid=0 WHERE id=1'))
            s.execute(text('UPDATE memory_runtime SET enabled=:enabled,paused=:paused,initialized=:initialized,start_rowid=:start,config_json=:config,config_hash=:hash,authorized_hash=:auth,updated_at=:now WHERE id=1'),dict(enabled=int(enabled),paused=int(not enabled),initialized=int(bool(old['initialized'] or enabled)),start=start,config=encoded(config.model_dump()),hash=digest,auth=authorized,now=utcnow()))
        return self.status()

    def pause(self,paused):
        self.require()
        with self.sessions.begin() as s:
            state=self.read(s)
            config=MemoryConfig.model_validate(json.loads(state['config_json'])) if state['config_hash'] else MemoryConfig()
            if not paused and config.provider!='mock' and not s.execute(text("SELECT 1 FROM memory_release_gate WHERE id=1 AND status='passed' AND policy_version=:policy"),dict(policy=CURRENT_POLICY)).first():raise AppError('quality_gate_required','云提取质量门禁未通过。',409)
            s.execute(text('UPDATE memory_runtime SET paused=:p,updated_at=:now WHERE id=1'),dict(p=int(paused),now=utcnow()))
        return self.status()

    def certify(self,report):
        from app.services.memory.runtime.evaluation import FROZEN_SHA256
        valid=report.get('sha256')==FROZEN_SHA256 and report.get('cases')==60 and report.get('release_gate') is True and report['validator']['unsupported_active']['numerator']==0 and report['reconciler']['replay_added_memories']['numerator']==0
        with self.sessions.begin() as s:
            old=s.execute(text('SELECT status,policy_version FROM memory_release_gate WHERE id=1')).one()
            if old.status=='blocked' and old.policy_version==CURRENT_POLICY and valid:raise AppError('quality_gate_required','已发生严重质量阻断，须修订策略并重新隔离验收；同策略证书不能覆盖阻断。',409)
            s.execute(text('UPDATE memory_release_gate SET status=:status,report_sha256=:hash,policy_version=:policy,updated_at=:now WHERE id=1'),dict(status='passed' if valid else 'blocked',hash=hashlib.sha256(encoded(report).encode()).hexdigest(),policy=CURRENT_POLICY,now=utcnow()))
            if not valid:s.execute(text('UPDATE memory_runtime SET paused=1 WHERE id=1'))
        return valid

    def block_quality(self):
        with self.sessions.begin() as s:
            s.execute(text("UPDATE memory_release_gate SET status='blocked',updated_at=:now WHERE id=1"),dict(now=utcnow()))
            s.execute(text('UPDATE memory_runtime SET paused=1 WHERE id=1'))

    def status(self):
        if not self.supported:return dict(supported=False,enabled=False,paused=True,reason='migration_required',schema_required=6)
        from app.services.memory.runtime.budget import BudgetLedger
        with self.sessions() as s:
            state=self.read(s)
            config=MemoryConfig.model_validate(json.loads(state['config_json'])) if state['config_hash'] else MemoryConfig()
            counts=dict(s.execute(text('SELECT status,count(*) FROM memory_extraction_jobs GROUP BY status')).all())
            segments=dict(s.execute(text('SELECT state,count(*) FROM memory_segments GROUP BY state')).all())
            last=s.execute(text("SELECT max(updated_at) FROM memory_extraction_jobs WHERE status='succeeded'")).scalar_one()
            gate_row=s.execute(text('SELECT status,policy_version FROM memory_release_gate WHERE id=1')).one()
            gate='stale' if gate_row.status=='passed' and gate_row.policy_version!=CURRENT_POLICY else gate_row.status
        return dict(supported=True,enabled=bool(state['enabled']),paused=bool(state['paused']),quality_gate=gate,cloud_authorized=bool(state['authorized_hash'] and state['authorized_hash']==state['config_hash']),provider=config.provider,config=config.model_dump(),config_hash=state['config_hash'],jobs=counts,segments=segments,last_processed_at=last,today=BudgetLedger(self.sessions).summary(),deletion='suppression_only_audit_and_backups_retained')


def notify_saved(session,conversation_id):
    # Best-effort notification only; rowid scan is the durable source of truth.
    session.execute(text('INSERT INTO memory_conversation_dirty(conversation_id,updated_at) SELECT :id,:now FROM memory_runtime WHERE id=1 AND enabled=1 ON CONFLICT(conversation_id) DO UPDATE SET updated_at=excluded.updated_at'),dict(id=conversation_id,now=utcnow()))
