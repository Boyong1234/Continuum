"""OneBot wire normalization only. No Continuum internals or history."""
from dataclasses import dataclass, field
import os
import re
from urllib.parse import urlsplit


@dataclass(frozen=True)
class BridgeSettings:
    url: str
    token: str = field(repr=False)
    account: str
    sender: str
    platform_id: str = 'continuum-onebot'

    def __post_init__(self):
        parsed=urlsplit(self.url)
        if parsed.scheme!='http' or parsed.hostname!='127.0.0.1' or parsed.path not in ('','/') or parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError('Only explicit local backend URLs are allowed')
        if not re.fullmatch(r'[A-Za-z0-9_-]{32,256}',self.token):raise ValueError('Strong bridge token required')
        if not all(re.fullmatch(r'[1-9][0-9]{4,19}',v) for v in (self.account,self.sender)) or self.account==self.sender:
            raise ValueError('Two configured distinct identities required')

    @classmethod
    def environment(cls):
        return cls(os.environ['CONTINUUM_BRIDGE_URL'],os.environ['CONTINUUM_BRIDGE_TOKEN'],
                   os.environ['CONTINUUM_GATEWAY_ACCOUNT_ID'],os.environ['CONTINUUM_GATEWAY_SENDER_ID'],
                   os.getenv('ASTRBOT_BRIDGE_PLATFORM_ID','continuum-onebot'))


def passive_only(config):
    """Fail closed; stop_event alone cannot prevent other enabled AI paths."""
    return (config.get('provider_settings',{}).get('enable') is False
        and not config.get('provider') and not config.get('provider_sources')
        and not config.get('provider_ltm_settings',{}).get('group_icl_enable',False)
        and not config.get('provider_ltm_settings',{}).get('active_reply',{}).get('enable',False)
        and not config.get('provider_ltm_settings',{}).get('group_message_history_enable',False)
        and not config.get('provider_ltm_settings',{}).get('image_caption',False)
        and not config.get('kb_names') and not config.get('subagent_orchestrator',{}).get('main_enable',False)
        and not config.get('trace_enable',False) and not config.get('provider_stt_settings',{}).get('enable',False)
        and not config.get('provider_tts_settings',{}).get('enable',False))


def envelope(raw,cfg):
    if not isinstance(raw,dict) or (raw.get('post_type'),raw.get('message_type'),raw.get('sub_type'),str(raw.get('self_id')),str(raw.get('user_id'))) != ('message','private','friend',cfg.account,cfg.sender):
        return None
    sender=raw.get('sender',{})
    if not isinstance(sender,dict) or sender.get('user_id') is not None and str(sender['user_id'])!=cfg.sender:return None
    mid=raw.get('message_id');timestamp=raw.get('time');segments=raw.get('message')
    if type(mid) not in (int,str) or not 0<len(str(mid))<=128 or type(timestamp) is not int or not isinstance(segments,list) or not 1<=len(segments)<=100:return None
    result=[];parts=[]
    for segment in segments:
        if not isinstance(segment,dict) or not isinstance(segment.get('data'),dict):return None
        kind,data=segment.get('type'),segment['data']
        if kind=='text' and isinstance(data.get('text'),str):
            normalized={'text':data['text']};parts.append(data['text'])
        elif kind in ('reply','image','forward'):
            normalized={k:v for k,v in data.items() if k in ('id','width','height','sub_type') and type(v) in (int,str) and len(str(v))<=128}
            parts.append({'reply':'[引用消息，未展开]','image':'[图片，暂未解析]','forward':'[转发材料，未展开]'}[kind])
        else:return None
        result.append(dict(type=kind,data=normalized))
    content=''.join(parts)
    if not content.strip() or len(content)>100000:return None
    return dict(channel='qq',transport='astrbot_onebot_v11',platform='qq',account_id=cfg.account,
                conversation_type='private',conversation_id=cfg.sender,sender_id=cfg.sender,
                message_id=str(mid),timestamp=timestamp,segments=result,plain_text=content,raw_event_id=str(mid))
