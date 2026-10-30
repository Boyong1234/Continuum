"""NapCat v11 text subset. Never evaluate CQ strings or fetch attachments."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class PrivateText:
    external_id: str
    content: str
    external_time: int
    created_at: str
    payload_sha256: str
    # Transport provenance only; never trusted as identity or model instructions.
    source_system: str = 'napcat'
    transport: str | None = None
    segments: tuple = field(default_factory=tuple)


def parse_private(event, config):
    if not isinstance(event, dict) or event.get('post_type') != 'message':
        return None
    if (event.get('message_type') != 'private' or event.get('sub_type') != 'friend'
        or str(event.get('self_id')) != config.bot_id or str(event.get('user_id')) != config.owner_id):
        return None
    sender = event.get('sender', {})
    if not isinstance(sender, dict) or ('user_id' in sender and str(sender['user_id']) != config.owner_id):
        return None
    mid = event.get('message_id')
    timestamp = event.get('time')
    segments = event.get('message')
    if type(mid) not in (str, int) or not str(mid) or len(str(mid)) > 128:
        return None
    if type(timestamp) is not int or not 0 <= timestamp <= 253402300799:
        return None
    if not isinstance(segments, list) or not 1 <= len(segments) <= 100:
        return None
    if any(not isinstance(s, dict) or s.get('type') != 'text' or not isinstance(s.get('data'), dict)
           or not isinstance(s['data'].get('text'), str) for s in segments):
        return None
    content = ''.join(s['data']['text'] for s in segments)
    if not content.strip() or len(content) > 100000 or '\x00' in content:
        return None
    try:
        content.encode('utf-8')
        created = datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')
        sha = fingerprint([content, timestamp])
    except (UnicodeError, ValueError, OverflowError, OSError):
        return None
    return PrivateText(str(mid), content, timestamp, created, sha)


def split_reply(content, byte_limit):
    """Offsets into the ORIGINAL reply; no text copied to the delivery table."""
    start = 0
    used = 0
    for index, char in enumerate(content):
        cost = len(char.encode('utf-8'))
        if used + cost > byte_limit:
            yield start, index
            start, used = index, 0
        used += cost
    if start < len(content):
        yield start, len(content)
