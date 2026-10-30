from contextlib import asynccontextmanager
from pathlib import Path
import sqlite3

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import text

from app.api import router
from app.core.config import Settings, ROOT, AppError
from app.database import setup_database
from app.services.providers import ProviderRegistry
from app.services.persona import PersonaManager
from app.services.context import ContextBuilder
from app.services.conversation import ConversationEngine
from app.services.memory.runtime.control import MemoryControl,notify_saved
from app.services.memory.runtime.api import router as memory_router
from app.services.channels.onebot import OneBotChannelAdapter
from app.services.channels.api import router as channel_router


def create_app(settings=None, providers=None):
    settings = settings or Settings()
    if settings.onebot.enabled:
        # Check only schema metadata BEFORE setup_database performs any writes.
        location=settings.database_url.removeprefix('sqlite:///')
        try:
            connection=sqlite3.connect(Path(location).resolve().as_uri()+'?mode=ro',uri=True)
            try:
                connection.execute('PRAGMA query_only=ON')
                version=connection.execute('SELECT version FROM schema_info').fetchall()
            finally:connection.close()
        except sqlite3.Error:
            version=[]
        if version != [(7,)]:
            raise AppError('migration_required','QQ需要已验证的v7副本；旧库不会被自动修改。',409)
    engine, sessions = setup_database(settings.database_url)

    @asynccontextmanager
    async def lifespan(_):
        try:
            await app.state.onebot.start()
            yield
        finally:
            await app.state.onebot.stop()
            engine.dispose()

    app = FastAPI(title='Continuum', version='0.2.0-qq', lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost', '[::1]'])
    app.state.db_engine, app.state.sessions = engine, sessions
    with engine.connect() as connection:
        app.state.database_schema_version = connection.execute(text('SELECT version FROM schema_info')).scalar_one()
    app.state.providers = providers or ProviderRegistry()
    app.state.persona = PersonaManager(settings.persona_dir)
    app.state.persona.read()  # Fail visibly at startup if local persona is invalid.
    app.state.memory_control=MemoryControl(sessions,app.state.providers)
    style_references=None
    if settings.style_references_enabled:
        from app.services.context.experience import StyleReferences
        if settings.style_reference_index is None:raise ValueError('Style reference index is required')
        style_references=StyleReferences(settings.style_reference_index,sessions,cloud_authorized=settings.style_cloud_authorized)
    correction_references=None
    if settings.correction_references_enabled:
        from app.services.context.experience import CorrectionReferences
        if settings.correction_reference_index is None:raise ValueError('Correction reference index is required')
        correction_references=CorrectionReferences(settings.correction_reference_index,sessions)
    app.state.conversation_engine = ConversationEngine(sessions, app.state.providers, ContextBuilder(app.state.persona, settings.context_budget, runtime_capabilities=settings.dialogue_capabilities, chat_history_provider=settings.chat_history_provider, natural_recall=settings.natural_recall,style_references=style_references,correction_references=correction_references),memory_notifier=notify_saved if app.state.memory_control.supported else None,recover_attempts=app.state.database_schema_version<7)
    app.state.onebot=OneBotChannelAdapter(sessions,app.state.conversation_engine,settings.onebot)
    app.state.conversation_engine.channel_guard=app.state.onebot.store.owned
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=['GET', 'POST', 'PATCH'], allow_headers=['Content-Type'])

    @app.middleware('http')
    async def origin_guard(request: Request, call_next):
        origin = request.headers.get('origin')
        same_origin = str(request.base_url).rstrip('/')
        if request.method in ('POST', 'PATCH', 'PUT', 'DELETE') and origin and origin not in settings.cors_origins and origin != same_origin:
            return JSONResponse({'code': 'origin_denied', 'message': '不允许此来源修改本地数据。'}, status_code=403)
        response = await call_next(request)
        if request.url.path.startswith('/api'):
            response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    @app.exception_handler(AppError)
    async def app_error(_, error):
        return JSONResponse({'code': error.code, 'message': error.message}, status_code=error.status)

    @app.exception_handler(SQLAlchemyError)
    async def db_error(_, error):
        return JSONResponse({'code': 'database_error', 'message': '数据库操作失败；未确认写入，请检查磁盘空间和文件权限。'}, status_code=503)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_, error):
        # FastAPI defaults echo submitted inputs; keep private text out of errors.
        return JSONResponse({'code': 'invalid_input', 'message': '请求字段不符合要求。请检查必填字段与长度。'}, status_code=422)

    app.include_router(router)
    app.include_router(memory_router,prefix='/api',dependencies=router.dependencies)
    app.include_router(channel_router)
    dist = ROOT / 'frontend/dist'
    if (dist / 'index.html').exists():
        app.mount('/assets', StaticFiles(directory=dist / 'assets'), name='assets')

        @app.get('/', include_in_schema=False)
        def index():
            return FileResponse(dist / 'index.html')
    return app
