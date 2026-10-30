from pathlib import Path

from sqlalchemy import create_engine, event, inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base

DATABASE_SCHEMA_VERSION = 1
SUPPORTED_SCHEMA_VERSIONS = (1, 2, 3, 4, 5, 6, 7)


def setup_database(url: str):
    if not url.startswith('sqlite:///'):
        raise RuntimeError('v0.1 only supports SQLite')
    location = url.removeprefix('sqlite:///')
    if location != ':memory:':
        Path(location).resolve().parent.mkdir(parents=True, exist_ok=True)
    options = {'poolclass': StaticPool} if location == ':memory:' else {}
    engine = create_engine(url, connect_args={'check_same_thread': False, 'timeout': 10}, **options)

    @event.listens_for(engine, 'connect')
    def pragmas(connection, _):
        connection.execute('PRAGMA foreign_keys=ON')
        connection.execute('PRAGMA busy_timeout=10000')
        connection.execute('PRAGMA journal_mode=WAL')
        # Explicit SQLAlchemy BEGIN covers DDL and read snapshots as well as writes.
        connection.isolation_level = None

    @event.listens_for(engine, 'begin')
    def begin(connection):
        connection.exec_driver_sql('BEGIN IMMEDIATE' if connection.get_execution_options().get('sqlite_begin_immediate') else 'BEGIN')

    try:
        with engine.connect().execution_options(sqlite_begin_immediate=True) as connection, connection.begin():
            tables = set(inspect(connection).get_table_names())
            if not tables:
                Base.metadata.create_all(connection)
                connection.exec_driver_sql('CREATE TABLE schema_info (version INTEGER NOT NULL)')
                connection.exec_driver_sql('INSERT INTO schema_info VALUES (?)', (DATABASE_SCHEMA_VERSION,))
            else:
                if 'schema_info' not in tables:
                    raise RuntimeError('Nonempty database has no schema version; explicit migration required')
                columns = {c['name'] for c in inspect(connection).get_columns('schema_info')}
                if columns != {'version'}:
                    raise RuntimeError('Invalid database schema version table')
                versions = list(connection.exec_driver_sql('SELECT version FROM schema_info').scalars())
                if len(versions) != 1 or versions[0] not in SUPPORTED_SCHEMA_VERSIONS:
                    raise RuntimeError(f'Unsupported database schema version: {versions}')
                if versions[0] >= 2:
                    if 'migration_history' not in tables:
                        raise RuntimeError('Unsupported database schema version: v2 ledger missing')
                    from app.maintenance.migrations import validate_history
                    from app.maintenance.validation import SafetyError
                    try:
                        validate_history(connection.connection.driver_connection)
                    except SafetyError as exc:
                        raise RuntimeError('Unsupported database schema version: invalid v2 migration ledger') from exc
                    if versions[0] >= 3:
                        from app.maintenance.validation import inspect_connection
                        inspect_connection(connection.connection.driver_connection)
                for table in Base.metadata.sorted_tables:
                    if table.name not in tables:
                        raise RuntimeError(f'Incomplete database schema: missing table {table.name}')
                    actual = {column['name'] for column in inspect(connection).get_columns(table.name)}
                    if not set(table.columns.keys()).issubset(actual):
                        raise RuntimeError(f'Incomplete database schema: missing columns in {table.name}')
            for action in ('UPDATE', 'DELETE'):
                connection.exec_driver_sql(f"CREATE TRIGGER IF NOT EXISTS messages_no_{action.lower()} BEFORE {action} ON messages BEGIN SELECT RAISE(ABORT, 'messages are append-only'); END")
            connection.exec_driver_sql('CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(id UNINDEXED, content)')
            connection.exec_driver_sql("CREATE TRIGGER IF NOT EXISTS memory_fts_insert AFTER INSERT ON memories BEGIN INSERT INTO memory_fts(id, content) VALUES (new.id,new.content); END")
            connection.exec_driver_sql("CREATE TRIGGER IF NOT EXISTS memory_fts_update AFTER UPDATE ON memories BEGIN DELETE FROM memory_fts WHERE id=old.id; INSERT INTO memory_fts(id,content) VALUES(new.id,new.content); END")
            connection.exec_driver_sql('DELETE FROM memory_fts')
            connection.exec_driver_sql('INSERT INTO memory_fts(id,content) SELECT id,content FROM memories')
    except Exception:
        engine.dispose()
        raise
    return engine, sessionmaker(engine, expire_on_commit=False)
