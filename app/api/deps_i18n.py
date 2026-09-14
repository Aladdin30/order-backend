"""FastAPI dependencies for internationalization and locale resolution."""

from __future__ import annotations

from fastapi import Request

from app.core.i18n import SupportedLocale, get_current_locale


def get_locale(request: Request) -> SupportedLocale:
    """Dependency injecting the active request's validated SupportedLocale."""
    if hasattr(request.state, "locale") and isinstance(request.state.locale, SupportedLocale):
        return request.state.locale
    return get_current_locale()
