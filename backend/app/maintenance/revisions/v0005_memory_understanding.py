"""Additive B1 evidence/episode storage; no original table rebuild or model call."""
REVISION='0005_memory_understanding'
TABLES={
    'episodes': """CREATE TABLE episodes (
 id TEXT PRIMARY KEY,
 conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE RESTRICT,
 title TEXT NOT NULL,
 content TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('active','tentative')),
 importance REAL NOT NULL CHECK(importance BETWEEN 0 AND 1),
 started_at TEXT NOT NULL,
 ended_at TEXT NOT NULL,
 candidate_json TEXT NOT NULL,
 supersedes TEXT REFERENCES episodes(id) ON DELETE RESTRICT,
 created_at TEXT NOT NULL,
 CHECK(supersedes IS NULL OR supersedes <> id)
)""",
    'episode_sources': """CREATE TABLE episode_sources (
 episode_id TEXT NOT NULL REFERENCES episodes(id) ON DELETE RESTRICT,
 message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE RESTRICT,
 PRIMARY KEY(episode_id,message_id)
)""",
    'episode_memory_links': """CREATE TABLE episode_memory_links (
 episode_id TEXT NOT NULL REFERENCES episodes(id) ON DELETE RESTRICT,
 memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE RESTRICT,
 PRIMARY KEY(episode_id,memory_id)
)""",
    'extraction_decisions': """CREATE TABLE extraction_decisions (
 id TEXT PRIMARY KEY,
 job_id TEXT NOT NULL REFERENCES memory_extraction_jobs(id) ON DELETE RESTRICT,
 candidate_id TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('semantic','episodic')),
 candidate_json TEXT NOT NULL,
 disposition TEXT NOT NULL CHECK(disposition IN ('active','tentative','reject')),
 reason TEXT NOT NULL,
 action TEXT NOT NULL CHECK(action IN ('created','duplicate','aligned','supplement','superseded','retracted','conflict','rejected','deferred')),
 memory_id TEXT REFERENCES memories(id) ON DELETE RESTRICT,
 episode_id TEXT REFERENCES episodes(id) ON DELETE RESTRICT,
 created_at TEXT NOT NULL,
 UNIQUE(job_id,candidate_id),
 CHECK(memory_id IS NULL OR episode_id IS NULL),
 CHECK(disposition <> 'reject' OR (memory_id IS NULL AND episode_id IS NULL))
)""",
    'version_alignments': """CREATE TABLE version_alignments (
 job_id TEXT PRIMARY KEY REFERENCES memory_extraction_jobs(id) ON DELETE RESTRICT,
 previous_job_id TEXT NOT NULL REFERENCES memory_extraction_jobs(id) ON DELETE RESTRICT,
 policy_version TEXT NOT NULL CHECK(policy_version = 'reconciler-v1'),
 created_at TEXT NOT NULL,
 CHECK(job_id <> previous_job_id)
)""",
}
TABLE_ORDERS={name: ('episode_id,message_id' if name=='episode_sources' else 'episode_id,memory_id' if name=='episode_memory_links' else 'job_id' if name=='version_alignments' else 'id') for name in TABLES}
TRIGGERS={
    f'{table}_no_{action.lower()}': f"CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT, 'derived evidence is append-only'); END"
    for table in TABLES for action in ('UPDATE','DELETE')
}


def apply(connection):
    for sql in TABLES.values():connection.execute(sql)
    for sql in TRIGGERS.values():connection.execute(sql)
    connection.execute('CREATE INDEX episodes_conversation_time ON episodes(conversation_id,ended_at)')
    connection.execute('CREATE INDEX extraction_decisions_memory ON extraction_decisions(memory_id)')
