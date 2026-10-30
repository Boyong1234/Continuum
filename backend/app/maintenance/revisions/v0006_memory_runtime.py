"""B2 additive scheduler/control/budget state. Released v2–v5 are untouched."""
from app.models import utcnow

REVISION='0006_memory_runtime'
TABLES={
 'memory_runtime': """CREATE TABLE memory_runtime (
 id INTEGER PRIMARY KEY CHECK(id=1),
 enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
 paused INTEGER NOT NULL CHECK(paused IN (0,1)),
 initialized INTEGER NOT NULL CHECK(initialized IN (0,1)),
 start_rowid INTEGER NOT NULL CHECK(start_rowid>=0),
 config_json TEXT NOT NULL,
 config_hash TEXT,
 authorized_hash TEXT,
 updated_at TEXT NOT NULL
)""",
 'memory_schedule_cursor': """CREATE TABLE memory_schedule_cursor (
 id INTEGER PRIMARY KEY CHECK(id=1),
 last_rowid INTEGER NOT NULL CHECK(last_rowid>=0)
)""",
 'memory_conversation_dirty': """CREATE TABLE memory_conversation_dirty (
 conversation_id TEXT PRIMARY KEY REFERENCES conversations(id) ON DELETE RESTRICT,
 updated_at TEXT NOT NULL
)""",
 'memory_segments': """CREATE TABLE memory_segments (
 id TEXT PRIMARY KEY,
 conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE RESTRICT,
 sequence INTEGER NOT NULL CHECK(sequence>=1),
 state TEXT NOT NULL CHECK(state IN ('open','sealed','queued','processed','blocked')),
 config_hash TEXT NOT NULL,
 config_json TEXT NOT NULL,
 job_id TEXT UNIQUE REFERENCES memory_extraction_jobs(id) ON DELETE RESTRICT,
 created_at TEXT NOT NULL,
 last_arrived_at TEXT NOT NULL,
 sealed_at TEXT,
 seal_reason TEXT,
 error_code TEXT,
 UNIQUE(conversation_id,sequence),
 CHECK(state NOT IN ('queued','processed') OR job_id IS NOT NULL)
)""",
 'memory_segment_sources': """CREATE TABLE memory_segment_sources (
 segment_id TEXT NOT NULL REFERENCES memory_segments(id) ON DELETE RESTRICT,
 message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE RESTRICT,
 arrival_rowid INTEGER NOT NULL CHECK(arrival_rowid>0),
 position INTEGER NOT NULL CHECK(position>=0),
 is_primary INTEGER NOT NULL CHECK(is_primary IN (0,1)),
 PRIMARY KEY(segment_id,message_id),
 UNIQUE(segment_id,position)
)""",
 'memory_budget_reservations': """CREATE TABLE memory_budget_reservations (
 id TEXT PRIMARY KEY,
 day TEXT NOT NULL,
 job_id TEXT NOT NULL REFERENCES memory_extraction_jobs(id) ON DELETE RESTRICT,
 attempt_id TEXT NOT NULL UNIQUE REFERENCES memory_extraction_attempts(id) ON DELETE RESTRICT,
 config_hash TEXT NOT NULL,
 provider TEXT NOT NULL,
 reserved_tokens INTEGER NOT NULL CHECK(reserved_tokens>0),
 actual_tokens INTEGER CHECK(actual_tokens>=0),
 unit_microusd INTEGER NOT NULL CHECK(unit_microusd>=0),
 state TEXT NOT NULL CHECK(state IN ('reserved','settled','unknown')),
 usage_json TEXT NOT NULL,
 created_at TEXT NOT NULL,
 finished_at TEXT,
 CHECK((state='settled' AND actual_tokens IS NOT NULL) OR (state<>'settled' AND actual_tokens IS NULL))
)""",
 'memory_extraction_payloads': """CREATE TABLE memory_extraction_payloads (
 attempt_id TEXT PRIMARY KEY REFERENCES memory_extraction_attempts(id) ON DELETE RESTRICT,
 raw_json TEXT NOT NULL,
 normalized_json TEXT NOT NULL,
 repairs_json TEXT NOT NULL,
 created_at TEXT NOT NULL
)""",
 'memory_suppressions': """CREATE TABLE memory_suppressions (
 kind TEXT NOT NULL CHECK(kind IN ('semantic','episodic')),
 target_id TEXT NOT NULL,
 created_at TEXT NOT NULL,
 PRIMARY KEY(kind,target_id)
)""",
 'memory_exclusions': """CREATE TABLE memory_exclusions (
 message_id TEXT PRIMARY KEY REFERENCES messages(id) ON DELETE RESTRICT,
 created_at TEXT NOT NULL
)""",
 'memory_segment_job_history': """CREATE TABLE memory_segment_job_history (
 job_id TEXT PRIMARY KEY REFERENCES memory_extraction_jobs(id) ON DELETE RESTRICT,
 segment_id TEXT NOT NULL REFERENCES memory_segments(id) ON DELETE RESTRICT,
 config_hash TEXT NOT NULL,
 config_json TEXT NOT NULL,
 created_at TEXT NOT NULL
)""",
 'memory_release_gate': """CREATE TABLE memory_release_gate (
 id INTEGER PRIMARY KEY CHECK(id=1),
 status TEXT NOT NULL CHECK(status IN ('pending','passed','blocked')),
 report_sha256 TEXT,
 policy_version TEXT NOT NULL,
 updated_at TEXT NOT NULL
)""",
}
TABLE_ORDERS={name: ('segment_id,position' if name=='memory_segment_sources' else 'kind,target_id' if name=='memory_suppressions' else 'conversation_id' if name=='memory_conversation_dirty' else 'attempt_id' if name=='memory_extraction_payloads' else 'message_id' if name=='memory_exclusions' else 'job_id' if name=='memory_segment_job_history' else 'id') for name in TABLES}
TRIGGERS={
 'memory_budget_no_delete': "CREATE TRIGGER memory_budget_no_delete BEFORE DELETE ON memory_budget_reservations BEGIN SELECT RAISE(ABORT, 'budget receipts cannot be deleted'); END",
 'memory_payloads_no_delete': "CREATE TRIGGER memory_payloads_no_delete BEFORE DELETE ON memory_extraction_payloads BEGIN SELECT RAISE(ABORT, 'extraction payloads are audit data'); END",
 'memory_payloads_no_update': "CREATE TRIGGER memory_payloads_no_update BEFORE UPDATE ON memory_extraction_payloads BEGIN SELECT RAISE(ABORT, 'extraction payloads are audit data'); END",
}


def apply(connection):
    for sql in TABLES.values():connection.execute(sql)
    for sql in TRIGGERS.values():connection.execute(sql)
    connection.execute('CREATE UNIQUE INDEX memory_primary_source ON memory_segment_sources(message_id) WHERE is_primary=1')
    connection.execute("CREATE UNIQUE INDEX memory_one_open_segment ON memory_segments(conversation_id) WHERE state='open'")
    connection.execute('CREATE INDEX memory_budget_day ON memory_budget_reservations(day)')
    connection.execute("INSERT INTO memory_runtime VALUES (1,0,1,0,0,'{}',NULL,NULL,?)",(utcnow(),))
    connection.execute('INSERT INTO memory_schedule_cursor VALUES (1,0)')
    connection.execute("INSERT INTO memory_release_gate VALUES (1,'pending',NULL,'b2-evidence-v1',?)",(utcnow(),))
