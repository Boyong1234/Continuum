"""Explicit ordered v1-v7 copy migrations; released revisions remain immutable."""
import sqlite3
from pathlib import Path

from app.models import utcnow
from app.maintenance.validation import SafetyError, require, file_digest, normalize_sql, inspect_database, inspect_connection
from app.maintenance.revisions import v0002_migration_history as revision_v2
from app.maintenance.revisions import v0003_generation_attempts as revision_v3
from app.maintenance.revisions import v0004_memory_foundation as revision_v4
from app.maintenance.revisions import v0005_memory_understanding as revision_v5
from app.maintenance.revisions import v0006_memory_runtime as revision_v6
from app.maintenance.revisions import v0007_qq_channel as revision_v7

REVISION = revision_v2.REVISION
LEDGER_SQL = revision_v2.LEDGER_SQL


def revision_checksum():
    return file_digest(Path(revision_v2.__file__))


def validate_history(connection):
    row = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='migration_history'").fetchone()
    require(row is not None and normalize_sql(row[0]) == normalize_sql(LEDGER_SQL), 'migration_history', 'Invalid migration ledger schema')
    rows = connection.execute('SELECT revision,from_version,to_version,applied_at,code_sha256,backup_id FROM migration_history').fetchall()
    version = connection.execute('SELECT version FROM schema_info').fetchone()[0]
    rows.sort(key=lambda row: row[1])
    require(len(rows) == version - 1 and version in (2, 3, 4, 5, 6, 7), 'migration_history', 'Unknown migration chain')
    from app.maintenance.validation import validate_utc
    for item, module, before in zip(rows, (revision_v2, revision_v3, revision_v4, revision_v5, revision_v6, revision_v7), (1, 2, 3, 4, 5, 6)):
        revision, source, after, date, checksum, backup_id = item
        require((revision, source, after, checksum) == (module.REVISION, before, before + 1, file_digest(Path(module.__file__))), 'migration_history', 'Migration revision/checksum mismatch')
        validate_utc(date)
        require(isinstance(backup_id, str) and bool(backup_id), 'migration_history', 'Missing migration backup identity')


def _apply_v2(connection):
    revision_v2.apply(connection)


def migrate_staged_database(path, backup_id, expected_entities, *, backup_package, target_version=2):
    from app.maintenance.backup import verify_backup
    manifest = verify_backup(backup_package)
    require(manifest['backup_id'] == backup_id and manifest['entities'] == expected_entities, 'backup_required', 'Migration requires a matching verified backup package')
    require(Path(path).resolve() != (Path(backup_package) / 'database.sqlite3').resolve(), 'unsafe_target', 'Never migrate a backup package in place')
    baseline = inspect_database(path)
    require(baseline['entities'] == expected_entities, 'baseline_mismatch', 'Migration input does not match verified backup')
    require(target_version in (2, 3, 4, 5, 6, 7) and baseline['database_schema_version'] <= target_version, 'schema_version', 'Unsupported migration target or downgrade')
    if baseline['database_schema_version'] == target_version:
        return 'already_current'
    require(bool(backup_id), 'backup_required', 'Migration requires a verified pre-migration backup')
    connection = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=rw', uri=True, isolation_level=None, timeout=10)
    try:
        connection.execute('PRAGMA foreign_keys=ON')
        connection.execute('BEGIN IMMEDIATE')
        for version in range(baseline['database_schema_version'] + 1, target_version + 1):
            module = {2: revision_v2, 3: revision_v3, 4: revision_v4, 5: revision_v5, 6: revision_v6, 7: revision_v7}[version]
            (_apply_v2 if version == 2 else module.apply)(connection)
            connection.execute('INSERT INTO migration_history VALUES (?,?,?,?,?,?)', (module.REVISION, version - 1, version, utcnow(), file_digest(Path(module.__file__)), backup_id))
            connection.execute('UPDATE schema_info SET version=?', (version,))
        after = inspect_connection(connection)
        validate_preserved(connection, backup_package, expected_entities)
        connection.commit()
        return 'applied'
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def validate_preserved(connection, backup_package, expected_entities):
    """v4 deliberately appends one baseline revision, preserving every old row."""
    after = inspect_connection(connection)
    from app.maintenance.validation import read_database
    with read_database(Path(backup_package) / 'database.sqlite3') as source:
        before_version = source.execute('SELECT version FROM schema_info').fetchone()[0]
        for table, expected in expected_entities.items():
            if after['entities'].get(table) == expected:
                continue
            require(table == 'memory_revisions' and before_version < 4 and after['database_schema_version'] >= 4, 'data_changed', 'Migration changed original entities')
            original = source.execute('SELECT * FROM memory_revisions ORDER BY id').fetchall()
            current = {row[0]: row for row in connection.execute('SELECT * FROM memory_revisions')}
            require(all(current.get(row[0]) == row for row in original), 'data_changed', 'Migration changed old revisions')
            original_ids = {row[0] for row in original}
            added = set(current) - original_ids
            mids = [row[0] for row in source.execute('SELECT id FROM memories')]
            require(added == {revision_v4.baseline_id(mid) for mid in mids}, 'data_changed', 'Unexpected migration revision additions')
            import json
            cursor = source.execute('SELECT * FROM memories ORDER BY id')
            columns = [column[0] for column in cursor.description]
            for row in cursor.fetchall():
                memory = dict(zip(columns, row))
                snapshot = json.loads(current[revision_v4.baseline_id(memory['id'])][3])
                for key, value in memory.items():
                    require(snapshot.get('legacy_status' if key == 'status' else key) == value, 'data_changed', 'Migration baseline does not preserve original memory')
                ids = [row[0] for row in source.execute('SELECT message_id FROM memory_sources WHERE memory_id=? ORDER BY message_id', (memory['id'],))]
                require(snapshot.get('source_message_ids') == ids, 'data_changed', 'Migration baseline source mismatch')
