import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy.dialects import sqlite
from sqlalchemy.schema import CreateTable

from app.models import Base

CORE_TABLES = ('conversations', 'messages', 'memories', 'memory_sources', 'memory_revisions')
PERSONA_FILES = ('core.md', 'preferences.yaml', 'examples.md')
FTS_TABLES = {'memory_fts', 'memory_fts_config', 'memory_fts_content', 'memory_fts_data', 'memory_fts_docsize', 'memory_fts_idx'}
TRIGGERS = {
    'messages_no_update': "CREATE TRIGGER messages_no_update BEFORE UPDATE ON messages BEGIN SELECT RAISE(ABORT, 'messages are append-only'); END",
    'messages_no_delete': "CREATE TRIGGER messages_no_delete BEFORE DELETE ON messages BEGIN SELECT RAISE(ABORT, 'messages are append-only'); END",
    'memory_fts_insert': 'CREATE TRIGGER memory_fts_insert AFTER INSERT ON memories BEGIN INSERT INTO memory_fts(id, content) VALUES (new.id,new.content); END',
    'memory_fts_update': 'CREATE TRIGGER memory_fts_update AFTER UPDATE ON memories BEGIN DELETE FROM memory_fts WHERE id=old.id; INSERT INTO memory_fts(id,content) VALUES(new.id,new.content); END',
}


class SafetyError(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def require(condition, code, message):
    if not condition:
        raise SafetyError(code, message)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def file_digest(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_sql(value):
    return re.sub(r'\s+', ' ', value.replace('"', '').strip().rstrip(';')).casefold()


def validate_utc(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        require(parsed.utcoffset() == timedelta(0), 'invalid_time', 'Non-UTC or missing timestamp timezone')
    except (ValueError, TypeError, AttributeError) as exc:
        raise SafetyError('invalid_time', 'Invalid UTC timestamp') from exc


@contextmanager
def read_database(path):
    path = Path(path).resolve()
    require(path.is_file(), 'missing_database', 'Database does not exist')
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=10)
    try:
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        yield connection
    except sqlite3.Error as exc:
        raise SafetyError('invalid_database', 'SQLite validation failed; private data omitted') from exc
    finally:
        connection.close()


def inspect_connection(connection):
    require(connection.execute('PRAGMA integrity_check').fetchall() == [('ok',)], 'integrity_error', 'SQLite integrity_check failed')
    require(not connection.execute('PRAGMA foreign_key_check').fetchall(), 'foreign_key_error', 'SQLite foreign_key_check failed')
    objects = {row[0]: row[1] for row in connection.execute("SELECT name,sql FROM sqlite_master WHERE type='table'")}
    require('schema_info' in objects, 'schema_version', 'Database has no supported schema version')
    versions = connection.execute('SELECT version FROM schema_info').fetchall()
    require(versions in ([(1,)], [(2,)], [(3,)], [(4,)], [(5,)], [(6,)], [(7,)]), 'schema_version', 'Unsupported or ambiguous database schema version')
    version = versions[0][0]
    expected = set(CORE_TABLES) | {'schema_info'} | FTS_TABLES
    if version >= 2:
        expected.add('migration_history')
    if version >= 3:
        expected.add('generation_attempts')
        from app.maintenance.revisions.v0003_generation_attempts import TABLE_SQL
        require(normalize_sql(objects.get('generation_attempts', '')) == normalize_sql(TABLE_SQL), 'schema_mismatch', 'Invalid generation attempt schema')
    if version >= 4:
        from app.maintenance.revisions.v0004_memory_foundation import TABLES
        expected.update(TABLES)
        for name, sql in TABLES.items():
            require(normalize_sql(objects.get(name, '')) == normalize_sql(sql), 'schema_mismatch', 'Invalid P2-A table schema')
    if version >= 5:
        from app.maintenance.revisions.v0005_memory_understanding import TABLES as B1_TABLES
        expected.update(B1_TABLES)
        for name, sql in B1_TABLES.items():
            require(normalize_sql(objects.get(name,'')) == normalize_sql(sql), 'schema_mismatch', 'Invalid B1 table schema')
    if version >= 6:
        from app.maintenance.revisions.v0006_memory_runtime import TABLES as B2_TABLES
        expected.update(B2_TABLES)
        for name, sql in B2_TABLES.items():
            require(normalize_sql(objects.get(name,'')) == normalize_sql(sql), 'schema_mismatch', 'Invalid B2 table schema')
    if version >= 7:
        from app.maintenance.revisions.v0007_qq_channel import TABLES as CHANNEL_TABLES
        expected.update(CHANNEL_TABLES)
        for name, sql in CHANNEL_TABLES.items():
            require(normalize_sql(objects.get(name,'')) == normalize_sql(sql), 'schema_mismatch', 'Invalid channel table schema')
    require(set(objects) == expected, 'schema_mismatch', 'Unexpected or missing database tables')
    for table in Base.metadata.sorted_tables:
        expected_sql = str(CreateTable(table).compile(dialect=sqlite.dialect()))
        require(normalize_sql(objects[table.name]) == normalize_sql(expected_sql), 'schema_mismatch', 'Core table schema does not match supported version')
    require(normalize_sql(objects['schema_info']) == normalize_sql('CREATE TABLE schema_info (version INTEGER NOT NULL)'), 'schema_mismatch', 'Invalid version table schema')
    fts_sql = 'CREATE VIRTUAL TABLE memory_fts USING fts5(id UNINDEXED, content)'
    require(normalize_sql(objects['memory_fts']) == normalize_sql(fts_sql), 'schema_mismatch', 'Invalid FTS schema')
    triggers = {r[0]: r[1] for r in connection.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'")}
    expected_triggers = dict(TRIGGERS)
    if version >= 4:
        from app.maintenance.revisions.v0004_memory_foundation import TRIGGERS as MEMORY_TRIGGERS
        expected_triggers.update(MEMORY_TRIGGERS)
    if version >= 5:
        from app.maintenance.revisions.v0005_memory_understanding import TRIGGERS as B1_TRIGGERS
        expected_triggers.update(B1_TRIGGERS)
    if version >= 6:
        from app.maintenance.revisions.v0006_memory_runtime import TRIGGERS as B2_TRIGGERS
        expected_triggers.update(B2_TRIGGERS)
    if version >= 7:
        from app.maintenance.revisions.v0007_qq_channel import TRIGGERS as CHANNEL_TRIGGERS
        expected_triggers.update(CHANNEL_TRIGGERS)
    require(set(triggers) == set(expected_triggers), 'schema_mismatch', 'Unexpected or missing database triggers')
    for name, sql in expected_triggers.items():
        require(normalize_sql(triggers[name]) == normalize_sql(sql), 'schema_mismatch', 'Database trigger definition mismatch')
    if version >= 2:
        from app.maintenance.migrations import validate_history
        validate_history(connection)

    entities = {}
    records = {}
    orders = {'memory_sources': 'memory_id,message_id'}
    extra = ()
    if version >= 4:
        from app.maintenance.revisions.v0004_memory_foundation import TABLE_ORDERS
        orders.update(TABLE_ORDERS)
        extra = tuple(TABLE_ORDERS)
    if version >= 5:
        from app.maintenance.revisions.v0005_memory_understanding import TABLE_ORDERS as B1_ORDERS
        orders.update(B1_ORDERS)
        extra += tuple(B1_ORDERS)
    if version >= 6:
        from app.maintenance.revisions.v0006_memory_runtime import TABLE_ORDERS as B2_ORDERS
        orders.update(B2_ORDERS)
        extra += tuple(B2_ORDERS)
    if version >= 7:
        from app.maintenance.revisions.v0007_qq_channel import TABLE_ORDERS as CHANNEL_ORDERS
        orders.update(CHANNEL_ORDERS)
        extra += tuple(CHANNEL_ORDERS)
    for table in CORE_TABLES + (('generation_attempts',) if version >= 3 else ()) + extra:
        order = orders.get(table, 'id')
        cursor = connection.execute(f'SELECT * FROM {table} ORDER BY {order}')
        columns = [item[0] for item in cursor.description]
        rows = cursor.fetchall()
        entities[table] = {'count': len(rows), 'sha256': sha256(json.dumps(rows, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode())}
        records[table] = [dict(zip(columns, row)) for row in rows]
        for row in records[table]:
            for column in ('created_at', 'updated_at'):
                if column in row:
                    validate_utc(row[column])
    messages = {m['id']: m for m in records['messages']}
    for attempt in records.get('generation_attempts', []):
        user = messages.get(attempt['user_message_id'])
        require(user is not None and user['role'] == 'user' and user['conversation_id'] == attempt['conversation_id'], 'invalid_attempt', 'Invalid generation source')
        metadata = json.loads(attempt['metadata'])
        require(isinstance(metadata, dict), 'invalid_attempt', 'Invalid generation metadata')
        if attempt['status'] == 'completed':
            reply = messages.get(attempt['result_message_id'])
            require(reply is not None and reply['role'] == 'assistant' and reply['conversation_id'] == attempt['conversation_id'] and json.loads(reply['metadata']).get('reply_to') == user['id'], 'invalid_attempt', 'Invalid generation result')
        else:
            require(attempt['result_message_id'] is None, 'invalid_attempt', 'Uncompleted generation has a result')
    memories = {m['id']: m for m in records['memories']}
    sources = {}
    for source in records['memory_sources']:
        sources.setdefault(source['memory_id'], []).append(source['message_id'])
    for memory in memories.values():
        ids = sources.get(memory['id'], [])
        require(bool(ids) and all(id in messages for id in ids), 'invalid_sources', 'Memory is missing original message sources')
        if memory['memory_type'] in ('user_statement', 'user_confirmed'):
            require(any(messages[id]['role'] == 'user' for id in ids), 'invalid_sources', 'User fact has no user message source')
    for message in messages.values():
        try:
            metadata = json.loads(message['metadata'])
            require(isinstance(metadata, dict), 'invalid_metadata', 'Invalid message metadata')
            if 'reply_to' in metadata:
                parent = messages.get(metadata['reply_to'])
                require(parent is not None and parent['role'] == 'user' and parent['conversation_id'] == message['conversation_id'], 'invalid_sources', 'Invalid reply source association')
        except (ValueError, TypeError) as exc:
            raise SafetyError('invalid_metadata', 'Invalid message metadata') from exc
    for revision in records['memory_revisions']:
        try:
            snapshot = json.loads(revision['snapshot'])
            require(isinstance(snapshot, dict) and snapshot.get('id') == revision['memory_id'], 'invalid_revision', 'Revision identity mismatch')
            ids = snapshot.get('source_message_ids')
            require(isinstance(ids, list) and bool(ids) and all(id in messages for id in ids), 'invalid_revision', 'Revision has missing original sources')
            for key in ('created_at', 'updated_at'):
                validate_utc(snapshot.get(key))
        except (ValueError, TypeError) as exc:
            raise SafetyError('invalid_revision', 'Invalid revision snapshot') from exc
    if version >= 4:
        from app.services.memory.integrity import validate_foundation
        try:
            validate_foundation(records)
        except (ValueError, TypeError, KeyError, IndexError):
            raise SafetyError('invalid_foundation', 'Invalid memory/job data; private values omitted') from None
    if version >= 5:
        from app.services.memory.understanding.integrity import validate_understanding
        try:
            validate_understanding(records)
        except (ValueError, TypeError, KeyError, IndexError):
            raise SafetyError('invalid_understanding', 'Invalid episode/evidence data; private values omitted') from None
    if version >= 6:
        from app.services.memory.runtime.integrity import validate_runtime
        try:
            validate_runtime(records,connection)
        except (ValueError,TypeError,KeyError,IndexError):
            raise SafetyError('invalid_memory_runtime','Invalid scheduler/budget state; private values omitted') from None
    if version >= 7:
        from app.services.channels.integrity import validate_channels
        try:
            validate_channels(records,connection)
        except (ValueError,TypeError,KeyError,IndexError):
            raise SafetyError('invalid_channel','Invalid channel state; private values omitted') from None
    content = connection.execute('SELECT id,content FROM memories ORDER BY id').fetchall()
    indexed = connection.execute('SELECT id,content FROM memory_fts ORDER BY id').fetchall()
    require(content == indexed, 'fts_mismatch', 'FTS content does not match original memories')
    return {'database_schema_version': version, 'entities': entities}


def inspect_database(path):
    with read_database(path) as connection:
        try:
            return inspect_connection(connection)
        except (ValueError, TypeError, KeyError) as exc:
            raise SafetyError('invalid_data', 'Unsupported database data; private values omitted') from exc
