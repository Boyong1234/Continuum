import json
from contextlib import aclosing
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Query
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import select

from app.core.config import AppError
from app.models import Conversation, Memory, MemoryRevision
from app.schemas import ConversationCreate, ChatRequest, MemoryCreate, MemoryUpdate, conversation_dict, message_dict
from app.services.conversation import require_conversation, history
from app.services.memory import MemoryManager
from app.schemas import MemoryLifecycleUpdate


def local_identity():
    """Authentication seam. Only safe for a single user on loopback in v0.1."""
    return 'local-user'


router = APIRouter(prefix='/api', dependencies=[Depends(local_identity)])


@router.get('/health')
def health(request: Request):
    return {'status': 'ok', 'schema_version': request.app.state.database_schema_version}


@router.get('/configs')
def configs(request: Request):
    return request.app.state.providers.public_configs()


@router.get('/persona')
def persona(request: Request):
    return request.app.state.persona.read()


@router.post('/conversations', status_code=201)
def create_conversation(body: ConversationCreate, request: Request):
    with request.app.state.sessions() as session:
        conversation = Conversation(title=body.title)
        session.add(conversation)
        session.commit()
        return conversation_dict(conversation)


@router.get('/conversations')
def list_conversations(request: Request):
    with request.app.state.sessions() as session:
        return [conversation_dict(c) for c in session.scalars(select(Conversation).order_by(Conversation.updated_at.desc(), Conversation.id))]


@router.get('/conversations/{id}/messages')
def messages(id: str, request: Request):
    with request.app.state.sessions() as session:
        require_conversation(session, id)
        return [message_dict(m) for m in history(session, id)]


@router.post('/conversations/{id}/chat')
async def chat(id: str, body: ChatRequest, request: Request):
    engine = request.app.state.conversation_engine
    user = engine.prepare(id, body)

    async def events():
        async with aclosing(engine.stream(id, body, user)) as stream:
            async for kind, data in stream:
                yield f'event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n'

    return StreamingResponse(events(), media_type='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@router.get('/conversations/{id}/attempts')
def attempts(id: str, request: Request):
    with request.app.state.sessions() as session:
        require_conversation(session, id)
    return request.app.state.conversation_engine.attempts.list(id)


@router.post('/conversations/{id}/cancel')
async def cancel(id: str, request: Request):
    if request.app.state.onebot.store.owned(id):
        raise AppError('channel_owned','QQ轮次请通过受鉴权的通道管理或QQ /stop停止。',409)
    with request.app.state.sessions() as session:
        require_conversation(session, id)
    return {'cancellation_requested': request.app.state.conversation_engine.cancel(id)}


@router.post('/memories', status_code=201)
def create_memory(body: MemoryCreate, request: Request):
    with request.app.state.sessions() as session:
        return MemoryManager(session).create_memory(body.model_dump())


@router.get('/memories')
def search_memories(request: Request, q: Annotated[str, Query(max_length=500)] = '', include_archived: bool = False):
    with request.app.state.sessions() as session:
        return MemoryManager(session).search_memories(q, include_archived)


@router.get('/memories/{id}')
def get_memory(id: str, request: Request):
    with request.app.state.sessions() as session:
        manager = MemoryManager(session)
        return manager.serialize(manager.get_memory(id))


@router.patch('/memories/{id}')
def update_memory(id: str, body: MemoryUpdate, request: Request):
    # Explicit null is rejected: it must not silently clear required source fields.
    values = body.model_dump(exclude_unset=True)
    if any(value is None for value in values.values()):
        raise AppError('invalid_input', '记忆字段不能为 null。', 422)
    with request.app.state.sessions() as session:
        return MemoryManager(session).update_memory(id, values)


@router.post('/memories/{id}/archive')
def archive_memory(id: str, request: Request):
    with request.app.state.sessions() as session:
        return MemoryManager(session).archive_memory(id)


@router.patch('/memories/{id}/lifecycle')
def update_lifecycle(id: str, body: MemoryLifecycleUpdate, request: Request):
    if request.app.state.database_schema_version < 4:
        raise AppError('migration_required', '记忆生命周期需要显式迁移的 v4 副本。', 409)
    from app.services.memory.lifecycle import MemoryLifecycle
    values = body.model_dump(exclude_unset=True)
    expected = values.pop('expected_version')
    reason = values.pop('reason')
    confirmation = values.pop('confirmation_message_id', None)
    with request.app.state.sessions() as session:
        return MemoryLifecycle(session).update(id, values, expected_version=expected, reason=reason, confirmation_message_id=confirmation)


def revision_dict(session, revision, v4=False):
    result = {'id':revision.id, 'created_at':revision.created_at, 'snapshot':revision.snapshot}
    if v4:
        from sqlalchemy import text
        row = session.execute(text('SELECT revision_number,change_reason FROM memory_revision_audit WHERE revision_id=:id'), {'id':revision.id}).mappings().one()
        result.update(row)
    return result


@router.get('/memories/{id}/revisions')
def revisions(id: str, request: Request):
    with request.app.state.sessions() as session:
        MemoryManager(session).get_memory(id)
        return [revision_dict(session, r, request.app.state.database_schema_version >= 4) for r in session.scalars(select(MemoryRevision).where(MemoryRevision.memory_id == id).order_by(MemoryRevision.created_at, MemoryRevision.id))]


@router.get('/exports/conversations.jsonl')
def export_all(request: Request):
    with request.app.state.sessions() as session:
        rows = [{'schema_version': 1, 'conversation': conversation_dict(c), 'messages': [message_dict(m) for m in history(session, c.id)]} for c in session.scalars(select(Conversation).order_by(Conversation.created_at, Conversation.id))]
    data = ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows)
    return Response(data, media_type='application/x-ndjson', headers={'Content-Disposition': 'attachment; filename="continuum-conversations.jsonl"'})


@router.get('/exports/conversations/{id}.md')
def export_conversation(id: str, request: Request):
    with request.app.state.sessions() as session:
        conversation = require_conversation(session, id)
        lines = [f'# {conversation.title}', '', 'schema_version: 1', f'conversation_id: {id}', f'created_at: {conversation.created_at}', '']
        for m in history(session, id):
            lines.extend([f'## {m.role} · {m.created_at}', f'<!-- message_id: {m.id}; model: {m.model or ""} -->', '', m.content, ''])
    return Response('\n'.join(lines), media_type='text/markdown', headers={'Content-Disposition': f'attachment; filename="conversation-{id}.md"'})


@router.get('/exports/memories.json')
def export_memories(request: Request):
    with request.app.state.sessions() as session:
        manager = MemoryManager(session)
        rows = [manager.serialize(m) for m in session.scalars(select(Memory).order_by(Memory.created_at))]
        revision_rows = [{'id':r.id, 'memory_id':r.memory_id, **revision_dict(session, r, request.app.state.database_schema_version >= 4)} for r in session.scalars(select(MemoryRevision).order_by(MemoryRevision.created_at))]
    return Response(json.dumps({'schema_version': 2 if request.app.state.database_schema_version >= 4 else 1, 'memories': rows, 'revisions': revision_rows}, ensure_ascii=False, indent=2), media_type='application/json', headers={'Content-Disposition': 'attachment; filename="continuum-memories.json"'})
