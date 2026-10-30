"""Registered transport profile: no quoted-message fetches or media expansion.

Extends the official reverse-WS adapter WITHOUT modifying AstrBot sources.
Only Plain components enter its generic preprocessor. Original minimal wire
segments are forwarded separately by Bridge, with the original message ID/time.
"""
from astrbot.core.platform.register import register_platform_adapter
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import AiocqhttpAdapter
from astrbot.api.platform import AstrBotMessage,MessageMember,MessageType
from astrbot.api.message_components import Plain
from .wire import BridgeSettings,envelope


@register_platform_adapter('continuum_onebot_gateway','Metadata-only OneBot v11 gateway')
class GatewayOneBotAdapter(AiocqhttpAdapter):
    async def convert_message(self,event):
        cfg=BridgeSettings.environment()
        normalized=envelope(event,cfg)
        notice=(event.get('post_type')=='notice' and event.get('notice_type')=='bot_status'
                and str(event.get('self_id'))==cfg.account and event.get('sub_type') in ('online','offline'))
        if normalized is None and not notice:return None
        message=AstrBotMessage()
        message.self_id=cfg.account
        message.sender=MessageMember(user_id=cfg.sender,nickname='gateway-user')
        message.type=MessageType.FRIEND_MESSAGE
        message.session_id=cfg.sender
        message.message_id=normalized['message_id'] if normalized else 'bot-status'
        message.timestamp=event.get('time',0)
        message.message_str=normalized['plain_text'] if normalized else '[OneBot status]'
        message.message=[Plain(text=message.message_str)]
        message.raw_message=event
        return message
