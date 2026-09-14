"""FastAPI middleware for dynamic request internationalization (i18n) and ContextVar scoping."""

from __future__ import annotations

from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.i18n import (
    DEFAULT_LOCALE,
    SupportedLocale,
    parse_accept_language,
    reset_current_locale,
    set_current_locale,
)


class I18nMiddleware(BaseHTTPMiddleware):
    """Intercept incoming HTTP requests to resolve and bind the active locale."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # 1. Check for explicit query parameter override (?lang=ar or ?locale=en)
        query_lang = request.query_params.get("lang") or request.query_params.get("locale")
        resolved_locale: SupportedLocale | None = None

        if query_lang:
            try:
                resolved_locale = SupportedLocale(query_lang.lower())
            except ValueError:
                resolved_locale = None

        # 2. Fall back to parsing RFC 9110 Accept-Language header
        if resolved_locale is None:
            accept_lang = request.headers.get("accept-language")
            resolved_locale = parse_accept_language(accept_lang)

        # 3. Bind to ContextVar and request state
        token = set_current_locale(resolved_locale)
        request.state.locale = resolved_locale

        try:
            response = await call_next(request)
            # 4. Attach standard Content-Language header to every HTTP response
            response.headers["Content-Language"] = resolved_locale.value
            return response
        finally:
            reset_current_locale(token)
