"""Internationalization (i18n) schemas: LocalizedField and dynamic LocalizedStr."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from app.core.i18n import get_current_locale


class LocalizedField(BaseModel):
    """Bilingual input field strictly enforcing English and Arabic values for admin schemas."""

    en: str = Field(..., min_length=1, description="English text representation")
    ar: str = Field(..., min_length=1, description="Arabic text representation")

    model_config = ConfigDict(frozen=True)

    def to_dict(self) -> dict[str, str]:
        """Export as plain dictionary matching PostgreSQL JSONB storage."""
        return {"en": self.en, "ar": self.ar}


def resolve_localized_string(value: Any) -> str:
    """Resolve a bilingual dictionary or string into a single localized string.

    Fallback hierarchy:
    requested locale -> alternative locale -> first available non-empty -> empty string.
    """
    if value is None:
        return ""

    if isinstance(value, LocalizedField):
        value = {"en": value.en, "ar": value.ar}

    if isinstance(value, dict):
        current_locale = get_current_locale()
        target_key = current_locale.value
        alt_key = "en" if target_key == "ar" else "ar"

        # 1. Requested locale
        target_val = value.get(target_key)
        if target_val is not None and str(target_val).strip():
            return str(target_val).strip()

        # 2. Alternative locale
        alt_val = value.get(alt_key)
        if alt_val is not None and str(alt_val).strip():
            return str(alt_val).strip()

        # 3. Any available non-empty string in dict
        for v in value.values():
            if v is not None and str(v).strip():
                return str(v).strip()

        # 4. Empty string fallback
        return ""

    return str(value)


def resolve_optional_localized_string(value: Any) -> str | None:
    """Resolve optional bilingual dictionary or string, preserving None."""
    if value is None:
        return None
    return resolve_localized_string(value)


# Dynamic localized string for customer read models
LocalizedStr = Annotated[str, BeforeValidator(resolve_localized_string)]
OptionalLocalizedStr = Annotated[str | None, BeforeValidator(resolve_optional_localized_string)]
