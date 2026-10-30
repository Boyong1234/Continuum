"""P2-A additive lifecycle and durable job foundation. No model calls."""
import json
from datetime import datetime, timezone
from uuid import uuid5, NAMESPACE_URL

REVISION = '0004_memory_foundation'
TABLES = {
    'memory_lifecycle': """CREATE TABLE memory_lifecycle (
 memory_id TEXT PRIMARY KEY REFERENCES memories(id) ON DELETE RESTRICT,
 state TEXT NOT NULL CHECK(state IN ('tentative','active','superseded','archived')),
 evidence_level TEXT NOT NULL CHECK(evidence_level IN ('user_explicit','user_confirmed','model_inferred')),
 topic_key TEXT,
 valid_from TEXT,
 valid_to TEXT,
 superseded_by TEXT REFERENCES memories(id) ON DELETE RESTRICT,
 version INTEGER NOT NULL CHECK(version >= 1),
 CHECK(state <> 'active' OR evidence_level <> 'model_inferred'),
 CHECK(state <> 'superseded' OR superseded_by IS NOT NULL),
 CHECK(superseded_by IS NULL OR superseded_by <> memory_id)
)""",
    'memory_revision_audit': """CREATE TABLE memory_revision_audit (
 revision_id TEXT PRIMARY KEY REFERENCES memory_revisions(id) ON DELETE RESTRICT,
 memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE RESTRICT,
 revision_number INTEGER NOT NULL CHECK(revision_number >= 1),
 change_reason TEXT NOT NULL,
 UNIQUE(memory_id,revision_number)
)""",
    'memory_extraction_jobs': """CREATE TABLE memory_extraction_jobs (
 id TEXT PRIMARY KEY,
 source_fingerprint TEXT NOT NULL,
 extractor_version TEXT NOT NULL,
 config_id TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('queued','leased','retry','succeeded','dead','alignment_required')),
 attempts INTEGER NOT NULL DEFAULT 0,
 max_attempts INTEGER NOT NULL CHECK(max_attempts BETWEEN 1 AND 5),
 next_retry_at TEXT,
 lease_token TEXT UNIQUE,
 lease_owner TEXT,
 lease_expires_at TEXT,
 last_error TEXT,
 result_json TEXT,
 result_sha256 TEXT,
 result_attempt_id TEXT REFERENCES memory_extraction_attempts(id) ON DELETE RESTRICT,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 UNIQUE(source_fingerprint,extractor_version),
 CHECK(attempts >= 0 AND attempts <= max_attempts),
 CHECK((status = 'leased' AND lease_token IS NOT NULL AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) OR (status <> 'leased' AND lease_token IS NULL AND lease_owner IS NULL AND lease_expires_at IS NULL)),
 CHECK((result_json IS NULL AND result_sha256 IS NULL AND result_attempt_id IS NULL) OR (result_json IS NOT NULL AND result_sha256 IS NOT NULL AND result_attempt_id IS NOT NULL)),
 CHECK(status <> 'succeeded' OR result_json IS NOT NULL)
)""",
    'memory_extraction_job_sources': """CREATE TABLE memory_extraction_job_sources (
 job_id TEXT NOT NULL REFERENCES memory_extraction_jobs(id) ON DELETE RESTRICT,
 message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE RESTRICT,
 position INTEGER NOT NULL CHECK(position >= 0),
 PRIMARY KEY(job_id,message_id),
 UNIQUE(job_id,position)
)""",
    'memory_extraction_attempts': """CREATE TABLE memory_extraction_attempts (
 id TEXT PRIMARY KEY,
 job_id TEXT NOT NULL REFERENCES memory_extraction_jobs(id) ON DELETE RESTRICT,
 attempt_number INTEGER NOT NULL CHECK(attempt_number >= 1),
 lease_token TEXT NOT NULL UNIQUE,
 lease_owner TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('leased','api_succeeded','succeeded','failed','expired')),
 provider TEXT,
 model TEXT,
 usage_json TEXT NOT NULL,
 error_code TEXT,
 started_at TEXT NOT NULL,
 finished_at TEXT,
 UNIQUE(job_id,attempt_number)
)""",
}
TABLE_ORDERS = {'memory_lifecycle': 'memory_id', 'memory_revision_audit': 'revision_id',
                'memory_extraction_jobs': 'id', 'memory_extraction_job_sources': 'job_id,position',
                'memory_extraction_attempts': 'id'}
TRIGGERS = {
    f'{table}_no_{action.lower()}': f"CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT, 'memory provenance is append-only'); END"
    for table in ('memory_revisions', 'memory_revision_audit', 'memory_sources')
    for action in ('UPDATE', 'DELETE')
}


def mapped_state(memory_type, legacy_status):
    evidence = {'user_statement': 'user_explicit', 'user_confirmed': 'user_confirmed'}.get(memory_type, 'model_inferred')
    state = 'archived' if legacy_status == 'archived' or memory_type == 'outdated' else 'tentative' if evidence == 'model_inferred' else 'active'
    return state, evidence


def baseline_id(memory_id):
    return str(uuid5(NAMESPACE_URL, 'continuum:v4:memory-baseline:' + memory_id))


def apply(connection):
    for sql in TABLES.values():
        connection.execute(sql)
    connection.execute('CREATE INDEX memory_jobs_ready ON memory_extraction_jobs(status,next_retry_at,created_at)')
    for sql in TRIGGERS.values():
        connection.execute(sql)
    cursor = connection.execute('SELECT * FROM memories ORDER BY id')
    names = [column[0] for column in cursor.description]
    now = datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')
    for row in cursor.fetchall():
        memory = dict(zip(names, row))
        mid = memory['id']
        old = connection.execute('SELECT id FROM memory_revisions WHERE memory_id=? ORDER BY created_at,id', (mid,)).fetchall()
        for number, (rid,) in enumerate(old, 1):
            connection.execute('INSERT INTO memory_revision_audit VALUES (?,?,?,?)', (rid, mid, number, 'legacy_revision'))
        version = len(old) + 1
        state, evidence = mapped_state(memory['memory_type'], memory['status'])
        connection.execute('INSERT INTO memory_lifecycle VALUES (?,?,?,?,?,?,?,?)', (mid, state, evidence, None, None, None, None, version))
        sources = connection.execute('SELECT ms.message_id,m.role FROM memory_sources ms JOIN messages m ON m.id=ms.message_id WHERE ms.memory_id=? ORDER BY ms.message_id', (mid,)).fetchall()
        snapshot = {**memory, 'legacy_status': memory['status'], 'status': state, 'evidence_level': evidence,
                    'topic_key': None, 'valid_from': None, 'valid_to': None, 'superseded_by': None, 'version': version,
                    'source_message_ids': [item[0] for item in sources],
                    'sources': [{'message_id': item[0], 'source_role': item[1]} for item in sources]}
        rid = baseline_id(mid)
        connection.execute('INSERT INTO memory_revisions VALUES (?,?,?,?)', (rid, mid, now, json.dumps(snapshot, ensure_ascii=False)))
        connection.execute('INSERT INTO memory_revision_audit VALUES (?,?,?,?)', (rid, mid, version, 'migration_baseline'))
