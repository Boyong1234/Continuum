"""Local-only isolated QQ server launcher. Never installs or logs in to QQ."""
import argparse
import asyncio
import os
from pathlib import Path
import sys

import uvicorn
from dotenv import dotenv_values

from app.core.config import ROOT, Settings
from app.maintenance.validation import inspect_database
from app.main import create_app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8003)
    parser.add_argument('--stop-file', type=Path)
    parser.add_argument('--env-file', type=Path)
    args = parser.parse_args()
    dataset = args.dataset.resolve()
    allowed = (ROOT / 'data/rehearsals').resolve()
    if not dataset.is_relative_to(allowed) or dataset == allowed or not 1024 <= args.port <= 65535:
        parser.error('Use an existing isolated data/rehearsals dataset and a local test port')
    if args.dataset.is_symlink() or (dataset / 'continuum.db').is_symlink() or (dataset / 'persona').is_symlink():
        parser.error('Linked datasets are not accepted')
    if args.stop_file and args.stop_file.exists():
        parser.error('Stop-file already exists; use a new path')
    try:
        if args.env_file:
            values=dotenv_values(args.env_file)
            if not args.env_file.is_file():
                raise RuntimeError('Channel configuration file does not exist')
            for key,value in values.items():
                if key.startswith('CONTINUUM_ONEBOT_') and value is not None:
                    os.environ[key]=value
        if inspect_database(dataset / 'continuum.db')['database_schema_version'] != 7:
            raise RuntimeError('Explicitly migrate a verified copy to v7 first')
        settings = Settings(database_url='sqlite:///' + (dataset / 'continuum.db').as_posix(), persona_dir=dataset / 'persona')
        if not settings.onebot.enabled:
            raise RuntimeError('Configure and explicitly enable the local OneBot channel first')
        app = create_app(settings)
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=args.port, workers=1,
            ws='websockets-sansio', ws_max_size=settings.onebot.max_frame_bytes, ws_max_queue=16,
            ws_ping_interval=20, ws_ping_timeout=20, ws_per_message_deflate=False,
            access_log=False, log_level='warning', timeout_graceful_shutdown=5))

        async def serve():
            task = asyncio.create_task(server.serve())
            try:
                while not task.done():
                    if args.stop_file and args.stop_file.exists():
                        server.should_exit = True
                    await asyncio.sleep(.1)
                await task
            finally:
                if not task.done():
                    server.should_exit = True
                    await task
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass
    except Exception:
        # Configuration, tokens and SQL payloads must not enter console logs.
        print('QQ server failed: verify isolated v7 data, single process, local config and port; private values omitted.', file=sys.stderr)
        sys.exit(2)


if __name__ == '__main__':
    main()
