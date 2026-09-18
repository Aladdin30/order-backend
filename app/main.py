from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.routing import APIRoute, _IncludedRouter
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.middleware.audit_middleware import AuditMiddleware
from app.api.middleware.i18n_middleware import I18nMiddleware
from app.api.v1 import get_api_v1_router
from app.api.v1.websocket import websocket_gateway
from app.core.config import settings
from app.core.exceptions import (
    localized_http_exception_handler,
    localized_validation_exception_handler,
)
from app.core.redis_pubsub import redis_pubsub
from app.core.websocket_manager import ws_manager


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application startup and shutdown lifecycle hooks."""
    # Startup: Initialize Redis Pub/Sub listener bridge
    await redis_pubsub.start(ws_manager)
    if settings.SLA_MONITOR_ENABLED:
        from app.services.sla_monitor_service import sla_monitor
        await sla_monitor.start()
    yield
    # Shutdown: Cleanly stop background tasks and close Redis Pub/Sub connections
    if settings.SLA_MONITOR_ENABLED:
        from app.services.sla_monitor_service import sla_monitor
        await sla_monitor.stop()
    await redis_pubsub.stop()


def create_app() -> FastAPI:
    """Initialize and configure the FastAPI application instance."""
    application = FastAPI(
        title=settings.PROJECT_NAME,
        openapi_url=f"{settings.API_V1_STR}/openapi.json",
        docs_url=f"{settings.API_V1_STR}/docs",
        redoc_url=f"{settings.API_V1_STR}/redoc",
        lifespan=lifespan,
    )

    # CORS configuration
    is_wildcard_cors = settings.BACKEND_CORS_ORIGINS == ["*"]
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.BACKEND_CORS_ORIGINS,
        allow_credentials=not is_wildcard_cors,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Automated mutation and security audit interceptor
    application.add_middleware(AuditMiddleware)

    # Internationalization and dynamic request locale middleware
    application.add_middleware(I18nMiddleware)

    # Global localized exception handlers
    application.add_exception_handler(HTTPException, localized_http_exception_handler)
    application.add_exception_handler(StarletteHTTPException, localized_http_exception_handler)
    application.add_exception_handler(RequestValidationError, localized_validation_exception_handler)

    # API Routers
    v1_router = get_api_v1_router()
    application.include_router(v1_router, prefix=settings.API_V1_STR)
    application.add_api_websocket_route("/ws", websocket_gateway)

    # Populate flattened route descriptors for external inspection scripts & tools
    def _flatten_routes(routes, prefix=""):
        flattened = []
        for route in routes:
            if isinstance(route, _IncludedRouter):
                sub_prefix = prefix + getattr(route.include_context, "prefix", "")
                flattened.extend(_flatten_routes(route.original_router.routes, sub_prefix))
            elif isinstance(route, APIRoute):
                full_path = prefix + route.path
                cloned = APIRoute(
                    path=full_path,
                    endpoint=route.endpoint,
                    methods=route.methods,
                    response_model=route.response_model,
                    status_code=route.status_code,
                    tags=route.tags,
                    dependencies=route.dependencies,
                    summary=route.summary,
                    description=route.description,
                    include_in_schema=False,
                )
                flattened.append(cloned)
        return flattened

    for r in _flatten_routes(list(application.routes)):
        application.routes.append(r)

    @application.get("/health", tags=["system"], summary="Service Health Check")
    async def health_check() -> dict[str, str]:
        return {"status": "healthy", "service": settings.PROJECT_NAME}

    @application.get("/health/ready", tags=["system"], summary="Service Readiness Check")
    async def readiness_check() -> dict[str, str]:
        try:
            from sqlalchemy import text
            from app.core.database import async_session_factory

            async with async_session_factory() as session:
                await session.execute(text("SELECT 1"))
            db_status = "connected"
        except Exception:
            db_status = "unavailable"

        redis_status = "connected" if redis_pubsub._is_connected else "offline"

        return {
            "status": "ready" if db_status == "connected" else "degraded",
            "database": db_status,
            "redis": redis_status,
            "service": settings.PROJECT_NAME,
        }

    @application.get("/", include_in_schema=False)
    async def root_redirect() -> RedirectResponse:
        return RedirectResponse(url=f"{settings.API_V1_STR}/docs")

    @application.get("/docs", include_in_schema=False)
    async def docs_redirect() -> RedirectResponse:
        return RedirectResponse(url=f"{settings.API_V1_STR}/docs")

    return application


app = create_app()
