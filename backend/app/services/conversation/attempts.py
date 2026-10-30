import json

from sqlalchemy import text

from app.models import uid, utcnow


def interrupt_attempts(session, request_id=None):
    """Retain trace/usage when recovering; never regenerate an ambiguous request."""
    session.execute(text('UPDATE generation_attempts SET updated_at=updated_at WHERE 0'))
    rows = session.execute(text("SELECT user_message_id,metadata FROM generation_attempts WHERE status='running' AND (:request IS NULL OR request_id=:request)"), {'request': request_id}).mappings().all()
    for row in rows:
        value = json.loads(row['metadata'])
        if 'turn_trace' in value:
            value['turn_trace']['status'] = 'interrupted'
        session.execute(text("UPDATE generation_attempts SET status='interrupted',updated_at=:now,metadata=:meta WHERE user_message_id=:id AND status='running'"), dict(now=utcnow(), meta=json.dumps(value), id=row['user_message_id']))


class AttemptStore:
    """Separate v3 ledger; legacy v1/v2 remain usable without implicit DDL."""
    def __init__(self, sessions, *, recover=True):
        self.sessions = sessions
        with sessions() as session:
            session.connection(execution_options={'sqlite_begin_immediate':True})
            self.enabled = session.execute(text('SELECT version FROM schema_info')).scalar_one() >= 3
            if self.enabled and recover:
                interrupt_attempts(session)
                session.commit()

    def recover(self):
        if self.enabled:
            with self.sessions.begin() as session:
                interrupt_attempts(session)

    def insert(self, session, conversation_id, user_id, request, config, trace=None):
        if not self.enabled:
            return
        now = utcnow()
        from app.services.dialogue import trace_seed
        trace = trace or trace_seed(request.content, config)
        session.execute(text('INSERT INTO generation_attempts (id,conversation_id,user_message_id,request_id,config_id,provider,requested_model,status,created_at,updated_at,metadata) VALUES (:id,:cid,:user,:request,:config,:provider,:model,\'running\',:now,:now,:metadata)'),
                        dict(id=uid(), cid=conversation_id, user=user_id, request=request.request_id, config=request.config_id, provider=config.provider if config else 'unconfigured', model=config.model if config else None, now=now, metadata=json.dumps({'usage': trace['usage'], 'turn_trace': trace})))

    def update_trace(self, session, user_id, trace):
        if self.enabled:
            session.execute(text('UPDATE generation_attempts SET updated_at=updated_at WHERE 0'))
            row = session.execute(text("SELECT metadata FROM generation_attempts WHERE user_message_id=:id AND status='running'"), {'id': user_id}).scalar_one_or_none()
            if row is not None:
                value = json.loads(row)
                value['turn_trace'] = {**value.get('turn_trace', {}), **trace}
                session.execute(text("UPDATE generation_attempts SET metadata=:meta WHERE user_message_id=:id AND status='running'"), dict(meta=json.dumps(value), id=user_id))

    def duplicate(self, session, request_id):
        return self.enabled and request_id is not None and session.execute(text('SELECT id FROM generation_attempts WHERE request_id=:id'), {'id': request_id}).first() is not None

    def finish(self, session, user_id, state, metadata, result_id=None, error_code=None, context=None):
        if self.enabled:
            # Merge requires a read: reserve the WAL writer BEFORE reading, as
            # cancellation/ingest may commit concurrently on another connection.
            session.execute(text('UPDATE generation_attempts SET updated_at=updated_at WHERE 0'))
            row = session.execute(text("SELECT metadata,provider,requested_model FROM generation_attempts WHERE user_message_id=:user AND status='running'"), {'user': user_id}).mappings().first()
            if row is None:
                return
            value = json.loads(row['metadata'])
            value.update(usage=metadata.usage, finish_reason=metadata.finish_reason, error_code=error_code)
            if context is not None:
                value['context'] = context
            if 'turn_trace' in value:
                value['turn_trace'].update(status=state, provider=row['provider'], model=metadata.model or row['requested_model'], usage=metadata.usage)
            session.execute(text('UPDATE generation_attempts SET status=:status,updated_at=:now,model=:model,result_message_id=:result,metadata=:metadata WHERE user_message_id=:user AND status=\'running\''),
                            dict(status=state, now=utcnow(), model=metadata.model, result=result_id, user=user_id, metadata=json.dumps(value)))

    def list(self, conversation_id):
        if not self.enabled:
            return []
        with self.sessions() as session:
            rows = session.execute(text('SELECT * FROM generation_attempts WHERE conversation_id=:id ORDER BY created_at,id'), {'id': conversation_id}).mappings()
            return [{**dict(row), 'metadata': json.loads(row['metadata'])} for row in rows]
