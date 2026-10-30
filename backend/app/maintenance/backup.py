import json
import os
import shutil
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from app.models import utcnow
from app.maintenance.validation import (
    PERSONA_FILES, SafetyError, require, sha256, file_digest,
    inspect_database, read_database, validate_utc,
)
from app.maintenance.migrations import migrate_staged_database

BACKUP_FORMAT_VERSION = 1
EXPORT_SCHEMA_VERSION = 1
PAYLOAD_FILES = {'database.sqlite3'} | {f'persona/{name}' for name in PERSONA_FILES}


def _read_persona(directory):
    directory = Path(directory)
    require(not directory.is_symlink(), 'persona_path', 'Symlink persona directories are not accepted')
    result = {}
    for name in PERSONA_FILES:
        path = directory / name
        require(path.is_file() and not path.is_symlink(), 'persona_path', 'Missing or symlink persona file')
        data = path.read_bytes()
        require(len(data) <= 4 * 1024 * 1024, 'persona_size', 'Persona file exceeds maintenance size limit')
        data.decode('utf-8')
        result[name] = data
    # Validate preferences before advertising a restorable application dataset.
    import yaml
    try:
        yaml.safe_load(result['preferences.yaml'].decode('utf-8'))
    except yaml.YAMLError as exc:
        raise SafetyError('persona_format', 'Invalid preferences YAML') from exc
    return result


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _json_write(path, value):
    _write(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8'))


def _sync_directory(path):
    if os.name != 'nt':
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


@contextmanager
def _staging(destination):
    destination = Path(destination).absolute()
    require(not destination.exists() and not destination.is_symlink(), 'target_exists', 'Destination must not exist')
    destination.parent.mkdir(parents=True, exist_ok=True)
    parent = destination.parent.resolve()
    destination = parent / destination.name
    stage = Path(tempfile.mkdtemp(prefix='.continuum-stage-', dir=parent))
    try:
        yield stage, destination
    finally:
        if stage.exists():
            # Resolve and constrain recursive deletion to this operation's own directory.
            require(not stage.is_symlink() and stage.resolve().parent == parent and stage.name.startswith('.continuum-stage-'), 'unsafe_cleanup', 'Refusing unsafe staging cleanup')
            shutil.rmtree(stage)


def _publish(stage, target):
    require(not target.exists() and not target.is_symlink(), 'target_exists', 'Destination appeared during staging')
    for path in stage.rglob('*'):
        if path.is_file():
            with path.open('r+b') as handle:
                os.fsync(handle.fileno())
    for directory in sorted((p for p in stage.rglob('*') if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        _sync_directory(directory)
    _sync_directory(stage)
    os.rename(stage, target)
    _sync_directory(target.parent)


def _copy_database(source, destination):
    deadline = time.monotonic() + 30

    def progress(status, remaining, total):
        require(time.monotonic() < deadline, 'backup_timeout', 'SQLite backup deadline exceeded')

    with read_database(source) as reader:
        writer = sqlite3.connect(destination)
        try:
            reader.backup(writer, pages=256, progress=progress, sleep=0.01)
            # Snapshot is standalone: it must not require WAL sidecar files.
            writer.execute('PRAGMA journal_mode=DELETE')
        finally:
            writer.close()


def create_backup(database, persona, destination, *, allow_plaintext=False):
    require(allow_plaintext, 'plaintext_opt_in', 'Backup is plaintext; pass --allow-plaintext explicitly')
    database, persona = Path(database).resolve(), Path(persona).resolve()
    require(database.is_file(), 'missing_database', 'Source database does not exist')
    with _staging(destination) as (stage, target):
        # Reserve the writer slot without modifying application rows or schema.
        lock = sqlite3.connect(database.as_uri() + '?mode=rw', uri=True, timeout=10, isolation_level=None)
        try:
            lock.execute('BEGIN IMMEDIATE')
            before = _read_persona(persona)
            _copy_database(database, stage / 'database.sqlite3')
            for name, data in before.items():
                _write(stage / 'persona' / name, data)
            require(_read_persona(persona) == before, 'persona_changed', 'persona changed during backup; retry without editing files')
            snapshot = inspect_database(stage / 'database.sqlite3')
            lock.rollback()
        except sqlite3.Error as exc:
            raise SafetyError('database_busy_or_invalid', 'Could not reserve or snapshot SQLite database') from exc
        finally:
            lock.close()
        # Close all database handles before hashing the published snapshot.
        manifest = {
            'backup_format_version': BACKUP_FORMAT_VERSION,
            'database_schema_version': snapshot['database_schema_version'],
            'export_schema_version': 2 if snapshot['database_schema_version'] >= 4 else EXPORT_SCHEMA_VERSION,
            'app_version': '0.2.0+qq',
            'backup_id': str(uuid4()),
            'snapshot_at': utcnow(),
            'encrypted': False,
            'consistency': 'sqlite-writer-reservation+persona-stability-check',
            'entities': snapshot['entities'],
            'files': {name: {'size': (stage / name).stat().st_size, 'sha256': file_digest(stage / name)} for name in sorted(PAYLOAD_FILES)},
        }
        _json_write(stage / 'manifest.json', manifest)
        verify_backup(stage)
        _publish(stage, target)
    return manifest


def verify_backup(package):
    package = Path(package)
    require(package.is_dir() and not package.is_symlink(), 'invalid_package', 'Backup package must be a regular directory')
    manifest_file = package / 'manifest.json'
    require(manifest_file.is_file() and not manifest_file.is_symlink() and manifest_file.stat().st_size < 1024 * 1024, 'invalid_manifest', 'Invalid backup manifest')
    try:
        manifest = json.loads(manifest_file.read_text(encoding='utf-8'))
    except (ValueError, UnicodeError) as exc:
        raise SafetyError('invalid_manifest', 'Invalid backup manifest JSON') from exc
    require(isinstance(manifest, dict), 'invalid_manifest', 'Manifest must be an object')
    required = {'backup_format_version', 'database_schema_version', 'export_schema_version', 'app_version', 'backup_id', 'snapshot_at', 'encrypted', 'consistency', 'entities', 'files'}
    require(set(manifest) == required, 'invalid_manifest', 'Unknown or missing manifest fields')
    require(type(manifest['backup_format_version']) is int and manifest['backup_format_version'] == 1, 'backup_version', 'Unsupported backup format version')
    require(type(manifest['database_schema_version']) is int and manifest['database_schema_version'] in (1, 2, 3, 4, 5, 6, 7), 'schema_version', 'Unsupported backup database version')
    require(type(manifest['export_schema_version']) is int and manifest['export_schema_version'] == (2 if manifest['database_schema_version'] >= 4 else 1), 'export_version', 'Unsupported export format version')
    require(manifest['encrypted'] is False, 'backup_encryption', 'Encrypted backup format not implemented')
    require(isinstance(manifest['backup_id'], str) and bool(manifest['backup_id']), 'invalid_manifest', 'Missing backup identity')
    validate_utc(manifest['snapshot_at'])
    require(isinstance(manifest['files'], dict) and set(manifest['files']) == PAYLOAD_FILES, 'invalid_manifest', 'Backup file allowlist mismatch')
    require(not (package / 'persona').is_symlink(), 'invalid_path', 'Symlink paths are not accepted')
    actual = set()
    for path in package.rglob('*'):
        require(not path.is_symlink(), 'invalid_path', 'Symlink paths are not accepted')
        if path.is_file():
            actual.add(path.relative_to(package).as_posix())
        else:
            require(path.relative_to(package).as_posix() == 'persona', 'invalid_path', 'Unexpected backup directory')
    require(actual == PAYLOAD_FILES | {'manifest.json'}, 'invalid_package', 'Missing or unexpected backup files')
    for name, details in manifest['files'].items():
        require(isinstance(details, dict) and set(details) == {'size', 'sha256'}, 'invalid_manifest', 'Invalid file metadata')
        path = package / name  # Fixed allowlist; never use arbitrary manifest paths.
        require(type(details['size']) is int and details['size'] >= 0 and path.stat().st_size == details['size'], 'hash_mismatch', 'Backup file size mismatch')
        require(file_digest(path) == details['sha256'], 'hash_mismatch', 'Backup file SHA-256 mismatch')
    _read_persona(package / 'persona')
    snapshot = inspect_database(package / 'database.sqlite3')
    require(snapshot['database_schema_version'] == manifest['database_schema_version'] and snapshot['entities'] == manifest['entities'], 'manifest_mismatch', 'Database content does not match manifest')
    return manifest


def _restore_stage(package, stage, manifest):
    # Copy only bytes that still match the verified manifest; never execute SQL input.
    for name in sorted(PAYLOAD_FILES):
        target = stage / ('continuum.db' if name == 'database.sqlite3' else name)
        data = (Path(package) / name).read_bytes()
        require(len(data) == manifest['files'][name]['size'] and sha256(data) == manifest['files'][name]['sha256'], 'package_changed', 'Backup changed during restore')
        _write(target, data)
    connection = sqlite3.connect(stage / 'continuum.db')
    try:
        connection.execute('DELETE FROM memory_fts')
        connection.execute('INSERT INTO memory_fts(id,content) SELECT id,content FROM memories')
        connection.commit()
    finally:
        connection.close()
    snapshot = inspect_database(stage / 'continuum.db')
    require(snapshot['entities'] == manifest['entities'], 'restore_mismatch', 'Restored original entities differ from backup')
    require(_read_persona(stage / 'persona') == _read_persona(Path(package) / 'persona'), 'package_changed', 'Persona changed during restore')


def restore_backup(package, destination):
    manifest = verify_backup(package)
    with _staging(destination) as (stage, target):
        _restore_stage(package, stage, manifest)
        _json_write(stage / 'restore_receipt.json', {'backup_id': manifest['backup_id'], 'restored_at': utcnow(), 'database_schema_version': manifest['database_schema_version']})
        _publish(stage, target)
    return {'destination': str(target), 'backup_id': manifest['backup_id'], 'database_schema_version': manifest['database_schema_version']}


def upgrade_copy(database, persona, destination, backup_root, *, allow_plaintext=False, target_version=2):
    # Refuse an existing destination before even creating the automatic backup.
    require(not Path(destination).exists() and not Path(destination).is_symlink(), 'target_exists', 'Upgrade destination must not exist')
    package = Path(backup_root).resolve() / ('pre-migration-' + str(uuid4()))
    manifest = create_backup(database, persona, package, allow_plaintext=allow_plaintext)
    manifest = verify_backup(package)
    with _staging(destination) as (stage, target):
        _restore_stage(package, stage, manifest)
        status = migrate_staged_database(stage / 'continuum.db', manifest['backup_id'], manifest['entities'], backup_package=package, target_version=target_version)
        snapshot = inspect_database(stage / 'continuum.db')
        from app.maintenance.migrations import validate_preserved
        with read_database(stage / 'continuum.db') as connection:
            validate_preserved(connection, package, manifest['entities'])
        _json_write(stage / 'restore_receipt.json', {'backup_id': manifest['backup_id'], 'restored_at': utcnow(), 'database_schema_version': target_version, 'migration': status})
        _publish(stage, target)
    return {'destination': str(target), 'backup': str(package), 'backup_id': manifest['backup_id'], 'database_schema_version': target_version, 'migration': status, 'entities': snapshot['entities']}
