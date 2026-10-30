"""SQLite channel state. All chat bodies remain in canonical Message rows."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
from types import SimpleNamespace

from sqlalchemy import text

from app.core.config import AppError
from app.models import Conversation, Message, uid, utcnow
from app.services.channels.protocol import split_reply
from app.services.conversation.attempts import interrupt_attempts


def later(now, seconds):
    return (datetime.fromisoformat(now.replace('Z', '+00:00')) + timedelta(seconds=seconds)).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def row(session, sql, **params):
    result = session.execute(text(sql), params).mappings().first()
    return dict(result) if result else None


class ChannelStore:
    binding_id = 'qq-private'

    def __init__(self, sessions, config):
        self.sessions, self.config, self.owner = sessions, config, uid()
        with sessions() as s:
            self.supported = s.execute(text('SELECT version FROM schema_info')).scalar_one() >= 7

    @contextmanager
    def transaction(self):
        with self.sessions() as s:
            s.connection(execution_options={'sqlite_begin_immediate': True})
            yield s
            s.commit()

    def check_owner(self, s):
        value = row(s, 'SELECT * FROM channel_runtime WHERE id=1')
        if value['owner_token'] != self.owner or value['lease_expires_at'] <= utcnow():
            raise AppError('channel_fenced', '通道运行租约失效；请停止重复进程。', 409)
        return value

    def acquire(self):
        if not self.supported:
            if self.config.enabled:
                raise AppError('migration_required', 'QQ需要显式迁移的v7副本；不会自动升级正式库。', 409)
            return
        with self.transaction() as s:
            old = row(s, 'SELECT * FROM channel_runtime WHERE id=1')
            if old['owner_token'] and old['lease_expires_at'] > utcnow():
                raise AppError('channel_process_busy', '该v7数据库已有活动API进程；只允许单进程。', 409)
            binding = row(s, 'SELECT * FROM channel_bindings WHERE id=:id', id=self.binding_id)
            if self.config.enabled and binding and (binding['bot_id'], binding['owner_id'], binding['canonical_user_id']) != (self.config.bot_id, self.config.owner_id, self.config.canonical_user_id):
                raise AppError('channel_binding_changed', '已有身份绑定不同；不得将旧会话静默交给新账号。', 409)
            now = utcnow()
            s.execute(text('UPDATE channel_runtime SET owner_token=:owner,lease_expires_at=:expires WHERE id=1'),
                      dict(owner=self.owner, expires=later(now, self.config.runtime_lease_seconds)))
            s.execute(text("UPDATE channel_connections SET state='restarted',disconnected_at=:now,error_code='process_restart' WHERE state='connected'"), dict(now=now))
            s.execute(text("UPDATE channel_deliveries SET state='unknown',updated_at=:now,error_code='process_restart' WHERE state='sending'"), dict(now=now))
            self.block_following(s)
            # A remote request may have executed: never silently repeat it.
            s.execute(text("UPDATE channel_turns SET state='interrupted',error_code='process_restart',updated_at=:now WHERE state='generating'"), dict(now=now))

    def renew(self):
        if self.supported:
            with self.transaction() as s:
                self.check_owner(s)
                s.execute(text('UPDATE channel_runtime SET lease_expires_at=:expires WHERE id=1'),
                          dict(expires=later(utcnow(), self.config.runtime_lease_seconds)))

    def release(self):
        if self.supported:
            with self.transaction() as s:
                s.execute(text('UPDATE channel_runtime SET owner_token=NULL,lease_expires_at=NULL WHERE id=1 AND owner_token=:owner'), dict(owner=self.owner))

    def owned(self, conversation_id):
        if not self.supported:
            return False
        with self.sessions() as s:
            return row(s, 'SELECT id FROM channel_bindings WHERE conversation_id=:cid', cid=conversation_id) is not None

    def ingest(self, item, engine):
        """Source mapping + original Message + batch membership commit together."""
        with self.transaction() as s:
            self.check_owner(s)
            binding = row(s, 'SELECT * FROM channel_bindings WHERE id=:id', id=self.binding_id)
            if binding is None:
                conversation = Conversation(title='QQ 私聊')
                s.add(conversation); s.flush()
                binding = dict(id=self.binding_id, canonical_user_id=self.config.canonical_user_id,
                               bot_id=self.config.bot_id, owner_id=self.config.owner_id,
                               conversation_id=conversation.id, created_at=utcnow())
                s.execute(text('INSERT INTO channel_bindings VALUES (:id,:canonical_user_id,:bot_id,:owner_id,:conversation_id,:created_at)'), binding)
            old = row(s, 'SELECT * FROM channel_messages WHERE binding_id=:binding AND external_message_id=:external',
                      binding=binding['id'], external=item.external_id)
            if old:
                if old['payload_sha256'] != item.payload_sha256:
                    s.execute(text('INSERT INTO channel_collisions VALUES (:id,:source,:sha,:now)'),
                              dict(id=uid(), source=old['id'], sha=item.payload_sha256, now=utcnow()))
                    return dict(state='id_collision')
                return dict(state='duplicate', message_id=old['canonical_message_id'])
            now = utcnow()
            control = item.content.strip() in ('/stop', '/停止')
            turn = None
            if not control:
                turn = row(s, "SELECT * FROM channel_turns WHERE binding_id=:id AND state='open'", id=binding['id'])
                if turn:
                    size = row(s, 'SELECT count(*) n,coalesce(sum(length(m.content)),0) chars FROM channel_messages c JOIN messages m ON m.id=c.canonical_message_id WHERE c.turn_id=:id', id=turn['id'])
                    if size['n'] >= self.config.max_batch_messages or size['chars'] + len(item.content) > self.config.max_batch_chars:
                        s.execute(text("UPDATE channel_turns SET state='queued',updated_at=:now WHERE id=:id"), dict(now=now, id=turn['id']))
                        turn = None
                if turn is None:
                    seq = s.execute(text('SELECT coalesce(max(sequence),0)+1 FROM channel_turns WHERE binding_id=:id'), dict(id=binding['id'])).scalar_one()
                    turn = dict(id=uid(), sequence=seq, first_received_at=now)
                    s.execute(text("""INSERT INTO channel_turns (id,binding_id,sequence,state,config_id,request_id,first_received_at,last_received_at,available_at,cancel_requested,created_at,updated_at)
                        VALUES (:id,:binding,:sequence,'open',:config,:request,:now,:now,:available,0,:now,:now)"""),
                              dict(**turn, binding=binding['id'], config=self.config.config_id,
                                   request='qq_' + turn['id'], now=now, available=later(now, self.config.aggregate_seconds)))
            metadata = dict(channel='qq_onebot_v11', source_system=item.source_system, channel_binding_id=binding['id'],
                            external_message_id=item.external_id, external_conversation=f"{self.config.bot_id}:private:{self.config.owner_id}",
                            external_time=item.external_time, received_at=now, source_role='user',
                            channel_turn_id=turn['id'] if turn else None, channel_control=control)
            if item.transport:
                metadata.update(transport=item.transport, segments=list(item.segments))
            message = engine.append_user(s, binding['conversation_id'], item.content, self.config.config_id,
                                         created_at=item.created_at, metadata=metadata)
            s.execute(text('INSERT INTO channel_messages VALUES (:id,:binding,:external,:sha,:message,:turn,:time,:received,:kind)'),
                      dict(id=uid(), binding=binding['id'], external=item.external_id, sha=item.payload_sha256,
                           message=message.id, turn=turn['id'] if turn else None, time=item.external_time,
                           received=now, kind='control' if control else 'text'))
            if control:
                target = row(s, "SELECT * FROM channel_turns WHERE binding_id=:id AND state IN ('generating','queued','open') ORDER BY CASE state WHEN 'generating' THEN 0 ELSE 1 END,sequence LIMIT 1", id=binding['id'])
                if target:
                    self.cancel_in(s, target['id'])
                return dict(state='control', message_id=message.id, cancel_turn_id=target['id'] if target else None)
            size = row(s, 'SELECT count(*) n,sum(length(m.content)) chars FROM channel_messages c JOIN messages m ON m.id=c.canonical_message_id WHERE c.turn_id=:id', id=turn['id'])
            pending = s.execute(text("SELECT count(*) FROM channel_messages c JOIN channel_turns t ON t.id=c.turn_id WHERE t.state IN ('open','queued')")).scalar_one()
            full = pending > self.config.max_pending_messages
            seal = full or size['n'] >= self.config.max_batch_messages or size['chars'] >= self.config.max_batch_chars or len(item.content) >= self.config.long_message_chars
            available = min(later(now, self.config.aggregate_seconds), later(turn['first_received_at'], self.config.max_wait_seconds))
            s.execute(text('UPDATE channel_turns SET state=:state,anchor_message_id=:anchor,last_received_at=:now,available_at=:available,updated_at=:now,error_code=:error WHERE id=:id'),
                      dict(state='failed' if full else 'queued' if seal else 'open', anchor=message.id, now=now,
                           available=now if seal else available, error='queue_full' if full else None, id=turn['id']))
            return dict(state='persisted', message_id=message.id, turn_id=turn['id'])

    def sources(self, s, turn_id):
        return list(s.execute(text('SELECT canonical_message_id FROM channel_messages WHERE turn_id=:id ORDER BY rowid'), dict(id=turn_id)).scalars())

    def claim(self):
        with self.transaction() as s:
            if self.check_owner(s)['paused']:
                return None
            now = utcnow()
            expired = s.execute(text("SELECT id,request_id FROM channel_turns WHERE state='generating' AND lease_expires_at<=:now"), dict(now=now)).mappings().all()
            for stale in expired:
                s.execute(text("UPDATE channel_turns SET state='interrupted',error_code='generation_lease_expired',updated_at=:now WHERE id=:id"), dict(now=now,id=stale['id']))
                interrupt_attempts(s,stale['request_id'])
            if row(s, "SELECT id FROM channel_turns WHERE state='generating'"):
                return None
            value = row(s, "SELECT t.*,b.conversation_id FROM channel_turns t JOIN channel_bindings b ON b.id=t.binding_id WHERE t.state IN ('open','queued') ORDER BY t.sequence LIMIT 1")
            if not value or value['state'] == 'open' and value['available_at'] > now:
                return None
            value['lease_token'] = uid()
            value['lease_expires_at'] = later(now, self.config.generation_timeout + 10)
            s.execute(text("UPDATE channel_turns SET state='generating',lease_token=:lease_token,lease_expires_at=:lease_expires_at,updated_at=:now WHERE id=:id"), dict(value, now=now))
            value['source_ids'] = self.sources(s, value['id'])
            value['content'] = '\n'.join(s.get(Message, id).content for id in value['source_ids'])
            return value

    def validate_turn(self, s, turn):
        self.check_owner(s)
        current = row(s, 'SELECT * FROM channel_turns WHERE id=:id', id=turn['id'])
        if current['cancel_requested']:
            raise AppError('cancelled', '该QQ轮次已停止；原文保留。', 409)
        if current['state'] != 'generating' or current['lease_token'] != turn['lease_token'] or current['lease_expires_at'] <= utcnow():
            raise AppError('channel_fenced', '该轮次的生成租约已失效。', 409)

    def complete(self, s, turn, reply):
        self.validate_turn(s, turn)
        now = utcnow()
        s.execute(text("UPDATE channel_turns SET state='completed',reply_message_id=:reply,updated_at=:now WHERE id=:id"), dict(reply=reply.id, now=now, id=turn['id']))
        chunks = list(split_reply(reply.content, self.config.reply_bytes))
        blocked = len(chunks) > self.config.max_reply_parts
        for index, (start, end) in enumerate(chunks):
            id = uid()
            s.execute(text("""INSERT INTO channel_deliveries (id,turn_id,assistant_message_id,part_index,start_offset,end_offset,text_sha256,state,echo,error_code,created_at,updated_at)
                VALUES (:id,:turn,:reply,:part,:start,:end,:sha,:state,:echo,:error,:now,:now)"""),
                      dict(id=id, turn=turn['id'], reply=reply.id, part=index, start=start, end=end,
                           sha=hashlib.sha256(reply.content[start:end].encode()).hexdigest(), state='blocked' if blocked else 'pending',
                           echo='qq-send-' + id, error='reply_too_long' if blocked else None, now=now))

    def fail(self, turn, code, *, state='failed'):
        with self.transaction() as s:
            self.check_owner(s)
            s.execute(text('UPDATE channel_turns SET state=:state,error_code=:code,updated_at=:now WHERE id=:id AND state=\'generating\' AND lease_token=:lease'),
                      dict(state=state, code=code, now=utcnow(), id=turn['id'], lease=turn['lease_token']))

    def cancel_in(self, s, id):
        target = row(s, 'SELECT * FROM channel_turns WHERE id=:id', id=id)
        if target is None:
            raise AppError('not_found', '轮次不存在。', 404)
        state = 'cancelled' if target['state'] in ('open', 'queued') else target['state']
        s.execute(text('UPDATE channel_turns SET state=:state,cancel_requested=1,updated_at=:now WHERE id=:id'), dict(state=state, now=utcnow(), id=id))
        s.execute(text("UPDATE channel_deliveries SET state='cancelled',updated_at=:now,error_code='cancelled' WHERE turn_id=:id AND state='pending'"), dict(now=utcnow(), id=id))
        return target

    def cancel(self, id):
        with self.transaction() as s:
            self.check_owner(s)
            return self.cancel_in(s, id)

    def pause(self, paused):
        with self.transaction() as s:
            self.check_owner(s)
            s.execute(text('UPDATE channel_runtime SET paused=:paused WHERE id=1'), dict(paused=int(paused)))

    def context(self, s, turn, context, memories, provider):
        recent = s.execute(text("SELECT * FROM channel_turns WHERE binding_id=:binding AND sequence<:sequence ORDER BY sequence DESC LIMIT 100"),
                           dict(binding=turn['binding_id'], sequence=turn['sequence'])).mappings().all()
        pairs = []
        for old in recent:
            if old['state'] != 'completed':
                break  # Do not silently join across an unanswered turn.
            sources=tuple(s.get(Message,id) for id in self.sources(s,old['id']))
            content = '\n'.join(m.content for m in sources)
            pairs.append((SimpleNamespace(role='user', content=content, source_rows=sources), s.get(Message, old['reply_message_id'])))
        history = [m for pair in reversed(pairs) for m in pair]
        anchor=s.get(Message,turn['anchor_message_id'])
        sources=tuple(s.get(Message,id) for id in self.sources(s,turn['id']))
        history.append(SimpleNamespace(role='user', content=turn['content'], id=anchor.id, created_at=anchor.created_at, metadata_json=anchor.metadata_json, source_rows=sources))
        hint = '本轮通过QQ私聊。'
        return context.plan(history, memories, provider, more_history=len(recent) >= 100 or len(pairs) < len(recent), channel_hint=hint)

    def open_connection(self):
        id, now = uid(), utcnow()
        with self.transaction() as s:
            self.check_owner(s)
            s.execute(text("INSERT INTO channel_connections (id,state,connected_at,last_received_at) VALUES (:id,'connected',:now,:now)"), dict(id=id, now=now))
        return id

    def connection_seen(self, id, version=None, app_name=None):
        with self.transaction() as s:
            self.check_owner(s)
            s.execute(text('UPDATE channel_connections SET last_received_at=:now,app_name=coalesce(:app,app_name),app_version=coalesce(:version,app_version) WHERE id=:id AND state=\'connected\''),
                      dict(now=utcnow(), app=app_name or ('NapCat.Onebot' if version else None), version=version, id=id))

    def block_following(self, s):
        s.execute(text("""UPDATE channel_deliveries SET state='blocked',error_code='previous_delivery_unconfirmed',updated_at=:now
            WHERE state='pending' AND EXISTS (SELECT 1 FROM channel_deliveries previous
                WHERE previous.turn_id=channel_deliveries.turn_id AND previous.part_index<channel_deliveries.part_index
                AND previous.state IN ('unknown','failed','blocked','cancelled'))"""), dict(now=utcnow()))

    def close_connection(self, id, code='disconnected'):
        with self.transaction() as s:
            self.check_owner(s)
            now = utcnow()
            s.execute(text("UPDATE channel_connections SET state=:state,disconnected_at=:now,error_code=:code WHERE id=:id AND state='connected'"),
                      dict(state='timed_out' if code == 'heartbeat_timeout' else 'rejected' if code == 'preflight_failed' else 'closed', now=now, code=code, id=id))
            s.execute(text("UPDATE channel_deliveries SET state='unknown',updated_at=:now,error_code=:code WHERE connection_id=:id AND state='sending'"), dict(now=now, code=code, id=id))
            self.block_following(s)

    def next_delivery(self, connection_id):
        with self.transaction() as s:
            if self.check_owner(s)['paused']:
                return None
            now = utcnow()
            s.execute(text("UPDATE channel_deliveries SET state='unknown',updated_at=:now,error_code='receipt_timeout' WHERE state='sending' AND updated_at<=:expired"),
                      dict(now=now, expired=later(now, -self.config.receipt_timeout)))
            self.block_following(s)
            if row(s, "SELECT id FROM channel_deliveries WHERE state='sending'"):
                return None
            delivery = row(s, """SELECT d.*,b.owner_id FROM channel_deliveries d JOIN channel_turns t ON t.id=d.turn_id
                JOIN channel_bindings b ON b.id=t.binding_id WHERE d.state='pending' AND t.cancel_requested=0
                ORDER BY t.sequence,d.part_index LIMIT 1""")
            if delivery is None:
                return None
            s.execute(text("UPDATE channel_deliveries SET state='sending',connection_id=:connection,updated_at=:now WHERE id=:id"),
                      dict(connection=connection_id, now=now, id=delivery['id']))
            message = s.get(Message, delivery['assistant_message_id'])
            delivery['content'] = message.content[delivery['start_offset']:delivery['end_offset']]
            return delivery

    def receipt(self, connection_id, payload):
        if not isinstance(payload, dict) or type(payload.get('echo')) is not str:
            return False
        data = payload.get('data')
        valid_id = isinstance(data, dict) and type(data.get('message_id')) in (int, str) and 0 < len(str(data['message_id'])) <= 128
        success = payload.get('status') == 'ok' and type(payload.get('retcode')) is int and payload['retcode'] == 0 and valid_id
        definite_failure = payload.get('status') == 'failed' and type(payload.get('retcode')) is int and payload['retcode'] != 0
        with self.transaction() as s:
            self.check_owner(s)
            changed = s.execute(text("UPDATE channel_deliveries SET state=:state,remote_message_id=:remote,error_code=:code,updated_at=:now WHERE echo=:echo AND connection_id=:connection AND state='sending'"),
                                dict(state='sent' if success else 'failed' if definite_failure else 'unknown', remote=str(data['message_id']) if success else None,
                                     code=None if success else 'onebot_rejected' if definite_failure else 'invalid_receipt', now=utcnow(), echo=payload['echo'], connection=connection_id)).rowcount
            self.block_following(s)
            return bool(changed)

    def status(self):
        if not self.supported:
            return dict(supported=False, enabled=False, schema_required=7)
        with self.sessions() as s:
            paused = s.execute(text('SELECT paused FROM channel_runtime WHERE id=1')).scalar_one()
            counts = lambda table: dict(s.execute(text(f'SELECT state,count(*) FROM {table} GROUP BY state')).all())
            latest = row(s, 'SELECT id,state,app_name,app_version,connected_at,last_received_at,disconnected_at,error_code FROM channel_connections ORDER BY rowid DESC LIMIT 1')
            return dict(supported=True, enabled=self.config.enabled, paused=bool(paused), model_config=self.config.config_id,
                        canonical_user_id=self.config.canonical_user_id, turns=counts('channel_turns'), deliveries=counts('channel_deliveries'),
                        collisions=s.execute(text('SELECT count(*) FROM channel_collisions')).scalar_one(), connection=latest)

    def list_turns(self):
        with self.sessions() as s:
            return [dict(x) for x in s.execute(text('SELECT id,sequence,state,config_id,anchor_message_id,reply_message_id,cancel_requested,error_code,created_at,updated_at FROM channel_turns ORDER BY sequence DESC LIMIT 50')).mappings()]

    def list_deliveries(self):
        with self.sessions() as s:
            return [dict(x) for x in s.execute(text('SELECT id,turn_id,part_index,state,remote_message_id,error_code,created_at,updated_at FROM channel_deliveries ORDER BY rowid DESC LIMIT 100')).mappings()]
