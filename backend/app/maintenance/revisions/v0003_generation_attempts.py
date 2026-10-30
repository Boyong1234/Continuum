"""Add generation bookkeeping without changing append-only messages."""
REVISION = '0003_generation_attempts'
TABLE_SQL = """CREATE TABLE generation_attempts (
 id TEXT PRIMARY KEY,
 conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE RESTRICT,
 user_message_id TEXT NOT NULL UNIQUE REFERENCES messages(id) ON DELETE RESTRICT,
 request_id TEXT UNIQUE,
 config_id TEXT NOT NULL,
 provider TEXT NOT NULL,
 requested_model TEXT,
 model TEXT,
 status TEXT NOT NULL CHECK(status IN ('running','completed','failed','interrupted','cancelled','truncated')),
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 result_message_id TEXT REFERENCES messages(id) ON DELETE RESTRICT,
 metadata TEXT NOT NULL
)"""


def apply(connection):
    connection.execute(TABLE_SQL)
