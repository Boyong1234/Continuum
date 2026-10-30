import os
import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class OneBotConfig:
    enabled: bool = False
    bot_id: str = ''
    owner_id: str = ''
    canonical_user_id: str = 'local-user'
    token: str = field(default='', repr=False)
    admin_token: str = field(default='', repr=False)
    config_id: str = 'mock'
    expected_version: str = '4.18.41'
    aggregate_seconds: float = 1.0
    max_wait_seconds: float = 2.5
    max_batch_messages: int = 8
    max_batch_chars: int = 4000
    long_message_chars: int = 1200
    generation_timeout: float = 90.0
    heartbeat_timeout: float = 90.0
    handshake_timeout: float = 8.0
    receipt_timeout: float = 15.0
    runtime_lease_seconds: float = 10.0
    reply_bytes: int = 1500
    max_reply_parts: int = 24
    send_interval: float = 0.3
    max_pending_messages: int = 128
    max_frame_bytes: int = 262144

    def __post_init__(self):
        if self.canonical_user_id != 'local-user':
            raise ValueError('Only the configured local single user is supported')
        for value, minimum, maximum in [
            (self.aggregate_seconds, 0, 5), (self.max_wait_seconds, .05, 15),
            (self.max_batch_messages, 1, 32), (self.max_batch_chars, 100, 100000),
            (self.long_message_chars, 100, 100000), (self.generation_timeout, .05, 180),
            (self.heartbeat_timeout, .1, 300), (self.handshake_timeout, .1, 30),
            (self.receipt_timeout, .1, 120), (self.runtime_lease_seconds, 2, 30),
            (self.reply_bytes, 32, 4000), (self.max_reply_parts, 1, 64),
            (self.send_interval, 0, 10), (self.max_pending_messages, 1, 1000),
            (self.max_frame_bytes, 1024, 1048576),
        ]:
            if not minimum <= value <= maximum:
                raise ValueError('Channel setting outside allowed range')
        if self.max_wait_seconds < self.aggregate_seconds:
            raise ValueError('Maximum batch wait is shorter than idle wait')
        if self.enabled:
            if not all(re.fullmatch(r'[1-9][0-9]{4,19}', v) for v in (self.bot_id, self.owner_id)) or self.bot_id == self.owner_id:
                raise ValueError('Two distinct numeric QQ identities are required')
            if not all(re.fullmatch(r'[A-Za-z0-9_-]{32,256}', v) for v in (self.token, self.admin_token)) or self.token == self.admin_token:
                raise ValueError('Distinct strong channel and management credentials are required')
            if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', self.expected_version):
                raise ValueError('An explicit tested NapCat version is required')

    @classmethod
    def from_environment(cls):
        names = {
            'enabled': ('CONTINUUM_ONEBOT_ENABLED', lambda v: v.lower() == 'true'),
            'bot_id': ('CONTINUUM_ONEBOT_BOT_ID', str),
            'owner_id': ('CONTINUUM_ONEBOT_OWNER_ID', str),
            'token': ('CONTINUUM_ONEBOT_TOKEN', str),
            'admin_token': ('CONTINUUM_ONEBOT_ADMIN_TOKEN', str),
            'config_id': ('CONTINUUM_ONEBOT_MODEL_CONFIG', str),
            'expected_version': ('CONTINUUM_ONEBOT_NAPCAT_VERSION', str),
            'aggregate_seconds': ('CONTINUUM_ONEBOT_AGGREGATE_SECONDS', float),
            'max_wait_seconds': ('CONTINUUM_ONEBOT_MAX_WAIT_SECONDS', float),
            'generation_timeout': ('CONTINUUM_ONEBOT_GENERATION_TIMEOUT', float),
            'reply_bytes': ('CONTINUUM_ONEBOT_REPLY_BYTES', int),
        }
        try:
            return cls(**{key: cast(os.environ[name]) for key, (name, cast) in names.items() if name in os.environ})
        except (ValueError, TypeError):
            raise RuntimeError('Invalid OneBot configuration; private values omitted') from None
