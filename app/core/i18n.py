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
    "GEOLOCATION_REQUIRED": {
        SupportedLocale.AR: "إحداثيات الموقع الجغرافي مطلوبة للتحقق من النطاق الجغرافي للفرع",
        SupportedLocale.EN: "Client geolocation coordinates are required for branch geofence verification",
    },
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
    "SESSION_TERMINATED_TABLE_AVAILABLE": {
        SupportedLocale.AR: "انتهت جلسة الطاولة وأصبحت الطاولة متاحة لضيوف جدد",
        SupportedLocale.EN: "Table session has terminated and the table is now available",
    },
    "INPUT_VALIDATION_FAILED": {
        SupportedLocale.AR: "بيانات الطلب المدخلة غير صحيحة",
        SupportedLocale.EN: "Input validation failed",
    },
    "INTERNAL_SERVER_ERROR": {
        SupportedLocale.AR: "حدث خطأ غير متوقع في النظام",
        SupportedLocale.EN: "An unexpected internal error occurred",
    },
    "ITEM_UNAVAILABLE": {
        SupportedLocale.AR: "العنصر المطلوب غير متوفر حالياً",
        SupportedLocale.EN: "The requested item is currently unavailable",
    },
    "ITEM_NOT_FOUND": {
        SupportedLocale.AR: "عنصر القائمة المطلوب غير موجود",
        SupportedLocale.EN: "The requested menu item was not found",
    },
    "MODIFIER_GROUP_REQUIRED": {
        SupportedLocale.AR: "مجموعة التعديلات هذه مطلوبة ويجب اختيار خيار واحد على الأقل",
        SupportedLocale.EN: "This modifier group is required; at least one option must be selected",
    },
    "MODIFIER_SELECTION_OUT_OF_BOUNDS": {
        SupportedLocale.AR: "عدد الخيارات المحددة خارج النطاق المسموح به للمجموعة",
        SupportedLocale.EN: "The number of selected options is out of the allowed bounds for this group",
    },
    "MODIFIER_OPTION_INVALID": {
        SupportedLocale.AR: "خيار التعديل المحدد غير صالح أو لا ينتمي لهذا العنصر",
        SupportedLocale.EN: "The selected modifier option is invalid or does not belong to this item",
    },
    "MODIFIER_OPTION_UNAVAILABLE": {
        SupportedLocale.AR: "خيار التعديل المحدد غير متوفر حالياً",
        SupportedLocale.EN: "The selected modifier option is currently unavailable",
    },
    "DUPLICATE_MODIFIER_OPTION": {
        SupportedLocale.AR: "لا يمكن تكرار نفس خيار التعديل في نفس المجموعة",
        SupportedLocale.EN: "Duplicate modifier option selections are not allowed",
    },
    "DUPLICATE_MODIFIER_GROUP": {
        SupportedLocale.AR: "لا يمكن تكرار تقديم نفس مجموعة التعديلات",
        SupportedLocale.EN: "Duplicate modifier group selections are not allowed",
    },
    "INVALID_STATE_TRANSITION": {
        SupportedLocale.AR: "الانتقال بين حالات الطلب غير صالح",
        SupportedLocale.EN: "Invalid order status transition",
    },
    "CANCELLATION_RESTRICTED_TO_STAFF": {
        SupportedLocale.AR: "لا يمكن إلغاء الطلب بعد اعتماده إلا من قبل مسؤول الفرع",
        SupportedLocale.EN: "Orders in progress can only be cancelled by a branch administrator",
    },
    "CANCELLATION_REASON_REQUIRED": {
        SupportedLocale.AR: "سبب الإلغاء مطلوب",
        SupportedLocale.EN: "A cancellation reason is required",
    },
    "NO_ACTIVE_ORDER": {
        SupportedLocale.AR: "لا يوجد طلب نشط لهذه الطاولة",
        SupportedLocale.EN: "No active order found for this table",
    },
    "ORDER_NOT_FOUND": {
        SupportedLocale.AR: "الطلب غير موجود",
        SupportedLocale.EN: "The requested order was not found",
    },
    "EMPTY_ORDER": {
        SupportedLocale.AR: "لا يمكن تقديم طلب فارغ بدون عناصر",
        SupportedLocale.EN: "Cannot checkout an empty order without items",
    },
    "ACTIVE_REQUEST_EXISTS": {
        SupportedLocale.AR: "يوجد طلب خدمة نشط من هذا النوع مسبقاً لهذه الطاولة",
        SupportedLocale.EN: "An active service request of this type already exists for this table",
    },
    "SERVICE_REQUEST_COOLDOWN": {
        SupportedLocale.AR: "يرجى الانتظار 60 ثانية قبل إرسال طلب خدمة آخر من هذا النوع",
        SupportedLocale.EN: "Please wait 60 seconds before submitting another service request of this type",
    },
    "SERVICE_REQUEST_NOT_FOUND": {
        SupportedLocale.AR: "طلب الخدمة غير موجود",
        SupportedLocale.EN: "The requested service request was not found",
    },
    "INVALID_SERVICE_REQUEST_TRANSITION": {
        SupportedLocale.AR: "تغيير حالة طلب الخدمة غير صالح",
        SupportedLocale.EN: "Invalid service request status transition",
    },
    "ORDER_ALREADY_PAID": {
        SupportedLocale.AR: "تم سداد قيمة هذا الطلب بالكامل مسبقاً",
        SupportedLocale.EN: "This order has already been fully settled",
    },
    "PAYMENT_AMOUNT_INVALID": {
        SupportedLocale.AR: "مبلغ السداد غير صالح أو يتجاوز الرصيد المتبقي",
        SupportedLocale.EN: "Payment amount must be greater than zero and cannot exceed the pending balance",
    },
    "PAYMENT_NOT_FOUND": {
        SupportedLocale.AR: "عملية الدفع غير موجودة",
        SupportedLocale.EN: "The requested payment transaction was not found",
    },
    "PAYMENT_ALREADY_SETTLED": {
        SupportedLocale.AR: "عملية الدفع تمت تسويتها مسبقاً",
        SupportedLocale.EN: "This payment transaction is already settled",
    },
    "INVALID_WEBHOOK_SIGNATURE": {
        SupportedLocale.AR: "توقيع الويب هوك غير صالح",
        SupportedLocale.EN: "Invalid webhook cryptographic signature",
    },
    "ACTIVE_OFFLINE_PAYMENT_EXISTS": {
        SupportedLocale.AR: "يوجد طلب سداد نقدي أو عبر نقطة البيع قيد المعالجة مسبقاً لهذه الطاولة",
        SupportedLocale.EN: "A pending cash/POS settlement request already exists for this table",
    },
    "ORDER_CANCELLED_CANNOT_PAY": {
        SupportedLocale.AR: "لا يمكن سداد طلب تم إلغاؤه",
        SupportedLocale.EN: "Cannot process payment for a cancelled order",
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
