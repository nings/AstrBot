from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from astrbot.core import LogBroker
from astrbot.core.core_lifecycle import AstrBotCoreLifecycle
from astrbot.core.db import BaseDatabase
from astrbot.core.log import LogManager
from astrbot.dashboard.responses import ApiError, error
from astrbot.dashboard.services.api_key_service import ApiKeyService
from astrbot.dashboard.services.auth_service import AuthService
from astrbot.dashboard.services.backup_service import BackupService
from astrbot.dashboard.services.chat_service import ChatService
from astrbot.dashboard.services.chatui_project_service import ChatUIProjectService
from astrbot.dashboard.services.command_service import CommandService
from astrbot.dashboard.services.config_service import (
    BotConfigService,
    ConfigDisplayService,
    ConfigFileService,
    ConfigProfileService,
    ConfigRoutingService,
    ProviderConfigService,
)
from astrbot.dashboard.services.conversation_service import ConversationService
from astrbot.dashboard.services.cron_service import CronService
from astrbot.dashboard.services.file_service import FileService
from astrbot.dashboard.services.knowledge_base_service import KnowledgeBaseService
from astrbot.dashboard.services.live_chat_service import LiveChatService
from astrbot.dashboard.services.log_service import LogService
from astrbot.dashboard.services.open_api_service import OpenApiService
from astrbot.dashboard.services.persona_service import PersonaService
from astrbot.dashboard.services.platform_service import PlatformService
from astrbot.dashboard.services.plugin_page_service import PluginPageService
from astrbot.dashboard.services.plugin_service import PluginService
from astrbot.dashboard.services.session_management_service import (
    SessionManagementService,
    register_session_rule_guard,
)
from astrbot.dashboard.services.skills_service import SkillsService
from astrbot.dashboard.services.stat_service import StatService
from astrbot.dashboard.services.subagent_service import SubAgentService
from astrbot.dashboard.services.t2i_service import T2iService
from astrbot.dashboard.services.tools_service import ToolsService
from astrbot.dashboard.services.update_service import (
    DEMO_MODE,
    UpdateService,
    call_download_dashboard,
    call_extract_dashboard,
    call_get_dashboard_version,
    call_pip_install,
)
from astrbot.portal import session_rule_guard as portal_session_rule_guard
from astrbot.portal.api.cors import install_portal_cors
from astrbot.portal.config import build_portal_settings, configure_portal_settings
from astrbot.portal.db import engine as portal_db_engine
from astrbot.portal.services import core_facade as portal_core_facade
from astrbot.portal.startup_checks import (
    PortalUmoRoutingUnisolatedError,
    check_umo_routing,
)

from .api_keys import legacy_router as legacy_api_keys_router
from .auth import legacy_router as legacy_auth_router
from .backups import legacy_router as legacy_backups_router
from .bots import legacy_router as legacy_bots_router
from .chat import legacy_router as legacy_chat_router
from .chat_projects import legacy_router as legacy_chat_projects_router
from .config_profiles import legacy_router as legacy_config_profiles_router
from .conversations import legacy_router as legacy_conversations_router
from .cron import legacy_router as legacy_cron_router
from .extensions import legacy_router as legacy_extensions_router
from .files import legacy_router as legacy_files_router
from .knowledge_bases import legacy_router as legacy_knowledge_bases_router
from .live_chat import legacy_router as legacy_live_chat_router
from .logs import legacy_router as legacy_logs_router
from .personas import legacy_router as legacy_personas_router
from .platform import legacy_router as legacy_platform_router
from .plugins import legacy_router as legacy_plugins_router
from .providers import legacy_router as legacy_providers_router
from .router import API_V1_PREFIX, build_api_router
from .sessions import legacy_router as legacy_sessions_router
from .skills import legacy_router as legacy_skills_router
from .static_files import router as static_files_router
from .stats import legacy_router as legacy_stats_router
from .subagents import legacy_router as legacy_subagents_router
from .t2i import legacy_router as legacy_t2i_router
from .tools import legacy_router as legacy_tools_router
from .updates import legacy_router as legacy_updates_router

CLEAR_SITE_DATA_HEADERS = {"Clear-Site-Data": '"cache"'}


async def init_portal_module_state(app: FastAPI) -> None:
    """Wire up the astrbot/portal/** module's own runtime state.

    Registered as a FastAPI startup hook below (needs to run once the event
    loop is actually up, since both steps need async I/O -- aiosqlite /
    ``SharedPreferences`` -- that cannot happen synchronously inside
    ``create_dashboard_asgi_app`` itself). Exposed as a standalone,
    module-level function (rather than a closure nested in
    ``create_dashboard_asgi_app``) so it can also be awaited directly from a
    test without having to drive the ASGI lifespan protocol.

    Without this, every request into ``/api/v1/portal/*`` fails:

    1. ``portal_db_engine.init_portal_db()`` creates the module's independent
       SQLite schema (``portal_users``/``portal_sessions``/etc). Nothing else
       in the codebase ever calls it, so without this the very first
       ``POST /api/v1/portal/auth/register`` raises
       ``sqlite3.OperationalError: no such table: portal_users``.
    2. ``app.state.portal_jwt_secret`` is read by
       ``astrbot.portal.auth.dependencies.get_current_user`` on every
       protected portal route; if it is never set there, every such request
       401s unconditionally regardless of how valid the caller's cookie is.
    """
    await portal_db_engine.init_portal_db()
    app.state.portal_jwt_secret = await portal_core_facade.get_jwt_secret()

    # 3. The portal claims the `kb_config` session rule. Core's
    #    /api/v1/sessions/rules route is gated only by require_data_scope and
    #    knows nothing about the portal's KB whitelist, so without this guard
    #    that route can bind arbitrary knowledge bases to a portal session, or
    #    delete the key and re-open the global-KB fallback. Registration is
    #    inverted deliberately: core exposes the hook and the portal fills it,
    #    so no core service imports this module.
    portal_session_rule_guard.register(register_session_rule_guard)


@asynccontextmanager
async def _dashboard_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """FastAPI ``lifespan`` handler: wires up the portal module's runtime
    state (see ``init_portal_module_state``) and runs its Phase 3 umo
    routing isolation health check on startup. Nothing needs to run on
    shutdown yet.
    """
    await init_portal_module_state(app)

    # Portal Phase 3: log (on every start, success or failure) whether
    # portal traffic routes to an isolated AstrBotConfig profile -- see
    # astrbot/portal/startup_checks.py for the full rationale and the
    # CRITICAL remediation logged when it does not. refuse_to_start is not
    # wired to any operator-facing setting yet (no such PortalSettings
    # field exists); it defaults to False, so a failing check only warns
    # loudly and never aborts startup by itself.
    try:
        check_umo_routing(app.state.core_lifecycle.umop_config_router)
    except PortalUmoRoutingUnisolatedError:
        # Unreachable while refuse_to_start defaults to False above; kept
        # so this still aborts startup correctly if a future change ever
        # passes refuse_to_start=True here.
        raise

    yield


def create_dashboard_asgi_app(
    *,
    core_lifecycle: AstrBotCoreLifecycle,
    db: BaseDatabase,
    jwt_secret: str,
    static_folder: str | None = None,
) -> FastAPI:
    app = FastAPI(
        title="AstrBot OpenAPI",
        version="1.0.0",
        openapi_url=f"{API_V1_PREFIX}/openapi.json",
        docs_url=f"{API_V1_PREFIX}/docs",
        redoc_url=f"{API_V1_PREFIX}/redoc",
        lifespan=_dashboard_lifespan,
    )
    app.state.core_lifecycle = core_lifecycle
    app.state.db = db
    app.state.jwt_secret = jwt_secret
    app.state.dashboard_static_folder = static_folder

    # Portal settings must be installed here, synchronously at app-construction
    # time, rather than in the lifespan hook below: `install_portal_cors` reads
    # them to decide whether to add middleware at all, and middleware cannot be
    # added once the app has started. A bad value raises out of
    # `build_portal_settings`/`PortalSettings.__post_init__` and aborts startup
    # by design -- this module fails closed on misconfiguration rather than
    # silently narrowing its own security surface.
    portal_settings = build_portal_settings(core_lifecycle.astrbot_config.get("portal"))
    configure_portal_settings(portal_settings)
    if install_portal_cors(app, portal_settings):
        LogManager.GetLogger("astrbot.portal").info(
            "Portal CORS enabled for %s; session cookies are %s.",
            ", ".join(portal_settings.cors_allowed_origins),
            "SameSite=None; Secure (third-party cookies -- blocked by Safari "
            "and being phased out by Chrome; prefer serving the portal SPA "
            "behind a reverse proxy on this same origin)"
            if portal_settings.cross_origin_cookies
            else "still SameSite=Lax, which a browser will NOT send on "
            "cross-site requests -- set portal.cross_origin_cookies if the "
            "SPA really is on another origin",
        )
    log_broker = getattr(core_lifecycle, "log_broker", None) or LogBroker()
    app.state.services = SimpleNamespace(
        config_profiles=ConfigProfileService(core_lifecycle, db),
        config_display=ConfigDisplayService(core_lifecycle),
        config_files=ConfigFileService(core_lifecycle),
        config_routes=ConfigRoutingService(core_lifecycle),
        api_keys=ApiKeyService(db),
        auth=AuthService(db, core_lifecycle.astrbot_config),
        backups=BackupService(db, core_lifecycle),
        chat=ChatService(db, core_lifecycle),
        chat_projects=ChatUIProjectService(db),
        commands=CommandService(core_lifecycle.astrbot_config, core_lifecycle),
        conversations=ConversationService(db, core_lifecycle),
        cron=CronService(core_lifecycle),
        files=FileService(),
        knowledge_bases=KnowledgeBaseService(core_lifecycle),
        live_chat=LiveChatService(db, core_lifecycle),
        logs=LogService(log_broker, core_lifecycle.astrbot_config),
        bots=BotConfigService(core_lifecycle),
        platforms=PlatformService(core_lifecycle),
        providers=ProviderConfigService(core_lifecycle),
        personas=PersonaService(core_lifecycle),
        plugins=PluginService(core_lifecycle, core_lifecycle.plugin_manager),
        plugin_pages=PluginPageService(
            core_lifecycle.plugin_manager,
            core_lifecycle=core_lifecycle,
        ),
        open_api=OpenApiService(db, core_lifecycle),
        sessions=SessionManagementService(core_lifecycle, db),
        skills=SkillsService(core_lifecycle),
        stats=StatService(db, core_lifecycle, core_lifecycle.astrbot_config),
        subagents=SubAgentService(core_lifecycle),
        t2i=T2iService(core_lifecycle),
        tools=ToolsService(core_lifecycle),
        updates=UpdateService(
            core_lifecycle.astrbot_updator,
            core_lifecycle,
            download_dashboard_func=call_download_dashboard,
            extract_dashboard_func=call_extract_dashboard,
            get_dashboard_version_func=call_get_dashboard_version,
            pip_install_func=call_pip_install,
            demo_mode=DEMO_MODE,
            clear_site_data_headers=CLEAR_SITE_DATA_HEADERS,
        ),
    )

    @app.exception_handler(ApiError)
    async def api_error_handler(_request: Request, exc: ApiError):
        return JSONResponse(
            error(exc.message, exc.data),
            status_code=exc.status_code,
        )

    @app.exception_handler(ValueError)
    async def value_error_handler(_request: Request, exc: ValueError):
        return JSONResponse(error(str(exc)), status_code=400)

    @app.exception_handler(StarletteHTTPException)
    async def starlette_http_error_handler(
        _request: Request, exc: StarletteHTTPException
    ):
        if isinstance(exc.detail, str):
            return JSONResponse(
                error(exc.detail), status_code=exc.status_code, headers=exc.headers
            )
        return JSONResponse(
            error("Request failed", exc.detail),
            status_code=exc.status_code,
            headers=exc.headers,
        )

    @app.exception_handler(Exception)
    async def catch_all_handler(_request: Request, exc: Exception):
        LogManager.GetLogger("astrbot.dashboard").error(
            "Unhandled exception in dashboard API",
            exc_info=exc,
        )
        return JSONResponse(
            error("Internal server error"),
            status_code=500,
        )

    # Legacy dashboard routes keep old /api/* callers working without entering OpenAPI.
    app.include_router(legacy_api_keys_router)
    app.include_router(legacy_auth_router)
    app.include_router(legacy_backups_router)
    app.include_router(legacy_config_profiles_router)
    app.include_router(legacy_bots_router)
    app.include_router(legacy_providers_router)
    app.include_router(legacy_chat_router)
    app.include_router(legacy_chat_projects_router)
    app.include_router(legacy_conversations_router)
    app.include_router(legacy_cron_router)
    app.include_router(legacy_extensions_router)
    app.include_router(legacy_files_router)
    app.include_router(legacy_knowledge_bases_router)
    app.include_router(legacy_live_chat_router)
    app.include_router(legacy_logs_router)
    app.include_router(legacy_sessions_router)
    app.include_router(legacy_skills_router)
    app.include_router(legacy_stats_router)
    app.include_router(legacy_subagents_router)
    app.include_router(legacy_tools_router)
    app.include_router(legacy_platform_router)
    app.include_router(legacy_plugins_router)
    app.include_router(legacy_t2i_router)
    app.include_router(legacy_personas_router)
    app.include_router(legacy_updates_router)
    app.include_router(build_api_router())
    app.include_router(static_files_router)
    return app
