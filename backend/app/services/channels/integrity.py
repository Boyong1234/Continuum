import hashlib
import json
from datetime import datetime, timezone

from app.maintenance.validation import require, validate_utc, normalize_sql
from app.services.channels.protocol import fingerprint


def validate_channels(records, connection):
    runtime = records['channel_runtime']
    require(len(runtime) == 1, 'invalid_channel', 'Missing channel runtime')
    if runtime[0]['lease_expires_at']:
        validate_utc(runtime[0]['lease_expires_at'])
    bindings = {r['id']: r for r in records['channel_bindings']}
    messages = {r['id']: r for r in records['messages']}
    turns = {r['id']: r for r in records['channel_turns']}
    attempts = {r['request_id']: r for r in records['generation_attempts']}
    require(len(bindings) <= 1, 'invalid_channel', 'Only one QQ binding is supported')
    for b in bindings.values():
        require(b['id'] == 'qq-private' and b['canonical_user_id'] == 'local-user' and b['bot_id'].isdigit() and b['owner_id'].isdigit() and b['bot_id'] != b['owner_id'], 'invalid_channel', 'Invalid channel binding')
    sources = {}
    for source in records['channel_messages']:
        m = messages[source['canonical_message_id']]
        b = bindings[source['binding_id']]
        meta = json.loads(m['metadata'])
        require(m['role'] == 'user' and m['conversation_id'] == b['conversation_id'], 'invalid_channel', 'Source identity/role mismatch')
        require(meta.get('channel') == 'qq_onebot_v11' and meta.get('external_message_id') == source['external_message_id'] and meta.get('external_time') == source['external_time'] and meta.get('received_at') == source['received_at'] and meta.get('channel_turn_id') == source['turn_id'], 'invalid_channel', 'Channel provenance mismatch')
        created = datetime.fromtimestamp(source['external_time'], timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')
        require(m['created_at'] == created and source['payload_sha256'] == fingerprint([m['content'], source['external_time']]), 'invalid_channel', 'Original channel text/time changed')
        validate_utc(source['received_at'])
        if source['turn_id']:
            require(turns[source['turn_id']]['binding_id'] == source['binding_id'], 'invalid_channel', 'Turn identity mismatch')
            sources.setdefault(source['turn_id'], []).append(source['canonical_message_id'])
    # Sources have UUID PKs; restore actual append order for aggregated turns.
    for id in sources:
        sources[id] = [r[0] for r in connection.execute('SELECT canonical_message_id FROM channel_messages WHERE turn_id=? ORDER BY rowid', (id,))]
    for t in turns.values():
        require(bool(sources.get(t['id'])) and t['anchor_message_id'] == sources[t['id']][-1], 'invalid_channel', 'Turn has missing/incorrect original sources')
        for field in ('first_received_at', 'last_received_at', 'available_at'):
            validate_utc(t[field])
        if t['lease_expires_at']:
            validate_utc(t['lease_expires_at'])
        if t['state'] == 'completed':
            reply = messages[t['reply_message_id']]
            meta = json.loads(reply['metadata'])
            a = attempts.get(t['request_id'])
            require(reply['role'] == 'assistant' and reply['conversation_id'] == bindings[t['binding_id']]['conversation_id'] and meta.get('source_user_ids') == sources[t['id']] and meta.get('channel_turn_id') == t['id'], 'invalid_channel', 'Reply provenance mismatch')
            require(a is not None and a['status'] == 'completed' and a['result_message_id'] == reply['id'], 'invalid_channel', 'Completed turn has no generation receipt')
    groups = {}
    for d in records['channel_deliveries']:
        t = turns[d['turn_id']]
        m = messages[d['assistant_message_id']]
        require(t['reply_message_id'] == m['id'], 'invalid_channel', 'Delivery reply mismatch')
        start, end = d['start_offset'], d['end_offset']
        require(0 <= start < end <= len(m['content']), 'invalid_channel', 'Invalid delivery span')
        require(d['text_sha256'] == hashlib.sha256(m['content'][start:end].encode()).hexdigest(), 'invalid_channel', 'Delivery text hash mismatch')
        groups.setdefault(t['id'], []).append(d)
    for t in turns.values():
        if t['state'] != 'completed':
            continue
        parts = sorted(groups.get(t['id'], []), key=lambda d: d['part_index'])
        require(bool(parts) and [p['part_index'] for p in parts] == list(range(len(parts))) and parts[0]['start_offset'] == 0 and parts[-1]['end_offset'] == len(messages[t['reply_message_id']]['content']) and all(a['end_offset'] == b['start_offset'] for a, b in zip(parts, parts[1:])), 'invalid_channel', 'Delivery does not cover full reply')
    for c in records['channel_connections']:
        for field in ('connected_at', 'last_received_at', 'disconnected_at'):
            if c[field]:validate_utc(c[field])
    indexes = dict(connection.execute("SELECT name,sql FROM sqlite_master WHERE type='index' AND name LIKE 'channel_%'"))
    for name, sql in {
        'channel_one_open': "CREATE UNIQUE INDEX channel_one_open ON channel_turns(binding_id) WHERE state='open'",
        'channel_one_generating': "CREATE UNIQUE INDEX channel_one_generating ON channel_turns(binding_id) WHERE state='generating'",
    }.items():
        require(normalize_sql(indexes.get(name, '')) == normalize_sql(sql), 'invalid_channel', 'Missing channel concurrency constraint')
