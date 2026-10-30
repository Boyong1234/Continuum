import argparse
import json
import sys
import sqlite3

from app.maintenance.backup import create_backup, verify_backup, restore_backup, upgrade_copy
from app.maintenance.validation import inspect_database, SafetyError


def main():
    parser = argparse.ArgumentParser(description='Continuum local backup/restore/copy-migration. No in-place upgrade.')
    subparsers = parser.add_subparsers(dest='command', required=True)
    inspect = subparsers.add_parser('inspect')
    inspect.add_argument('--database', required=True)
    verify = subparsers.add_parser('verify')
    verify.add_argument('--backup', required=True)
    restore = subparsers.add_parser('restore')
    restore.add_argument('--backup', required=True)
    restore.add_argument('--destination', required=True)
    for name in ('backup', 'upgrade'):
        command = subparsers.add_parser(name)
        command.add_argument('--database', required=True)
        command.add_argument('--persona', required=True)
        command.add_argument('--destination', required=True)
        command.add_argument('--allow-plaintext', action='store_true', help='Explicit acknowledgement: package contains sensitive unencrypted data')
        if name == 'upgrade':
            command.add_argument('--backup-root', required=True)
            command.add_argument('--target-version', type=int, choices=[2, 3, 4, 5, 6, 7], default=2)
    args = parser.parse_args()
    try:
        if args.command == 'inspect':
            result = inspect_database(args.database)
        elif args.command == 'verify':
            manifest = verify_backup(args.backup)
            result = {'backup_id': manifest['backup_id'], 'database_schema_version': manifest['database_schema_version'], 'entities': manifest['entities']}
        elif args.command == 'restore':
            result = restore_backup(args.backup, args.destination)
        elif args.command == 'backup':
            print('NOTICE: sensitive plaintext backup; .env and keys are excluded.', file=sys.stderr)
            manifest = create_backup(args.database, args.persona, args.destination, allow_plaintext=args.allow_plaintext)
            result = {'backup_id': manifest['backup_id'], 'destination': args.destination, 'database_schema_version': manifest['database_schema_version'], 'entities': manifest['entities']}
        else:
            print('NOTICE: migration operates on a new copy; automatic backup is plaintext.', file=sys.stderr)
            result = upgrade_copy(args.database, args.persona, args.destination, args.backup_root, allow_plaintext=args.allow_plaintext, target_version=args.target_version)
        print(json.dumps({'status': 'ok', **result}, ensure_ascii=False))
        return 0
    except SafetyError as exc:
        print(json.dumps({'status': 'error', 'code': exc.code, 'message': str(exc)}), file=sys.stderr)
        return 2
    except (OSError, ValueError, RuntimeError, sqlite3.Error):
        print(json.dumps({'status': 'error', 'code': 'maintenance_failed', 'message': 'Maintenance failed; source was not replaced. Private details omitted.'}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
