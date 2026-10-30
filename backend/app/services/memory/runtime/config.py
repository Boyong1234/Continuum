import hashlib
import json
import os
from pydantic import BaseModel,ConfigDict,Field

CURRENT_POLICY='b2-evidence-v3-k1-contracts'


def encoded(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)


class MemoryConfig(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    provider: str=Field(default='mock',pattern=r'^(mock|deepseek)$')
    max_messages: int=Field(default=32,ge=2,le=80)
    max_segment_tokens: int=Field(default=6000,ge=128,le=20000)
    idle_seconds: int=Field(default=30,ge=0,le=3600)
    max_wait_seconds: int=Field(default=600,ge=1,le=86400)
    message_threshold: int=Field(default=12,ge=2,le=80)
    overlap_messages: int=Field(default=2,ge=0,le=8)
    daily_requests: int=Field(default=20,ge=0,le=10000)
    daily_tokens: int=Field(default=200000,ge=0,le=100000000)
    daily_microusd: int=Field(default=1000000,ge=0,le=100000000)
    unit_microusd: int=Field(default=4,ge=4,le=1000)
    max_concurrent: int=Field(default=1,ge=1,le=4)
    timeout_seconds: int=Field(default=45,ge=1,le=90)
    input_bytes: int=Field(default=24000,ge=12000,le=100000)
    output_tokens: int=Field(default=1536,ge=256,le=4096)
    extractor_version: str=Field(default='b2-v1',pattern=r'^[A-Za-z0-9_.-]{1,60}$')
    selective_sync: bool=False
    qq_only: bool=False

    @classmethod
    def from_environment(cls):
        mapping={'provider':'MEMORY_EXTRACT_PROVIDER','max_messages':'MEMORY_MAX_SEGMENT_MESSAGES',
                 'max_segment_tokens':'MEMORY_MAX_SEGMENT_TOKENS','idle_seconds':'MEMORY_IDLE_WINDOW_SECONDS',
                 'max_wait_seconds':'MEMORY_MAX_WAIT_SECONDS','daily_requests':'MEMORY_DAILY_REQUEST_BUDGET',
                 'daily_tokens':'MEMORY_DAILY_TOKEN_BUDGET','max_concurrent':'MEMORY_MAX_CONCURRENT_JOBS',
                 'timeout_seconds':'MEMORY_EXTRACTION_TIMEOUT','daily_microusd':'MEMORY_DAILY_COST_MICROUSD'}
        return cls(**{name:(os.environ[key] if name=='provider' else int(os.environ[key])) for name,key in mapping.items() if key in os.environ})


def strategy(config,provider):
    # Endpoint/model bound authorization, never a secret fingerprint.
    execution={name:value for name,value in config.model_dump().items() if name not in ('daily_requests','daily_tokens','daily_microusd','unit_microusd','max_concurrent','idle_seconds','max_wait_seconds','message_threshold')}
    value=dict(config=execution,model=provider.config.model,base_url=provider.config.base_url,provider=provider.config.provider,policy=CURRENT_POLICY,preflight='trivial-acks-v1')
    digest=hashlib.sha256(encoded(value).encode()).hexdigest()
    return digest,value


def accepts_source(role, metadata, config):
    if role not in ('user','assistant'):return False
    if not config.qq_only:return True
    if isinstance(metadata,str):metadata=json.loads(metadata)
    if metadata.get('channel')!='qq_onebot_v11' or metadata.get('origin') in ('historical_import','synthetic','forwarded') or metadata.get('source_kind') in ('historical_import','synthetic','forwarded') or metadata.get('synthetic') or metadata.get('is_test'):return False
    return True
