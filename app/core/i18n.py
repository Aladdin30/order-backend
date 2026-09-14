"""Internationalization (i18n) core primitives: locales, ContextVar, RFC 9110 parser, and messages."""

from __future__ import annotations

import re
from contextvars import ContextVar, Token
from enum import StrEnum
from typing import NamedTuple


class SupportedLocale(StrEnum):
    """Supported system locales."""

    AR = "ar"  # Arabic (Primary default)
    EN = "en"  # English


DEFAULT_LOCALE = SupportedLocale.AR

# Async/thread-safe locale context for per-request isolation
_current_locale: ContextVar[SupportedLocale] = ContextVar("current_locale", default=DEFAULT_LOCALE)


def get_current_locale() -> SupportedLocale:
    """Retrieve the currently active locale for the async context."""
    return _current_locale.get()


def set_current_locale(locale: SupportedLocale | str) -> Token[SupportedLocale]:
    """Set the active locale for the current async task and return its reset token."""
    if isinstance(locale, str):
        try:
            resolved_locale = SupportedLocale(locale.lower())
        except ValueError:
            resolved_locale = DEFAULT_LOCALE
    else:
        resolved_locale = locale
    return _current_locale.set(resolved_locale)


def reset_current_locale(token: Token[SupportedLocale]) -> None:
    """Reset the locale context back to the state represented by the token."""
    _current_locale.reset(token)


class _ParsedLanguage(NamedTuple):
    locale: SupportedLocale
    q: float
    index: int


def parse_accept_language(header: str | None) -> SupportedLocale:
    """Parse RFC 9110 Accept-Language header supporting quality factors (q-values).

    Example header: "ar-EG,ar;q=0.9,en-US;q=0.8,en;q=0.7" -> SupportedLocale.AR
    Fallback behavior: Defaults to Arabic (ar) if missing, malformed, or unsupported.
    """
    if not header or not header.strip():
        return DEFAULT_LOCALE

    candidates: list[_ParsedLanguage] = []

    for index, item in enumerate(header.split(",")):
        item = item.strip()
        if not item:
            continue

        parts = item.split(";")
        raw_tag = parts[0].strip().lower()
        if not raw_tag:
            continue

        # Default q-value is 1.0 according to RFC 9110
        q_value = 1.0
        for param in parts[1:]:
            param = param.strip().lower()
            if param.startswith("q="):
                try:
                    q_value = float(param[2:].strip())
                except ValueError:
                    q_value = 1.0
                break

        # Discard zero or negative weight languages
        if q_value <= 0.0:
            continue

        # Extract primary language subtag (e.g., "ar-EG" -> "ar")
        primary_tag = raw_tag.split("-")[0].strip()

        matched_locale: SupportedLocale | None = None
        if raw_tag == "ar" or primary_tag == "ar":
            matched_locale = SupportedLocale.AR
        elif raw_tag == "en" or primary_tag == "en":
            matched_locale = SupportedLocale.EN
        elif raw_tag == "*":
            # Wildcard tag matches default locale with its specified weight
            matched_locale = DEFAULT_LOCALE

        if matched_locale is not None:
            candidates.append(_ParsedLanguage(locale=matched_locale, q=q_value, index=index))

    if not candidates:
        return DEFAULT_LOCALE

    # Sort primarily by quality descending, secondarily by appearance index ascending
    candidates.sort(key=lambda c: (-c.q, c.index))
    return candidates[0].locale


# Centralized system and exception message catalog
SYSTEM_MESSAGES: dict[str, dict[SupportedLocale, str]] = {
    "OUT_OF_GEOFENCE": {
        SupportedLocale.AR: "أنت خارج النطاق الجغرافي المسموح به للفرع",
        SupportedLocale.EN: "You are outside the permitted branch geofence radius",
    },
    "TABLE_INACTIVE": {
        SupportedLocale.AR: "الطاولة أو الفرع غير متاح حالياً",
        SupportedLocale.EN: "The table or branch is not currently active",
    },
    "UNAUTHORIZED": {
        SupportedLocale.AR: "غير مصرح لك بالوصول إلى هذه العملية",
        SupportedLocale.EN: "You are not authorized to perform this operation",
    },
    "ENTITY_NOT_FOUND": {
        SupportedLocale.AR: "العنصر المطلوب غير موجود",
        SupportedLocale.EN: "The requested entity was not found",
    },
    "TOKEN_INVALID": {
        SupportedLocale.AR: "رمز التحقق غير صالح أو تالف",
        SupportedLocale.EN: "Verification token is invalid or corrupted",
    },
    "TOKEN_EXPIRED": {
        SupportedLocale.AR: "انتهت صلاحية الرمز",
        SupportedLocale.EN: "Token has expired",
    },
    "INPUT_VALIDATION_FAILED": {
        SupportedLocale.AR: "بيانات الطلب المدخلة غير صحيحة",
        SupportedLocale.EN: "Input validation failed",
    },
    "INTERNAL_SERVER_ERROR": {
        SupportedLocale.AR: "حدث خطأ غير متوقع في النظام",
        SupportedLocale.EN: "An unexpected internal error occurred",
    },
}


def get_localized_message(
    key: str,
    locale: SupportedLocale | None = None,
    **kwargs: str,
) -> str:
    """Retrieve localized message for a key from catalog with fallback."""
    target_locale = locale or get_current_locale()
    translations = SYSTEM_MESSAGES.get(key)
    if not translations:
        return key.format(**kwargs) if kwargs else key

    message = translations.get(target_locale) or translations.get(DEFAULT_LOCALE, key)
    return message.format(**kwargs) if kwargs else message
