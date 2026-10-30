"""Additive channel transport state; original Message is the sole chat record."""

REVISION = '0007_qq_channel'
TABLES = {
    'channel_runtime': '''CREATE TABLE channel_runtime (
 id INTEGER PRIMARY KEY CHECK(id=1),
 owner_token TEXT,
 lease_expires_at TEXT,
 paused INTEGER NOT NULL CHECK(paused IN (0,1)),
 CHECK((owner_token IS NULL)=(lease_expires_at IS NULL))
)''',
    'channel_bindings': '''CREATE TABLE channel_bindings (
 id TEXT PRIMARY KEY,
 canonical_user_id TEXT NOT NULL CHECK(canonical_user_id='local-user'),
 bot_id TEXT NOT NULL,
 owner_id TEXT NOT NULL,
 conversation_id TEXT NOT NULL UNIQUE REFERENCES conversations(id) ON DELETE RESTRICT,
 created_at TEXT NOT NULL,
 CHECK(bot_id<>owner_id)
)''',
    'channel_turns': '''CREATE TABLE channel_turns (
 id TEXT PRIMARY KEY,
 binding_id TEXT NOT NULL REFERENCES channel_bindings(id) ON DELETE RESTRICT,
 sequence INTEGER NOT NULL CHECK(sequence>0),
 state TEXT NOT NULL CHECK(state IN ('open','queued','generating','completed','failed','cancelled','interrupted')),
 config_id TEXT NOT NULL,
 request_id TEXT NOT NULL UNIQUE,
 first_received_at TEXT NOT NULL,
 last_received_at TEXT NOT NULL,
 available_at TEXT NOT NULL,
 anchor_message_id TEXT REFERENCES messages(id) ON DELETE RESTRICT,
 reply_message_id TEXT UNIQUE REFERENCES messages(id) ON DELETE RESTRICT,
 lease_token TEXT,
 lease_expires_at TEXT,
 cancel_requested INTEGER NOT NULL CHECK(cancel_requested IN (0,1)),
 error_code TEXT,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 UNIQUE(binding_id,sequence),
 CHECK((state='completed')=(reply_message_id IS NOT NULL)),
 CHECK(state<>'generating' OR (lease_token IS NOT NULL AND lease_expires_at IS NOT NULL))
)''',
    'channel_messages': '''CREATE TABLE channel_messages (
 id TEXT PRIMARY KEY,
 binding_id TEXT NOT NULL REFERENCES channel_bindings(id) ON DELETE RESTRICT,
 external_message_id TEXT NOT NULL,
 payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64),
 canonical_message_id TEXT NOT NULL UNIQUE REFERENCES messages(id) ON DELETE RESTRICT,
 turn_id TEXT REFERENCES channel_turns(id) ON DELETE RESTRICT,
 external_time INTEGER NOT NULL CHECK(external_time>=0),
 received_at TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('text','control')),
 UNIQUE(binding_id,external_message_id),
 CHECK((kind='control')=(turn_id IS NULL))
)''',
    'channel_deliveries': '''CREATE TABLE channel_deliveries (
 id TEXT PRIMARY KEY,
 turn_id TEXT NOT NULL REFERENCES channel_turns(id) ON DELETE RESTRICT,
 assistant_message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE RESTRICT,
 part_index INTEGER NOT NULL CHECK(part_index>=0),
 start_offset INTEGER NOT NULL CHECK(start_offset>=0),
 end_offset INTEGER NOT NULL CHECK(end_offset>start_offset),
 text_sha256 TEXT NOT NULL CHECK(length(text_sha256)=64),
 state TEXT NOT NULL CHECK(state IN ('pending','sending','sent','failed','unknown','blocked','cancelled')),
 echo TEXT NOT NULL UNIQUE,
 connection_id TEXT REFERENCES channel_connections(id) ON DELETE RESTRICT,
 remote_message_id TEXT,
 error_code TEXT,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 UNIQUE(turn_id,part_index),
 CHECK(state<>'sent' OR remote_message_id IS NOT NULL),
 CHECK(state NOT IN ('sending','sent','unknown') OR connection_id IS NOT NULL)
)''',
    'channel_connections': '''CREATE TABLE channel_connections (
 id TEXT PRIMARY KEY,
 state TEXT NOT NULL CHECK(state IN ('connected','closed','timed_out','restarted','rejected')),
 app_name TEXT,
 app_version TEXT,
 connected_at TEXT NOT NULL,
 last_received_at TEXT NOT NULL,
 disconnected_at TEXT,
 error_code TEXT
)''',
    'channel_collisions': '''CREATE TABLE channel_collisions (
 id TEXT PRIMARY KEY,
 source_id TEXT NOT NULL REFERENCES channel_messages(id) ON DELETE RESTRICT,
 conflicting_sha256 TEXT NOT NULL CHECK(length(conflicting_sha256)=64),
 created_at TEXT NOT NULL
)''',
}
TABLE_ORDERS = {name: 'id' for name in TABLES}
TRIGGERS = {}
for table in ('channel_bindings', 'channel_messages', 'channel_collisions'):
    for operation in ('UPDATE', 'DELETE'):
        name = f'{table}_no_{operation.lower()}'
        TRIGGERS[name] = f"CREATE TRIGGER {name} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT, 'channel sources are immutable'); END"


def apply(connection):
    for sql in TABLES.values():
        connection.execute(sql)
    for sql in TRIGGERS.values():
        connection.execute(sql)
    connection.execute("CREATE UNIQUE INDEX channel_one_open ON channel_turns(binding_id) WHERE state='open'")
    connection.execute("CREATE UNIQUE INDEX channel_one_generating ON channel_turns(binding_id) WHERE state='generating'")
    connection.execute('CREATE INDEX channel_delivery_state ON channel_deliveries(state,created_at)')
    connection.execute('INSERT INTO channel_runtime VALUES (1,NULL,NULL,0)')
