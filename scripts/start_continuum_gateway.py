"""Explicit loopback gateway entry; native OneBot remains an independent fallback."""
import argparse
import asyncio
import os
import json
import sys
from gateway_common import ROOT,local_env,isolated


async def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset',required=True);parser.add_argument('--env-file',required=True)
    parser.add_argument('--port',type=int,default=8010);parser.add_argument('--stop-file')
    args=parser.parse_args();dataset=isolated(args.dataset)
    local_env(args.env_file)
    sys.path.insert(0,str(ROOT/'backend'))
    from dotenv import dotenv_values
    pilot_keys=('CONTINUUM_DIALOGUE_CAPABILITIES','CONTINUUM_CHAT_HISTORY_PROVIDER','CONTINUUM_NATURAL_RECALL')
    for key,value in dotenv_values(dataset/'memory-pilot.env').items():
        if key not in pilot_keys or value is None:raise ValueError('Unexpected memory pilot setting')
        os.environ[key]=value
    from app.core.config import Settings
    from app.services.channels.gateway import create_gateway_app,GatewayConfig
    import uvicorn
    settings=Settings(database_url='sqlite:///'+(dataset/'continuum.db').as_posix(),persona_dir=dataset/'persona')
    # The entry already confines paths to this project's data directory. The
    # original opt-in rehearsal guard is for development API callers, not new users.
    app=create_gateway_app(settings,GatewayConfig.environment(),rehearsal_only=False)
    context=app.state.conversation_engine.context
    if context.correction_references is not None:context.correction_references.refresh()
    print('continuum_runtime '+json.dumps(dict(schema=app.state.database_schema_version,
        dataset=str(dataset.relative_to(ROOT)),correction_references=context.correction_references is not None,
        correction_index=str(context.correction_references.path.relative_to(ROOT)) if context.correction_references is not None else None,
        style_anchor=context.style_references is not None,history_retriever=type(app.state.conversation_engine.retriever).__name__),ensure_ascii=False),flush=True)
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=args.port,access_log=False,log_level='warning'))
    async def watch():
        if args.stop_file:
            stop=isolated(args.stop_file)
            while not stop.exists():await asyncio.sleep(.1)
            server.should_exit=True
    watcher=asyncio.create_task(watch())
    try:await server.serve()
    finally:watcher.cancel();await asyncio.gather(watcher,return_exceptions=True)


if __name__=='__main__':asyncio.run(main())
