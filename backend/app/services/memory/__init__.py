from sqlalchemy import select, delete, text

from app.core.config import AppError
from app.models import Memory, MemorySource, MemoryRevision, Message, utcnow


class MemoryManager:
    def __init__(self, session):
        self.session = session
        self.schema_version=session.execute(text('SELECT version FROM schema_info')).scalar_one()
        self.v4 = self.schema_version >= 4

    def get_memory(self, id):
        memory = self.session.get(Memory, id)
        if memory is None:
            raise AppError('not_found', '记忆不存在。', 404)
        return memory

    def serialize(self, memory):
        value = {**{key: getattr(memory, key) for key in ('id', 'memory_type', 'content', 'created_at', 'updated_at', 'confidence', 'status')}, 'source_message_ids': list(self.session.scalars(select(MemorySource.message_id).where(MemorySource.memory_id == memory.id).order_by(MemorySource.message_id)))}
        if self.v4:
            from app.services.memory.lifecycle import MemoryLifecycle
            state = MemoryLifecycle(self.session).get(memory.id)
            value.update(legacy_status=value['status'], status=state.pop('state'))
            state.pop('memory_id')
            value.update(state)
            value['sources'] = [{'message_id': id, 'source_role': self.session.get(Message, id).role} for id in value['source_message_ids']]
        return value

    def validate_sources(self, ids, memory_type):
        ids = sorted(set(ids))
        sources = list(self.session.scalars(select(Message).where(Message.id.in_(ids))))
        if not ids or len(sources) != len(ids):
            raise AppError('invalid_sources', '记忆必须关联至少一条存在的原始消息。', 422)
        if memory_type in ('user_statement', 'user_confirmed') and not any(s.role == 'user' for s in sources):
            raise AppError('invalid_sources', '用户陈述或确认必须包含用户原始消息作为来源。', 422)
        return ids

    def create_memory(self, values, *, commit=True):
        ids = self.validate_sources(values['source_message_ids'], values['memory_type'])
        memory = Memory(**{k: v for k, v in values.items() if k != 'source_message_ids'})
        self.session.add(memory)
        self.session.flush()
        self.session.add_all([MemorySource(memory_id=memory.id, message_id=id) for id in ids])
        self.session.flush()
        if self.v4:
            from app.services.memory.lifecycle import MemoryLifecycle
            MemoryLifecycle(self.session).initialize(memory)
        if commit:self.session.commit()
        return self.serialize(memory)

    def update_memory(self, id, values, *, commit=True):
        memory = self.get_memory(id)
        snapshot = self.serialize(memory)
        values = dict(values)
        if self.v4:
            from app.services.memory.lifecycle import MemoryLifecycle
            expected = values.pop('expected_version', None)
            if expected != snapshot['version']:
                raise AppError('version_conflict' if expected is not None else 'version_required', '请提供最新 expected_version 后修改记忆。', 409 if expected is not None else 422)
            if values.get('memory_type', memory.memory_type) != memory.memory_type:
                raise AppError('lifecycle_required', 'v4 不允许用旧 memory_type 修改证据等级，请使用明确确认接口。', 422)
            if 'status' in values:
                if set(values) != {'status'} or values['status'] != 'archived':
                    raise AppError('lifecycle_required', '生命周期变更请使用独立接口。', 422)
                return MemoryLifecycle(self.session).update(id, {'state':'archived'}, expected_version=expected, reason='archive', commit=commit)
            ids = self.validate_sources(values.get('source_message_ids', snapshot['source_message_ids']), memory.memory_type)
            if values.get('content', memory.content) != memory.content and not any(source not in snapshot['source_message_ids'] and self.session.get(Message, source).role == 'user' for source in ids):
                raise AppError('new_evidence_required', '修改事实内容需要新增用户原文来源。', 422)
            # Preserve all old sources; revisions retain their exact earlier state.
            ids = sorted(set(ids) | set(snapshot['source_message_ids']))
            with self.session.begin_nested():
                MemoryLifecycle(self.session).update(id, {}, expected_version=expected, reason='manual_update', commit=False)
                for key, value in values.items():
                    if key != 'source_message_ids':setattr(memory, key, value)
                existing = set(snapshot['source_message_ids'])
                self.session.add_all([MemorySource(memory_id=id,message_id=source) for source in ids if source not in existing])
                memory.updated_at = utcnow()
                self.session.flush()
            if commit:self.session.commit()
            return self.serialize(memory)
        values.pop('expected_version', None)
        ids = self.validate_sources(values.get('source_message_ids', snapshot['source_message_ids']), values.get('memory_type', memory.memory_type))
        self.session.add(MemoryRevision(memory_id=id, snapshot=snapshot))
        for key, value in values.items():
            if key != 'source_message_ids':
                setattr(memory, key, value)
        memory.updated_at = utcnow()
        self.session.execute(delete(MemorySource).where(MemorySource.memory_id == id))
        self.session.flush()
        self.session.add_all([MemorySource(memory_id=id, message_id=source) for source in ids])
        if commit:self.session.commit()
        return self.serialize(memory)

    def archive_memory(self, id):
        values = {'status': 'archived'}
        if self.v4:values['expected_version'] = self.serialize(self.get_memory(id))['version']
        return self.update_memory(id, values)

    def search_memories(self, query='', include_archived=False, limit=100, memory_types=None, *, query_terms=None):
        statement = select(Memory)
        if self.schema_version>=6 and not include_archived:
            statement=statement.where(text("NOT EXISTS(SELECT 1 FROM memory_suppressions b WHERE b.kind='semantic' AND b.target_id=memories.id) AND NOT EXISTS(SELECT 1 FROM memory_sources a JOIN memory_exclusions x ON x.message_id=a.message_id WHERE a.memory_id=memories.id)"))
        if self.v4:
            statement = statement.where(text('EXISTS (SELECT 1 FROM memory_lifecycle ml WHERE ml.memory_id=memories.id)'))
        if not include_archived:
            if self.v4:
                now = utcnow()
                statement = statement.where(text("EXISTS (SELECT 1 FROM memory_lifecycle ml WHERE ml.memory_id=memories.id AND ml.state='active' AND ml.evidence_level IN ('user_explicit','user_confirmed') AND (ml.valid_from IS NULL OR ml.valid_from<=:now) AND (ml.valid_to IS NULL OR ml.valid_to>:now))")).params(now=now)
            else:statement = statement.where(Memory.status == 'active')
        if memory_types is not None:
            if self.v4:
                allowed = [level for kind, level in [('user_statement','user_explicit'),('user_confirmed','user_confirmed'),('model_inference','model_inferred')] if kind in memory_types]
                from sqlalchemy import bindparam
                statement = statement.where(text('EXISTS (SELECT 1 FROM memory_lifecycle ml WHERE ml.memory_id=memories.id AND ml.evidence_level IN :levels)').bindparams(bindparam('levels', expanding=True))).params(levels=allowed)
            else:statement = statement.where(Memory.memory_type.in_(memory_types))
        if query_terms:
            from sqlalchemy import or_
            fts_query=' OR '.join('"'+term.replace('"','""')+'"' for term in query_terms)
            ids=list(self.session.execute(text('SELECT id FROM memory_fts WHERE memory_fts MATCH :q'),dict(q=fts_query)).scalars())
            patterns=[Memory.content.contains(term,autoescape=True) for term in query_terms]
            statement=statement.where(or_(Memory.id.in_(ids),*patterns))
        elif query.strip():
            # Literal phrase protects FTS query grammar; substring fallback handles Chinese.
            phrase = '"' + query.replace('"', '""') + '"'
            ids = list(self.session.execute(text('SELECT id FROM memory_fts WHERE memory_fts MATCH :q'), {'q': phrase}).scalars())
            escaped = query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
            statement = statement.where(Memory.id.in_(ids) | Memory.content.like(f'%{escaped}%', escape='\\'))
        return [self.serialize(m) for m in self.session.scalars(statement.order_by(Memory.updated_at.desc(), Memory.id).limit(limit))]

    def retrieve(self, query):
        # Compatibility helper; ConversationEngine uses the injectable protocol.
        return self.search_memories(query, limit=10, memory_types=('user_statement', 'user_confirmed')) if query.strip() else []
