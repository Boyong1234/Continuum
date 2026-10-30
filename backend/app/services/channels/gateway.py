"""Loopback AstrBot gateway API. Reuses v7 ChannelStore + native generation.

No new archive schema, context, model, memory or AstrBot intelligence.
"""
import asyncio
from dataclasses import dataclass, field
import hmac
import ipaddress
import logging
import os
from pathlib import Path
import re
import sqlite3
import time

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from app.core.config import AppError, ROOT, Settings
from app.services.channels.config import OneBotConfig
from app.services.channels.onebot import OneBotChannelAdapter
from app.services.channels.protocol import PrivateText, fingerprint, parse_private

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GatewayConfig:
    account_id: str
    sender_id: str
    token: str = field(repr=False)
    admin_token: str = field(repr=False)
    model_config: str = 'mock'
    enabled: bool = False
    platform_id: str = 'continuum-onebot'
    upstream_app: str = 'SnowLuma'
    upstream_version: str = '2.2.1-node'

    def channel(self):
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', self.platform_id):
            raise ValueError('Invalid platform identifier')
        return OneBotConfig(enabled=self.enabled,bot_id=self.account_id,owner_id=self.sender_id,
                            token=self.token,admin_token=self.admin_token,config_id=self.model_config)

    @classmethod
    def environment(cls):
        return cls(account_id=os.getenv('CONTINUUM_GATEWAY_ACCOUNT_ID',''),sender_id=os.getenv('CONTINUUM_GATEWAY_SENDER_ID',''),
                   token=os.getenv('CONTINUUM_BRIDGE_TOKEN',''),admin_token=os.getenv('CONTINUUM_GATEWAY_ADMIN_TOKEN',''),
                   model_config=os.getenv('CONTINUUM_GATEWAY_MODEL_CONFIG','mock'),
                   enabled=os.getenv('CONTINUUM_GATEWAY_ENABLED','false').lower()=='true',
                   platform_id=os.getenv('ASTRBOT_BRIDGE_PLATFORM_ID','continuum-onebot'))


class Strict(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')


class Connect(Strict):
    client_id: str = Field(pattern=r'^[a-zA-Z0-9_-]{1,128}$')
    account_id: str
    sender_id: str
    platform_id: str
    app_name: str
    app_version: str
    online: bool


class Epoch(Strict):
    connection_id: str = Field(max_length=64)


class Envelope(Strict):
    channel: str = 'qq'
    transport: str = 'astrbot_onebot_v11'
    platform: str = 'qq'
    account_id: str
    conversation_type: str
    conversation_id: str
    sender_id: str
    message_id: str = Field(min_length=1,max_length=128)
    timestamp: int
    segments: list[dict] = Field(min_length=1,max_length=100)
    plain_text: str = Field(max_length=100000)
    raw_event_id: str = Field(min_length=1,max_length=128)


def normalized_item(body, config):
    if (body.channel,body.transport,body.platform,body.account_id,body.sender_id,body.conversation_type,body.conversation_id,body.raw_event_id) != (
            'qq','astrbot_onebot_v11','qq',config.account_id,config.sender_id,'private',config.sender_id,body.message_id):
        raise AppError('source_denied','来源不符合固定私聊绑定。',403)
    chunks=[];metadata=[]
    for segment in body.segments:
        if set(segment)!={'type','data'} or not isinstance(segment['data'],dict):
            raise AppError('invalid_segment','不支持此消息段。',422)
        kind,data=segment['type'],segment['data']
        if kind=='text' and set(data)=={'text'} and isinstance(data['text'],str):
            chunks.append(data['text']);metadata.append(segment)
        elif kind in ('reply','image','forward'):
            if set(data)-{'id','width','height','sub_type'} or any(type(v) not in (int,str) or len(str(v))>128 for v in data.values()):
                raise AppError('invalid_segment','仅允许有限媒体元数据。',422)
            chunks.append({'reply':'[引用消息，未展开]','image':'[图片，暂未解析]','forward':'[转发材料，未展开]'}[kind])
            metadata.append(segment)
        else:
            raise AppError('invalid_segment','本阶段只支持文本和有限媒体元数据。',422)
    text=''.join(chunks)
    if text!=body.plain_text:
        raise AppError('invalid_segment','文本与消息段不一致。',422)
    raw=dict(post_type='message',message_type='private',sub_type='friend',self_id=config.account_id,
             user_id=config.sender_id,message_id=body.message_id,time=body.timestamp,message=[dict(type='text',data=dict(text=text))])
    parsed=parse_private(raw,config.channel())
    if parsed is None:
        raise AppError('invalid_event','原始消息 ID、时间或文本无效。',422)
    # Plain-text cross-transport replay retains the ORIGINAL fingerprint algorithm.
    sha=parsed.payload_sha256 if all(s['type']=='text' for s in metadata) else fingerprint([text,body.timestamp,metadata])
    return PrivateText(parsed.external_id,text,parsed.external_time,parsed.created_at,sha,
                       source_system='onebot',transport='astrbot_onebot_v11',segments=tuple(metadata))


class BridgeCoordinator(OneBotChannelAdapter):
    """Only transport scheduling differs; generate/cancel/ingest remain inherited."""
    def __init__(self,sessions,engine,config):
        super().__init__(sessions,engine,config.channel())
        self.gateway_config=config;self.epoch=None;self.client_id=None;self.last_poll=0.;self.qq_state='unknown'

    async def websocket(self,ws):
        await ws.close(code=1008)  # native fallback is a separate, mutually exclusive entry point.

    async def stop(self):
        if self.epoch:
            await asyncio.to_thread(self.store.close_connection,self.epoch,'gateway_shutdown')
            self.epoch=None
        await super().stop()

    async def run(self):
        renewed=time.monotonic()
        while not self.stop_event.is_set():
            try:
                if time.monotonic()-renewed>=self.config.runtime_lease_seconds/3:
                    await asyncio.to_thread(self.store.renew);renewed=time.monotonic()
                if self.generation and self.generation.done():
                    await self.generation;self.generation=None
                # Persisted work survives the gateway/browser, with the SAME Engine.
                if self.config.enabled and self.generation is None:
                    turn=await asyncio.to_thread(self.store.claim)
                    if turn:self.generation=asyncio.create_task(self.generate(turn))
                if self.epoch and time.monotonic()-self.last_poll>20:
                    await asyncio.to_thread(self.store.close_connection,self.epoch,'bridge_timeout')
                    self.epoch=None;self.qq_state='unknown'
                self.wakeup.clear()
                try:await asyncio.wait_for(self.wakeup.wait(),.05)
                except TimeoutError:pass
            except asyncio.CancelledError:raise
            except Exception:
                self.fatal_code='gateway_coordinator_error';self.stop_event.set()
                logger.warning('gateway_error code=coordinator_stopped')
                if self.current_turn:self.engine.cancel(self.current_turn['conversation_id'])

    async def connect(self,body):
        cfg=self.gateway_config
        if (body.account_id,body.sender_id,body.platform_id,body.app_name,body.app_version)!=(cfg.account_id,cfg.sender_id,cfg.platform_id,cfg.upstream_app,cfg.upstream_version):
            raise AppError('source_denied','账号或锁定的上游版本不符。',403)
        async with self.connection_lock:
            if self.epoch and self.client_id==body.client_id:
                self.last_poll=time.monotonic();self.qq_state='online' if body.online else 'offline'
                return self.epoch
            if self.epoch:await asyncio.to_thread(self.store.close_connection,self.epoch,'gateway_restart')
            self.epoch=await asyncio.to_thread(self.store.open_connection)
            await asyncio.to_thread(self.store.connection_seen,self.epoch,body.app_version,body.app_name)
            self.client_id=body.client_id;self.last_poll=time.monotonic();self.qq_state='online' if body.online else 'offline'
            return self.epoch

    def check_epoch(self,id):
        if id!=self.epoch or self.stop_event.is_set():raise AppError('stale_connection','网关连接需重新认证。',409)
        self.last_poll=time.monotonic()

    def health(self):
        live=self.epoch is not None and time.monotonic()-self.last_poll<=20
        layer=lambda status,evidence:dict(status=status,evidence=evidence)
        return dict(snowluma_process=layer('unknown','no process inspection'),
            qq_session=layer(self.qq_state if live else 'unknown','verified RPC / bot_status notice'),
            onebot_ws=layer('connected' if live and self.qq_state=='online' else 'unknown','gateway RPC observation, not process presence'),
            astrbot=layer('responsive' if live else 'unknown','authenticated bridge heartbeat'),
            continuum_bridge=layer('ready' if live else 'waiting','connection epoch'),
            continuum_backend=layer('ready' if not self.stop_event.is_set() else 'stopped','local coordinator'),
            provider=layer('configured_not_probed','no paid health calls'))


router=APIRouter(prefix='/api/channels/bridge')


def authorized(request:Request):
    adapter=request.app.state.onebot
    cfg=getattr(adapter,'gateway_config',None)
    try:local=ipaddress.ip_address(request.client.host).is_loopback
    except (ValueError,AttributeError):local=False
    if not cfg or not cfg.enabled or not local or request.headers.get('origin') is not None or not hmac.compare_digest(request.headers.get('authorization','').encode(),('Bearer '+cfg.token).encode()):
        raise AppError('authentication_required','需要本地 Bridge 鉴权。',401)
    return adapter


@router.post('/connect')
async def connect(body:Connect,a=Depends(authorized)):
    return dict(connection_id=await a.connect(body))


@router.post('/events',status_code=202)
async def incoming(body:Envelope,a=Depends(authorized)):
    return await a.ingest(normalized_item(body,a.gateway_config))


@router.post('/poll')
async def poll(body:Epoch,a=Depends(authorized)):
    a.check_epoch(body.connection_id)
    await asyncio.to_thread(a.store.connection_seen,body.connection_id)
    delivery=await asyncio.to_thread(a.store.next_delivery,body.connection_id) if a.qq_state=='online' else None
    if not delivery:return dict(delivery=None)
    with a.store.sessions() as session:
        reply_to=session.execute(text('SELECT c.external_message_id FROM channel_messages c JOIN channel_turns t ON t.anchor_message_id=c.canonical_message_id WHERE t.id=:id'),dict(id=delivery['turn_id'])).scalar_one()
    # Offsets/splitting come ONLY from Continuum's existing Outbox.
    return dict(delivery=dict(delivery_id=delivery['id'],turn_id=delivery['turn_id'],reply_to=reply_to,
        echo=delivery['echo'],text=delivery['content'],segments=[dict(type='text',data=dict(text=delivery['content']))]))


class Receipt(Epoch):
    echo:str=Field(max_length=128)
    status:str=Field(pattern='^(sent|failed|unknown)$')
    message_id:str|None=Field(default=None,max_length=128)


@router.post('/receipt')
async def receipt(body:Receipt,a=Depends(authorized)):
    a.check_epoch(body.connection_id)
    payload=dict(echo=body.echo,status='ok' if body.status=='sent' else 'failed' if body.status=='failed' else 'unknown',
                 retcode=0 if body.status=='sent' else 1 if body.status=='failed' else None,data=dict(message_id=body.message_id) if body.message_id else {})
    return dict(recorded=await asyncio.to_thread(a.store.receipt,body.connection_id,payload))


class Notice(Epoch):
    account_id:str
    notice_type:str=Field(pattern='^bot_status$')
    sub_type:str=Field(pattern='^(online|offline)$')


@router.post('/notice')
async def notice(body:Notice,a=Depends(authorized)):
    a.check_epoch(body.connection_id)
    if body.account_id!=a.gateway_config.account_id:raise AppError('source_denied','状态账号不符。',403)
    a.qq_state=body.sub_type
    logger.info('gateway_status code=qq_%s',body.sub_type)
    return dict(recorded=True)


@router.get('/health')
async def health(a=Depends(authorized)):
    return dict(layers=a.health(),fatal_code=a.fatal_code)


def create_gateway_app(settings:Settings,config:GatewayConfig,providers=None,*,rehearsal_only=True):
    from app.main import create_app
    from dataclasses import replace
    path=Path(settings.database_url.removeprefix('sqlite:///')).resolve()
    if rehearsal_only and not path.is_relative_to(ROOT/'data/rehearsals'):
        raise AppError('dataset_denied','本阶段只允许隔离 rehearsal 数据集。',409)
    if not config.enabled:raise AppError('gateway_disabled','需显式启用隔离网关。',409)
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as conn:
        conn.execute('PRAGMA query_only=ON')
        if conn.execute('SELECT version FROM schema_info').fetchall()!=[(7,)]:raise AppError('migration_required','需要已有隔离 v7。',409)
        if conn.execute('SELECT enabled FROM memory_runtime').fetchone()[0]:raise AppError('memory_enabled','本阶段要求 Memory Worker OFF。',409)
    app=create_app(replace(settings,onebot=config.channel()),providers)
    a=BridgeCoordinator(app.state.sessions,app.state.conversation_engine,config)
    app.state.onebot=a
    app.state.conversation_engine.channel_guard=a.store.owned
    app.include_router(router)
    return app
