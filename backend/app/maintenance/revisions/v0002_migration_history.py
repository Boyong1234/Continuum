"""Revision 0002. Do not edit after a migrated dataset is published."""

REVISION = '0002_migration_history'
LEDGER_SQL = '''CREATE TABLE migration_history (
    revision TEXT PRIMARY KEY,
    from_version INTEGER NOT NULL,
    to_version INTEGER NOT NULL,
    applied_at TEXT NOT NULL,
    code_sha256 TEXT NOT NULL,
    backup_id TEXT NOT NULL
)'''


def apply(connection):
    connection.execute(LEDGER_SQL)
