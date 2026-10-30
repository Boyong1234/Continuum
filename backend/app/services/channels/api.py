import hmac

from fastapi import APIRouter, Depends, Request, WebSocket
from pydantic import BaseModel, ConfigDict

from app.core.config import AppError

router = APIRouter()


def management(request: Request):
    adapter = request.app.state.onebot
    token = adapter.config.admin_token
    if not token or not hmac.compare_digest(request.headers.get('authorization', '').encode(), ('Bearer ' + token).encode()):
        raise AppError('authentication_required', '通道管理需要独立的本地管理凭据。', 401)
    if not adapter.store.supported:
        raise AppError('migration_required', '通道管理需要显式迁移的v7副本。', 409)
    return adapter


class PauseRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    paused: bool


@router.websocket('/onebot/v11/ws')
async def onebot_socket(ws: WebSocket):
    await ws.app.state.onebot.websocket(ws)


@router.get('/api/channels/onebot')
async def status(adapter=Depends(management)):
    result = adapter.store.status()
    return {**result, 'fatal_code': adapter.fatal_code, 'ignored_events': adapter.ignored_events}


@router.get('/api/channels/onebot/turns')
async def turns(adapter=Depends(management)):
    return adapter.store.list_turns()


@router.get('/api/channels/onebot/deliveries')
async def deliveries(adapter=Depends(management)):
    return adapter.store.list_deliveries()


@router.post('/api/channels/onebot/pause')
async def pause(body: PauseRequest, adapter=Depends(management)):
    adapter.store.pause(body.paused)
    adapter.wakeup.set()
    return dict(paused=body.paused)


@router.post('/api/channels/onebot/turns/{id}/cancel')
async def cancel(id: str, adapter=Depends(management)):
    return await adapter.cancel(id)
