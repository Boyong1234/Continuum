"""AstrBot is a router. Continuum owns generation, archive, queue and Outbox."""
import asyncio
import logging
import uuid
import httpx
from aiocqhttp.exceptions import ActionFailed
from astrbot.api.event import filter,AstrMessageEvent
from astrbot.api.star import Context,Star
from .wire import BridgeSettings,envelope,passive_only
from . import gateway_platform  # register metadata-only OneBot transport profile

logger=logging.getLogger(__name__)


class ContinuumBridge(Star):
    def __init__(self,context:Context):
        super().__init__(context)
        self.context=context;self.cfg=BridgeSettings.environment()
        self.client_id=uuid.uuid4().hex;self.epoch=None;self.task=None;self.closing=False
        self.http=httpx.AsyncClient(base_url=self.cfg.url,headers={'Authorization':'Bearer '+self.cfg.token},
                                   timeout=3,trust_env=False,follow_redirects=False)

    async def initialize(self):
        if not passive_only(self.context.get_config()):raise RuntimeError('Continuum bridge requires AI/history/trace disabled')
        self.task=asyncio.create_task(self.pump(),name='continuum-bridge-outbox')

    async def terminate(self):
        self.closing=True
        if self.task:
            self.task.cancel();await asyncio.gather(self.task,return_exceptions=True)
        await self.http.aclose()

    async def post(self,path,body):
        response=await self.http.post('/api/channels/bridge/'+path,json=body)
        if response.status_code==409:self.epoch=None
        response.raise_for_status()
        return response.json()

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.ALL,priority=10000)
    async def bridge(self,event:AstrMessageEvent):
        if event.get_platform_id()!=self.cfg.platform_id:return
        # BEFORE any network await. Never fall through to AstrBot on errors.
        event.stop_event()
        if not passive_only(self.context.get_config(event.unified_msg_origin)):return
        raw=event.message_obj.raw_message
        try:
            if raw.get('post_type')=='notice':
                if self.epoch and raw.get('notice_type')=='bot_status' and str(raw.get('self_id'))==self.cfg.account and raw.get('sub_type') in ('online','offline'):
                    await self.post('notice',dict(connection_id=self.epoch,account_id=self.cfg.account,notice_type='bot_status',sub_type=raw['sub_type']))
                return
            body=envelope(raw,self.cfg)
            if body:
                await self.post('events',body)  # durable 202, NOT model generation
        except Exception:
            logger.warning('continuum_bridge code=ingress_unconfirmed')
            # No LLM, fallback answer, history or blind request replay.

    async def probe(self,bot):
        async with asyncio.timeout(5):
            version=await bot.call_action('get_version_info',self_id=int(self.cfg.account))
            login=await bot.call_action('get_login_info',self_id=int(self.cfg.account))
            status=await bot.call_action('get_status',self_id=int(self.cfg.account))
        if (version.get('app_name'),version.get('app_version'),version.get('protocol_version'),str(login.get('user_id')))!=('SnowLuma','2.2.1-node','v11',self.cfg.account):
            raise ValueError('Upstream version/identity mismatch')
        return bool(status.get('online') is True)

    async def deliver(self,bot,item):
        state='unknown';remote=None
        try:
            # No AstrBot reply decorations, segmentation, history or send wrapper.
            async with asyncio.timeout(8):
                result=await bot.call_action('send_private_msg',self_id=int(self.cfg.account),user_id=int(self.cfg.sender),
                                             message=item['segments'])
            if isinstance(result,dict) and type(result.get('message_id')) in (int,str):
                remote=str(result['message_id']);state='sent'
        except ActionFailed as exc:
            state='failed' if isinstance(getattr(exc,'retcode',None),int) and exc.retcode!=0 else 'unknown'
        except Exception:pass
        await self.post('receipt',dict(connection_id=self.epoch,echo=item['echo'],status=state,message_id=remote))

    async def pump(self):
        next_probe=0.
        while not self.closing:
            try:
                if not passive_only(self.context.get_config()):return
                platform=self.context.get_platform_inst(self.cfg.platform_id)
                if platform is None:
                    await asyncio.sleep(.5);continue
                bot=platform.get_client()
                now=asyncio.get_running_loop().time()
                if not self.epoch or now>=next_probe:
                    online=await self.probe(bot)
                    connected=await self.post('connect',dict(client_id=self.client_id,account_id=self.cfg.account,
                        sender_id=self.cfg.sender,platform_id=self.cfg.platform_id,app_name='SnowLuma',app_version='2.2.1-node',online=online))
                    self.epoch=connected['connection_id'];next_probe=now+10
                result=await self.post('poll',dict(connection_id=self.epoch))
                if result.get('delivery'):await self.deliver(bot,result['delivery'])
                await asyncio.sleep(.3)
            except asyncio.CancelledError:raise
            except Exception:
                logger.warning('continuum_bridge code=transport_unconfirmed')
                # A lost send receipt remains unknown in Continuum; never resend.
                await asyncio.sleep(1)
