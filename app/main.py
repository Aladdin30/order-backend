"""FastAPI application initialization and middleware configuration."""

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.middleware.audit_middleware import AuditMiddleware
from app.api.middleware.i18n_middleware import I18nMiddleware
from app.api.v1 import api_v1_router
from app.core.config import settings
from app.core.exceptions import (
    localized_http_exception_handler,
    localized_validation_exception_handler,
)


def create_app() -> FastAPI:
    """Initialize and configure the FastAPI application instance."""
    application = FastAPI(
        title=settings.PROJECT_NAME,
        openapi_url=f"{settings.API_V1_STR}/openapi.json",
        docs_url=f"{settings.API_V1_STR}/docs",
        redoc_url=f"{settings.API_V1_STR}/redoc",
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
    application.include_router(api_v1_router, prefix=settings.API_V1_STR)

    @application.get("/health", tags=["system"], summary="Service Health Check")
    async def health_check() -> dict[str, str]:
        return {"status": "healthy", "service": settings.PROJECT_NAME}

    @application.get("/", include_in_schema=False)
    async def root_redirect() -> RedirectResponse:
        return RedirectResponse(url=f"{settings.API_V1_STR}/docs")

    @application.get("/docs", include_in_schema=False)
    async def docs_redirect() -> RedirectResponse:
        return RedirectResponse(url=f"{settings.API_V1_STR}/docs")

    return application


app = create_app()
