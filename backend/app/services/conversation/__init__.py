import asyncio
from contextlib import aclosing
from datetime import datetime,timedelta

from sqlalchemy import select,text
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import AppError
from app.models import Conversation, Message, utcnow
from app.schemas import message_dict
from app.services.memory.retrieval import MemoryRetriever, RetrievalRequest, LocalHistoryRetriever
from app.services.providers import GenerationMetadata
from app.services.dialogue import Recall, plan_turn
from app.services.conversation.attempts import AttemptStore


def require_conversation(session, id):
    item = session.get(Conversation, id)
    if item is None:
        raise AppError('not_found', '会话不存在。', 404)
    return item


def history(session, id):
    version = session.execute(text('SELECT version FROM schema_info')).scalar_one()
    if version >= 7 and session.execute(text('SELECT id FROM channel_bindings WHERE conversation_id=:id'), {'id':id}).first():
        return list(session.scalars(select(Message).where(Message.conversation_id == id).order_by(text('rowid'))))
    return list(session.scalars(select(Message).where(Message.conversation_id == id).order_by(Message.created_at, Message.id)))


def next_created_at(session,conversation_id):
    """Preserve order of NEW messages when the local wall clock has a coarse tick.

    Never updates existing/imported timestamps; large clock discontinuities are
    not hidden by assigning a message an arbitrary future date.
    """
    now=utcnow()
    previous=session.scalar(select(Message.created_at).where(Message.conversation_id==conversation_id).order_by(text('rowid DESC')).limit(1))
    if previous:
        last=datetime.fromisoformat(previous.replace('Z','+00:00'));current=datetime.fromisoformat(now.replace('Z','+00:00'))
        delta=(last-current).total_seconds()
        if 0<=delta<=.1:return (last+timedelta(microseconds=1)).isoformat(timespec='microseconds').replace('+00:00','Z')
    return now


class ConversationEngine:
    def __init__(self, sessions, providers, context, retriever: MemoryRetriever | None = None,*,memory_notifier=None,recover_attempts=True,channel_guard=None):
        self.sessions, self.providers, self.context = sessions, providers, context
        self.retriever = retriever if retriever is not None else LocalHistoryRetriever(sessions)
        self.busy: set[str] = set()
        self.attempts = AttemptStore(sessions,recover=recover_attempts)
        self.cancellations: dict[str, asyncio.Event] = {}
        self.memory_notifier=memory_notifier
        self.channel_guard=channel_guard

    def append_user(self, session, conversation_id, content, config_id, *, created_at=None, metadata=None):
        """One canonical append path, caller owns the short database transaction."""
        if not content.strip():
            raise AppError('invalid_input','消息不能全部为空白。',422)
        conversation=require_conversation(session,conversation_id)
        provider=self.providers.providers.get(config_id)
        user=Message(conversation_id=conversation_id,role='user',content=content,
                     created_at=created_at or next_created_at(session,conversation_id),
                     model=provider.config.model if provider else None,
                     metadata_json={'config_id':config_id,'provider':provider.config.provider if provider else 'unconfigured','ingested_at':utcnow(),**(metadata or {})})
        session.add(user)
        session.flush()
        conversation.updated_at=utcnow()
        if conversation.title=='新会话':conversation.title=content.strip()[:40]
        self.notify(session,conversation_id)
        return user

    def prepare_existing(self, conversation_id, request, source_ids, *, validate):
        """Generate from already persisted channel originals, never save a merged user."""
        if conversation_id in self.busy:
            raise AppError('conversation_busy','此会话正在生成。',409)
        self.busy.add(conversation_id)
        try:
            with self.sessions() as session:
                session.connection(execution_options={'sqlite_begin_immediate':True})
                validate(session)
                users=[session.get(Message,id) for id in source_ids]
                if not users or any(m is None or m.role!='user' or m.conversation_id!=conversation_id for m in users):
                    raise AppError('invalid_sources','通道来源无效。',409)
                if self.attempts.duplicate(session,request.request_id):
                    raise AppError('duplicate_request','该轮已经开始，请核对生成状态。',409)
                provider=self.providers.get(request.config_id)
                self.attempts.insert(session,conversation_id,users[-1].id,request,provider.config,self.context.initial_trace(request.content,provider.config))
                result=message_dict(users[-1])
                session.commit()
            self.cancellations[conversation_id]=asyncio.Event()
            return result
        except Exception:
            self.busy.discard(conversation_id)
            raise

    def notify(self,session,conversation_id):
        if self.memory_notifier:
            try:
                with session.begin_nested():self.memory_notifier(session,conversation_id)
            except Exception:
                pass  # The independent arrival scan recovers a missed hint.

    def cancel(self, conversation_id):
        event = self.cancellations.get(conversation_id)
        if event:
            event.set()
        return bool(event)

    def record_failure(self, user_id, state, metadata, code, context_info):
        try:
            with self.sessions() as session:
                self.attempts.finish(session, user_id, state, metadata, error_code=code, context=context_info)
                session.commit()
            return True
        except SQLAlchemyError:
            # The caller reports the failed acknowledgement explicitly. Original
            # messages are never rewritten to compensate for a broken ledger.
            return False

    def prepare(self, conversation_id, request):
        if self.channel_guard and self.channel_guard(conversation_id):
            raise AppError('channel_owned','QQ会话仅可查看；请从QQ发送，或新建Web会话。',409)
        if conversation_id in self.busy:
            raise AppError('conversation_busy', '此会话正在生成回复，请等待完成。', 409)
        if not request.content.strip():
            raise AppError('invalid_input', '消息不能全部为空白。', 422)
        self.busy.add(conversation_id)
        try:
            with self.sessions() as session:
                session.connection(execution_options={'sqlite_begin_immediate':True})
                conversation = require_conversation(session, conversation_id)
                if self.attempts.duplicate(session, request.request_id):
                    raise AppError('duplicate_request', '此请求已接收，请读取历史和生成状态；不要重复发送。', 409)
                provider = self.providers.providers.get(request.config_id)
                user = self.append_user(session,conversation_id,request.content,request.config_id)
                config=provider.config if provider else None
                self.attempts.insert(session, conversation_id, user.id, request, config, self.context.initial_trace(request.content,config))
                conversation.updated_at = utcnow()
                if conversation.title == '新会话':
                    conversation.title = request.content.strip()[:40]
                session.commit()
                self.cancellations[conversation_id] = asyncio.Event()
                return message_dict(user)
        except Exception:
            self.busy.discard(conversation_id)
            raise

    async def stream(self, conversation_id, request, user, *, context_factory=None,on_reply=None,reply_metadata=None):
        metadata = GenerationMetadata()
        terminal = False
        context_info = None
        error_code = None
        try:
            yield ('user_saved', user)
            if self.cancellations[conversation_id].is_set():
                raise AppError('cancelled', '生成已停止，用户消息已保留。', 409)
            provider = self.providers.get(request.config_id)
            if provider.config.provider == 'deepseek' and not self.attempts.enabled:
                raise AppError('migration_required', 'DeepSeek P1 需要 v3 数据副本，请先执行备份与显式副本迁移。', 409)
            turn = plan_turn(request.content)
            query = request.content if turn.recall == Recall.LOCAL else ''
            if self.context.natural_recall:
                with self.sessions() as session:
                    recent_plan = context_factory(session,[],provider) if context_factory else self.context.from_database(session, conversation_id, user['id'], [], provider)
                query = self.context.recall_query(recent_plan.messages)
            memories = [hit.memory for hit in self.retriever.retrieve(RetrievalRequest(text=query, chat_context=bool(self.context.chat_history_provider),exclude_content=request.content,before=user.get("created_at")))] if query else []
            with self.sessions() as session:
                if not query:memories=self.context.communication_preferences(session,request.content)
                plan = context_factory(session,memories,provider) if context_factory else self.context.from_database(session, conversation_id, user['id'], memories, provider)
                messages = plan.messages
            with self.sessions.begin() as session:
                self.attempts.update_trace(session,user['id'],plan.turn_trace)
            context_info = {'source': 'local_estimate', 'estimated_prompt_tokens': plan.estimated_tokens, 'history_omitted': plan.history_omitted, 'memory_omitted': plan.memory_omitted}
            if plan.history_omitted or plan.memory_omitted:
                yield ('context', context_info)
            chunks = []
            async with aclosing(provider.stream_response(messages, metadata)) as stream:
                cancellation = asyncio.create_task(self.cancellations[conversation_id].wait())
                pending = None
                try:
                    while True:
                        pending = asyncio.create_task(anext(stream))
                        ready, _ = await asyncio.wait((pending, cancellation), return_when=asyncio.FIRST_COMPLETED)
                        if cancellation in ready:
                            pending.cancel()
                            await asyncio.gather(pending, return_exceptions=True)
                            raise AppError('cancelled', '生成已停止，临时回复未保存；用户消息已保留。', 409)
                        try:
                            delta = pending.result()
                        except StopAsyncIteration:
                            break
                        chunks.append(delta)
                        yield ('delta', {'content': delta})
                finally:
                    if pending and not pending.done():
                        pending.cancel()
                        await asyncio.gather(pending, return_exceptions=True)
                    cancellation.cancel()
                    await asyncio.gather(cancellation, return_exceptions=True)
            content = ''.join(chunks)
            if not content.strip():
                raise AppError('empty_response', '模型未返回文本，用户消息已保留。', 502)
            with self.sessions() as session:
                session.connection(execution_options={'sqlite_begin_immediate':True})
                conversation = require_conversation(session, conversation_id)
                reply = Message(conversation_id=conversation_id, role='assistant', content=content,created_at=next_created_at(session,conversation_id), model=metadata.model or provider.config.model, metadata_json={'config_id': request.config_id, 'provider': provider.config.provider, 'reply_to': user['id'], 'status': 'completed', 'usage': metadata.usage, 'context': context_info, 'ingested_at':utcnow()})
                if reply_metadata:
                    reply.metadata_json={**reply.metadata_json,**reply_metadata}
                session.add(reply)
                session.flush()
                self.attempts.finish(session, user['id'], 'completed', metadata, reply.id, context=context_info)
                conversation.updated_at = utcnow()
                self.notify(session,conversation_id)
                if on_reply:on_reply(session,reply)
                session.commit()
                terminal = True
                result = message_dict(reply)
            yield ('done', result)
        except AppError as exc:
            error_code = exc.code
            state = 'cancelled' if exc.code == 'cancelled' else 'truncated' if exc.code == 'incomplete_response' and metadata.finish_reason not in (None, 'stop') else 'interrupted' if exc.code in ('network_error', 'incomplete_response') else 'failed'
            recorded = self.record_failure(user['id'], state, metadata, exc.code, context_info)
            terminal = True
            yield ('error', {'code': exc.code if recorded else 'database_error', 'message': exc.message if recorded else '数据库写入失败，生成状态未确认；临时回复未保存，请恢复连接后核对历史。', 'user_message_id': user['id'], 'reply_persisted': False, 'attempt_status_persisted': self.attempts.enabled and recorded})
        except SQLAlchemyError:
            recorded = self.record_failure(user['id'], 'failed', metadata, 'database_error', context_info)
            terminal = True
            yield ('error', {'code': 'database_error', 'message': '数据库写入失败，临时回复未保存；用户消息已保留。', 'user_message_id': user['id'], 'reply_persisted': False, 'attempt_status_persisted': self.attempts.enabled and recorded})
        except asyncio.CancelledError:
            raise
        except Exception:
            recorded = self.record_failure(user['id'], 'failed', metadata, 'internal_error', context_info)
            terminal = True
            yield ('error', {'code': 'internal_error', 'message': '生成失败，临时回复未保存；用户消息已保留。请检查服务配置。', 'user_message_id': user['id'], 'reply_persisted': False, 'attempt_status_persisted': self.attempts.enabled and recorded})
        finally:
            try:
                if not terminal:
                    self.record_failure(user['id'], 'interrupted', metadata, error_code, context_info)
            finally:
                self.cancellations.pop(conversation_id, None)
                self.busy.discard(conversation_id)
