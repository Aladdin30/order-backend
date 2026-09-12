"""FastAPI application initialization and middleware configuration."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.middleware.audit_middleware import AuditMiddleware
from app.api.v1 import api_v1_router
from app.core.config import settings


def create_app() -> FastAPI:
    """Initialize and configure the FastAPI application instance."""
    application = FastAPI(
        title=settings.PROJECT_NAME,
        openapi_url=f"{settings.API_V1_STR}/openapi.json",
        docs_url=f"{settings.API_V1_STR}/docs",
        redoc_url=f"{settings.API_V1_STR}/redoc",
    )

    # CORS configuration
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Automated mutation and security audit interceptor
    application.add_middleware(AuditMiddleware)

    # API Routers
    application.include_router(api_v1_router, prefix=settings.API_V1_STR)

    @application.get("/health", tags=["system"], summary="Service Health Check")
    async def health_check() -> dict[str, str]:
        return {"status": "healthy", "service": settings.PROJECT_NAME}

    return application


app = create_app()
