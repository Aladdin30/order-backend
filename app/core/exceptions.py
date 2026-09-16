"""Global localized exception handlers for FastAPI and Starlette exceptions."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.i18n import get_current_locale, get_localized_message


async def localized_http_exception_handler(
    request: Request,
    exc: HTTPException | StarletteHTTPException,
) -> JSONResponse:
    """Intercept HTTP exceptions and dynamically translate known system message keys."""
    locale = getattr(request.state, "locale", None) or get_current_locale()

    # Translate detail if it matches a catalog key; otherwise preserve verbatim
    detail: Any = exc.detail
    if isinstance(detail, str):
        detail = get_localized_message(detail, locale=locale)

    headers = dict(getattr(exc, "headers", None) or {})
    headers["Content-Language"] = locale.value

    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": detail},
        headers=headers,
    )


async def localized_validation_exception_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    """Intercept Pydantic validation errors and return a localized envelope with details."""
    locale = getattr(request.state, "locale", None) or get_current_locale()
    summary = get_localized_message("INPUT_VALIDATION_FAILED", locale=locale)

    formatted_errors = jsonable_encoder(exc.errors())

    return JSONResponse(
        status_code=422,
        content={
            "detail": summary,
            "errors": formatted_errors,
        },
        headers={"Content-Language": locale.value},
    )
