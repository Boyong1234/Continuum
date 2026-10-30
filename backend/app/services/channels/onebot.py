"""Authenticated reverse WebSocket transport + durable serial turn coordinator."""
import asyncio
from contextlib import aclosing
import hmac
import ipaddress
import json
import logging
import time

from fastapi import WebSocket, WebSocketDisconnect
from anyio import CancelScope, ClosedResourceError, BrokenResourceError
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import AppError
from app.schemas import ChatRequest
from app.services.channels.protocol import parse_private
from app.services.channels.store import ChannelStore

logger = logging.getLogger(__name__)


class OneBotChannelAdapter:
    def __init__(self, sessions, engine, config):
        self.config, self.engine = config, engine
        self.store = ChannelStore(sessions, config)
        self.stop_event = asyncio.Event()
        self.wakeup = asyncio.Event()
        self.connection_lock = asyncio.Lock()
        self.connection = None
        self.generation = None
        self.current_turn = None
        self.loop_task = None
        self.fatal_code = None
        self.ignored_events = 0
        self.last_send = 0.0

    async def start(self):
        await asyncio.to_thread(self.store.acquire)
        if not self.store.supported:
            return
        try:
            await asyncio.to_thread(self.engine.attempts.recover)
            if self.config.enabled:
                self.engine.providers.get(self.config.config_id)  # Explicit selection, never fallback.
            self.loop_task = asyncio.create_task(self.run(), name='onebot-channel-coordinator')
        except BaseException:
            await asyncio.to_thread(self.store.release)
            raise

    async def stop(self):
        self.stop_event.set(); self.wakeup.set()
        if self.current_turn:
            self.engine.cancel(self.current_turn['conversation_id'])
        if self.connection:
            try:await self.connection['socket'].close(code=1001)
            except (RuntimeError, WebSocketDisconnect, ClosedResourceError, BrokenResourceError):pass
        if self.generation:
            try:await asyncio.wait_for(self.generation, 3)
            except (TimeoutError, asyncio.CancelledError):pass
        if self.loop_task:
            try:await asyncio.wait_for(self.loop_task, 3)
            except (TimeoutError, asyncio.CancelledError):pass
        await asyncio.to_thread(self.store.release)

    async def run(self):
        renewed = time.monotonic()
        errors = 0
        while not self.stop_event.is_set():
            try:
                if time.monotonic() - renewed >= self.config.runtime_lease_seconds / 3:
                    await asyncio.to_thread(self.store.renew)
                    renewed = time.monotonic()
                if self.generation and self.generation.done():
                    await self.generation
                    self.generation = None
                connection = self.connection
                if self.config.enabled and connection and connection['ready']:
                    if self.generation is None:
                        turn = await asyncio.to_thread(self.store.claim)
                        if turn:
                            self.generation = asyncio.create_task(self.generate(turn), name='qq-turn-' + turn['id'])
                    if time.monotonic() - self.last_send >= self.config.send_interval:
                        delivery = await asyncio.to_thread(self.store.next_delivery, connection['id'])
                        if delivery:
                            # State is committed BEFORE touching the network.
                            if self.connection is not connection:
                                await asyncio.to_thread(self.store.close_connection, connection['id'])
                            else:
                                await asyncio.wait_for(connection['socket'].send_json(dict(action='send_private_msg',
                                    params=dict(user_id=self.config.owner_id, message=[dict(type='text', data=dict(text=delivery['content']))]), echo=delivery['echo'])), min(2, self.config.runtime_lease_seconds / 3))
                                self.last_send = time.monotonic()
                errors = 0
            except (WebSocketDisconnect, RuntimeError, OSError, TimeoutError, ClosedResourceError, BrokenResourceError) as exc:
                failed_connection=self.connection
                if failed_connection:
                    await asyncio.to_thread(self.store.close_connection, failed_connection['id'], 'send_timeout' if isinstance(exc,TimeoutError) else 'send_error')
                    failed_connection['ready'] = False
                    try:await failed_connection['socket'].close(code=1011)
                    except (RuntimeError,WebSocketDisconnect,ClosedResourceError,BrokenResourceError):pass
            except AppError:
                self.fatal_code = 'channel_fenced'
                self.stop_event.set()
                if self.current_turn:self.engine.cancel(self.current_turn['conversation_id'])
            except SQLAlchemyError:
                errors += 1
                if errors >= 5:
                    self.fatal_code = 'database_error'; self.stop_event.set()
                    if self.current_turn:self.engine.cancel(self.current_turn['conversation_id'])
                logger.warning('channel_error code=database_error')  # No exception/payload logging.
            except Exception:
                self.fatal_code = 'internal_error'; self.stop_event.set()
                if self.current_turn:self.engine.cancel(self.current_turn['conversation_id'])
                logger.warning('channel_error code=internal_error')
            if self.stop_event.is_set() and self.connection:
                try:await self.connection['socket'].close(code=1011)
                except (RuntimeError,WebSocketDisconnect,ClosedResourceError,BrokenResourceError):pass
            self.wakeup.clear()
            try:await asyncio.wait_for(self.wakeup.wait(), .05 if errors == 0 else .2)
            except TimeoutError:pass

    async def generate(self, turn):
        self.current_turn = turn
        try:
            request = ChatRequest(content=turn['content'], config_id=turn['config_id'], request_id=turn['request_id'])
            user = self.engine.prepare_existing(turn['conversation_id'], request, turn['source_ids'],
                                                validate=lambda s: self.store.validate_turn(s, turn))
            async with asyncio.timeout(self.config.generation_timeout):
                async with aclosing(self.engine.stream(turn['conversation_id'], request, user,
                    context_factory=lambda s, memories, provider: self.store.context(s, turn, self.engine.context, memories, provider),
                    on_reply=lambda s, reply: self.store.complete(s, turn, reply),
                    reply_metadata=dict(channel='qq_onebot_v11', channel_turn_id=turn['id'], source_user_ids=turn['source_ids']))) as stream:
                    async for kind, payload in stream:
                        if kind == 'error':
                            await asyncio.to_thread(self.store.fail, turn, payload['code'], state='cancelled' if payload['code'] == 'cancelled' else 'failed')
        except TimeoutError:
            await asyncio.to_thread(self.store.fail, turn, 'generation_timeout', state='interrupted')
        except asyncio.CancelledError:
            await asyncio.to_thread(self.store.fail, turn, 'shutdown', state='interrupted')
            raise
        except (AppError, ValueError):
            await asyncio.to_thread(self.store.fail, turn, 'generation_rejected')
        except SQLAlchemyError:
            # The durable generating row is recovered as interrupted, never replayed.
            logger.warning('channel_error code=database_error')
        finally:
            self.current_turn = None
            self.wakeup.set()

    async def cancel(self, id):
        target = await asyncio.to_thread(self.store.cancel, id)
        if self.current_turn and self.current_turn['id'] == id:
            self.engine.cancel(self.current_turn['conversation_id'])
        self.wakeup.set()
        return dict(cancel_requested=True, turn_id=id)

    async def ingest(self, item):
        result = await asyncio.to_thread(self.store.ingest, item, self.engine)
        if result.get('cancel_turn_id'):
            if self.current_turn and self.current_turn['id'] == result['cancel_turn_id']:
                self.engine.cancel(self.current_turn['conversation_id'])
        self.wakeup.set()
        return result

    async def websocket(self, ws: WebSocket):
        try:local = ipaddress.ip_address(ws.client.host).is_loopback
        except (ValueError, AttributeError):local = False
        expected = ('Bearer ' + self.config.token).encode()
        authenticated = hmac.compare_digest(ws.headers.get('authorization', '').encode(), expected)
        if (not self.config.enabled or not self.store.supported or self.stop_event.is_set() or not local
            or not authenticated or ws.headers.get('x-self-id') != self.config.bot_id
            or ws.headers.get('x-client-role', '').lower() != 'universal' or ws.headers.get('origin') is not None):
            await ws.close(code=1008)
            return
        async with self.connection_lock:
            if self.connection:
                await ws.close(code=1013)
                return
            await ws.accept()
            id = await asyncio.to_thread(self.store.open_connection)
            connection = dict(id=id, socket=ws, ready=False)
            self.connection = connection
        code = 'disconnected'
        version_ok = login_ok = False
        waiting = []
        deadline = time.monotonic() + self.config.handshake_timeout
        try:
            await ws.send_json(dict(action='get_version_info', params={}, echo=id + ':version'))
            await ws.send_json(dict(action='get_login_info', params={}, echo=id + ':login'))
            while not self.stop_event.is_set():
                remaining = self.config.heartbeat_timeout if connection['ready'] else max(.001, deadline - time.monotonic())
                try:raw = await asyncio.wait_for(ws.receive_text(), remaining)
                except TimeoutError:
                    code = 'heartbeat_timeout' if connection['ready'] else 'preflight_failed'
                    await ws.close(code=1001)
                    break
                if len(raw) > self.config.max_frame_bytes or len(raw.encode('utf-8')) > self.config.max_frame_bytes:
                    code = 'frame_too_large'; await ws.close(code=1009); break
                try:payload = json.loads(raw)
                except (ValueError, RecursionError):
                    code = 'invalid_frame'; await ws.close(code=1008); break
                if not isinstance(payload, dict):
                    code = 'invalid_frame'; await ws.close(code=1008); break
                await asyncio.to_thread(self.store.connection_seen, id)
                if payload.get('echo') in (id + ':version', id + ':login'):
                    data = payload.get('data')
                    if payload.get('status') != 'ok' or type(payload.get('retcode')) is not int or payload['retcode'] != 0 or not isinstance(data, dict):
                        code = 'preflight_failed'; await ws.close(code=1008); break
                    if payload['echo'].endswith(':version'):
                        version_ok = data.get('app_name') == 'NapCat.Onebot' and data.get('protocol_version') == 'v11' and str(data.get('app_version', '')).lstrip('v') == self.config.expected_version
                        if not version_ok:
                            code = 'preflight_failed'; await ws.close(code=1008); break
                    else:
                        login_ok = str(data.get('user_id')) == self.config.bot_id
                        if not login_ok:
                            code = 'preflight_failed'; await ws.close(code=1008); break
                    if version_ok and login_ok and not connection['ready']:
                        await asyncio.to_thread(self.store.connection_seen, id, self.config.expected_version)
                        connection['ready'] = True
                        for item in waiting:await self.ingest(item)
                        waiting.clear(); self.wakeup.set()
                    continue
                if 'post_type' not in payload and 'echo' in payload:
                    if connection['ready']:await asyncio.to_thread(self.store.receipt, id, payload)
                    self.wakeup.set()
                    continue
                if payload.get('post_type') == 'meta_event':
                    if str(payload.get('self_id')) != self.config.bot_id:
                        self.ignored_events += 1
                    elif payload.get('meta_event_type') == 'heartbeat' and isinstance(payload.get('status'), dict) and payload['status'].get('online') is False:
                        code = 'upstream_offline'; await ws.close(code=1001); break
                    continue
                item = parse_private(payload, self.config)
                if item is None:
                    self.ignored_events += 1
                    continue
                if connection['ready']:
                    await self.ingest(item)
                elif len(waiting) < 32:
                    waiting.append(item)
                else:
                    code = 'preflight_buffer_full'; await ws.close(code=1009); break
        except (WebSocketDisconnect, RuntimeError, OSError, ClosedResourceError, BrokenResourceError):
            pass
        except SQLAlchemyError:
            code = 'database_error'
            try:await ws.close(code=1011)
            except (RuntimeError, WebSocketDisconnect):pass
        finally:
            connection['ready'] = False
            if self.connection is connection:self.connection = None
            with CancelScope(shield=True):
                try:await asyncio.to_thread(self.store.close_connection, id, code)
                except (AppError, SQLAlchemyError):pass
            self.wakeup.set()
