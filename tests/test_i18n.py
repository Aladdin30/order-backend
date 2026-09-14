"""Comprehensive test suite for i18n middleware, dynamic Pydantic serialization, and exception handlers."""

import asyncio
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from fastapi import APIRouter, Depends, HTTPException, Query
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, ValidationError

from app.api.deps_i18n import get_locale
from app.core.i18n import (
    DEFAULT_LOCALE,
    SYSTEM_MESSAGES,
    SupportedLocale,
    get_current_locale,
    get_localized_message,
    parse_accept_language,
    reset_current_locale,
    set_current_locale,
)
from app.main import create_app
from app.schemas.i18n import LocalizedField, LocalizedStr, OptionalLocalizedStr


# ---------------------------------------------------------------------------
# Dummy Pydantic Models for Serialization Tests
# ---------------------------------------------------------------------------

class CustomerMenuItemResponse(BaseModel):
    """Customer-facing read model flattening bilingual JSONB into localized string."""

    name: LocalizedStr
    description: OptionalLocalizedStr = None


class AdminMenuItemCreate(BaseModel):
    """Admin-facing write model strictly enforcing bilingual English & Arabic inputs."""

    name: LocalizedField
    description: LocalizedField | None = None


# ---------------------------------------------------------------------------
# 1. RFC 9110 Accept-Language Parser Unit Tests
# ---------------------------------------------------------------------------

class TestAcceptLanguageParser:
    """Rigorous tests for RFC 9110 Accept-Language header parsing and quality factor resolution."""

    def test_default_fallback_on_none_and_empty(self) -> None:
        """Missing, empty, or whitespace-only headers must gracefully resolve to Arabic (default)."""
        assert parse_accept_language(None) == SupportedLocale.AR
        assert parse_accept_language("") == SupportedLocale.AR
        assert parse_accept_language("   ") == SupportedLocale.AR

    def test_simple_language_tags(self) -> None:
        """Basic ISO 639-1 language tags and regional variants."""
        assert parse_accept_language("ar") == SupportedLocale.AR
        assert parse_accept_language("en") == SupportedLocale.EN
        assert parse_accept_language("ar-SA") == SupportedLocale.AR
        assert parse_accept_language("ar-EG") == SupportedLocale.AR
        assert parse_accept_language("en-US") == SupportedLocale.EN
        assert parse_accept_language("en-GB") == SupportedLocale.EN
        assert parse_accept_language("EN-us") == SupportedLocale.EN

    def test_quality_factors_priority(self) -> None:
        """Quality factor weights (q-values) must dictate language selection order."""
        # Arabic higher priority
        header_ar_pref = "ar-EG,ar;q=0.9,en-US;q=0.8,en;q=0.7"
        assert parse_accept_language(header_ar_pref) == SupportedLocale.AR

        # English higher priority
        header_en_pref = "en-US,en;q=0.9,ar;q=0.8"
        assert parse_accept_language(header_en_pref) == SupportedLocale.EN

        # Explicit q comparison
        assert parse_accept_language("en;q=0.6,ar;q=0.9") == SupportedLocale.AR
        assert parse_accept_language("ar;q=0.4,en;q=0.85") == SupportedLocale.EN

    def test_unsupported_languages_fallback(self) -> None:
        """Unsupported languages should be discarded and fallback appropriately."""
        # Only unsupported languages -> default AR
        assert parse_accept_language("fr-FR,fr;q=0.9,de;q=0.8,es;q=0.7") == SupportedLocale.AR

        # Unsupported language with higher q, but supported English with lower q -> selects English
        assert parse_accept_language("fr-FR;q=0.95,en-US;q=0.7") == SupportedLocale.EN

        # Unsupported language with higher q, but supported Arabic with lower q -> selects Arabic
        assert parse_accept_language("de-DE;q=0.99,ar-SA;q=0.6") == SupportedLocale.AR

    def test_zero_or_negative_quality_discarded(self) -> None:
        """Languages with q=0 or negative weight must not be selected."""
        assert parse_accept_language("en;q=0,ar;q=0.5") == SupportedLocale.AR
        assert parse_accept_language("ar;q=0.0,en;q=0.0") == SupportedLocale.AR

    def test_wildcard_language_tag(self) -> None:
        """Wildcard '*' tag maps to system default."""
        assert parse_accept_language("*") == SupportedLocale.AR
        assert parse_accept_language("fr;q=0.9,*;q=0.5") == SupportedLocale.AR
        assert parse_accept_language("en;q=0.9,*;q=0.2") == SupportedLocale.EN

    def test_malformed_header_resilience(self) -> None:
        """Malformed or semi-corrupted headers should not crash the parser."""
        assert parse_accept_language(";;;,invalid-tag,en") == SupportedLocale.EN
        assert parse_accept_language("ar;q=not_a_float,en;q=0.5") == SupportedLocale.AR


# ---------------------------------------------------------------------------
# 2. Async ContextVar Management Tests
# ---------------------------------------------------------------------------

class TestContextVarManagement:
    """Verify async-safety and task isolation of the locale ContextVar."""

    def test_set_and_reset_locale(self) -> None:
        """Setting locale returns token and resetting restores original state."""
        original_locale = get_current_locale()
        token = set_current_locale(SupportedLocale.EN)
        assert get_current_locale() == SupportedLocale.EN

        reset_current_locale(token)
        assert get_current_locale() == original_locale

    def test_string_coercion_in_set_locale(self) -> None:
        """Setting locale via string (case-insensitive) works seamlessly."""
        token = set_current_locale("en")
        assert get_current_locale() == SupportedLocale.EN
        reset_current_locale(token)

        token = set_current_locale("AR")
        assert get_current_locale() == SupportedLocale.AR
        reset_current_locale(token)

        # Invalid string defaults to AR
        token = set_current_locale("invalid_lang")
        assert get_current_locale() == SupportedLocale.AR
        reset_current_locale(token)

    @pytest.mark.asyncio
    async def test_coroutine_task_isolation(self) -> None:
        """Verify concurrent coroutines maintain independent locale contexts."""

        async def worker_ar():
            token = set_current_locale(SupportedLocale.AR)
            await asyncio.sleep(0.01)
            locale = get_current_locale()
            reset_current_locale(token)
            return locale

        async def worker_en():
            token = set_current_locale(SupportedLocale.EN)
            await asyncio.sleep(0.01)
            locale = get_current_locale()
            reset_current_locale(token)
            return locale

        # Run concurrent tasks
        results = await asyncio.gather(
            worker_ar(),
            worker_en(),
            worker_ar(),
            worker_en(),
        )

        assert results == [
            SupportedLocale.AR,
            SupportedLocale.EN,
            SupportedLocale.AR,
            SupportedLocale.EN,
        ]


# ---------------------------------------------------------------------------
# 3. Dynamic Pydantic v2 Localized Serialization Tests
# ---------------------------------------------------------------------------

class TestDynamicPydanticSerialization:
    """Test LocalizedStr dynamic flattening and LocalizedField strict validation."""

    def test_localized_str_arabic_context(self) -> None:
        """In Arabic context, LocalizedStr flattens JSONB dict to Arabic string."""
        token = set_current_locale(SupportedLocale.AR)
        try:
            item = CustomerMenuItemResponse(
                name={"en": "Truffle Burger", "ar": "برجر الكمأة"},
                description={"en": "Delicious beef burger", "ar": "برجر لحم شهي"},
            )
            assert item.name == "برجر الكمأة"
            assert item.description == "برجر لحم شهي"
        finally:
            reset_current_locale(token)

    def test_localized_str_english_context(self) -> None:
        """In English context, LocalizedStr flattens JSONB dict to English string."""
        token = set_current_locale(SupportedLocale.EN)
        try:
            item = CustomerMenuItemResponse(
                name={"en": "Truffle Burger", "ar": "برجر الكمأة"},
                description={"en": "Delicious beef burger", "ar": "برجر لحم شهي"},
            )
            assert item.name == "Truffle Burger"
            assert item.description == "Delicious beef burger"
        finally:
            reset_current_locale(token)

    def test_localized_str_fallback_hierarchy(self) -> None:
        """Fallback hierarchy: requested locale -> alternative locale -> empty string."""
        # Scenario A: In English context, English text is empty -> fallback to Arabic
        token = set_current_locale(SupportedLocale.EN)
        try:
            item_en_missing = CustomerMenuItemResponse(
                name={"en": "", "ar": "شاورما دجاج"},
            )
            assert item_en_missing.name == "شاورما دجاج"
        finally:
            reset_current_locale(token)

        # Scenario B: In Arabic context, Arabic text is whitespace -> fallback to English
        token = set_current_locale(SupportedLocale.AR)
        try:
            item_ar_missing = CustomerMenuItemResponse(
                name={"en": "Chicken Shawarma", "ar": "   "},
            )
            assert item_ar_missing.name == "Chicken Shawarma"
        finally:
            reset_current_locale(token)

        # Scenario C: Both empty -> empty string
        token = set_current_locale(SupportedLocale.EN)
        try:
            item_both_empty = CustomerMenuItemResponse(
                name={"en": "", "ar": ""},
            )
            assert item_both_empty.name == ""
        finally:
            reset_current_locale(token)

    def test_localized_str_from_localized_field_instance(self) -> None:
        """Feeding LocalizedField into LocalizedStr resolves correctly."""
        admin_field = LocalizedField(en="Grilled Salmon", ar="سلمون مشوي")

        token = set_current_locale(SupportedLocale.AR)
        try:
            item = CustomerMenuItemResponse(name=admin_field)
            assert item.name == "سلمون مشوي"
        finally:
            reset_current_locale(token)

        token = set_current_locale(SupportedLocale.EN)
        try:
            item = CustomerMenuItemResponse(name=admin_field)
            assert item.name == "Grilled Salmon"
        finally:
            reset_current_locale(token)

    def test_localized_str_optional_none_preservation(self) -> None:
        """OptionalLocalizedStr correctly preserves None when input is None."""
        item = CustomerMenuItemResponse(
            name={"en": "Coffee", "ar": "قهوة"},
            description=None,
        )
        assert item.description is None

    def test_localized_field_admin_validation(self) -> None:
        """LocalizedField strictly enforces bilingual presence and rejects incomplete inputs."""
        # Valid input
        valid_field = LocalizedField(en="Salad", ar="سلطة")
        assert valid_field.en == "Salad"
        assert valid_field.ar == "سلطة"
        assert valid_field.to_dict() == {"en": "Salad", "ar": "سلطة"}

        # Missing 'ar'
        with pytest.raises(ValidationError):
            LocalizedField.model_validate({"en": "Salad"})

        # Missing 'en'
        with pytest.raises(ValidationError):
            LocalizedField.model_validate({"ar": "سلطة"})

        # Empty string in 'en'
        with pytest.raises(ValidationError):
            LocalizedField(en="", ar="سلطة")


# ---------------------------------------------------------------------------
# 4. System Messages Catalog Unit Tests
# ---------------------------------------------------------------------------

class TestSystemMessagesCatalog:
    """Ensure all required system message keys exist in Arabic and English."""

    @pytest.mark.parametrize(
        "key",
        [
            "OUT_OF_GEOFENCE",
            "TABLE_INACTIVE",
            "UNAUTHORIZED",
            "ENTITY_NOT_FOUND",
            "TOKEN_INVALID",
            "TOKEN_EXPIRED",
            "INPUT_VALIDATION_FAILED",
            "INTERNAL_SERVER_ERROR",
        ],
    )
    def test_system_message_keys_exist_in_both_languages(self, key: str) -> None:
        ar_msg = get_localized_message(key, locale=SupportedLocale.AR)
        en_msg = get_localized_message(key, locale=SupportedLocale.EN)

        assert ar_msg != key, f"Key {key} missing Arabic translation"
        assert en_msg != key, f"Key {key} missing English translation"
        assert ar_msg != en_msg, f"Translations for {key} must be distinct between AR and EN"

    def test_unknown_key_passthrough(self) -> None:
        """Unknown keys are passed through unchanged."""
        assert get_localized_message("UNKNOWN_CUSTOM_KEY") == "UNKNOWN_CUSTOM_KEY"


# ---------------------------------------------------------------------------
# 5. Middleware & API Endpoints Integration Tests
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture(scope="function")
async def i18n_test_client() -> AsyncGenerator[AsyncClient, None]:
    """FastAPI test client with sample i18n routes attached."""
    app = create_app()

    test_router = APIRouter(prefix="/api/v1/test-i18n", tags=["test-i18n"])

    # 1. Customer read endpoint with LocalizedStr
    @test_router.get("/menu-item", response_model=CustomerMenuItemResponse)
    async def get_menu_item(locale: SupportedLocale = Depends(get_locale)):
        return {
            "name": {"en": "Crispy Calamari", "ar": "كالاماري مقرمش"},
            "description": {"en": "Served with tartar sauce", "ar": "يقدم مع صلصة التارتار"},
        }

    # 2. Admin write endpoint with LocalizedField
    @test_router.post("/menu-item")
    async def create_menu_item(data: AdminMenuItemCreate):
        return {"status": "created", "stored_name": data.name.to_dict()}

    # 3. Known system error endpoint
    @test_router.get("/error-geofence")
    async def error_geofence():
        raise HTTPException(status_code=403, detail="OUT_OF_GEOFENCE")

    # 4. Unknown custom error endpoint
    @test_router.get("/error-custom")
    async def error_custom():
        raise HTTPException(status_code=400, detail="Custom uncatalogued error message")

    app.include_router(test_router)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest.mark.asyncio
class TestI18nMiddlewareAndEndpoints:
    """Integration tests verifying I18nMiddleware, header injection, and localized responses."""

    async def test_default_request_resolves_arabic(self, i18n_test_client: AsyncClient) -> None:
        """Requests without headers or query parameters default to Arabic."""
        resp = await i18n_test_client.get("/api/v1/test-i18n/menu-item")
        assert resp.status_code == 200
        assert resp.headers["Content-Language"] == "ar"
        data = resp.json()
        assert data["name"] == "كالاماري مقرمش"
        assert data["description"] == "يقدم مع صلصة التارتار"

    async def test_accept_language_english_header(self, i18n_test_client: AsyncClient) -> None:
        """Accept-Language: en resolves to English and serializes English strings."""
        resp = await i18n_test_client.get(
            "/api/v1/test-i18n/menu-item",
            headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        assert resp.status_code == 200
        assert resp.headers["Content-Language"] == "en"
        data = resp.json()
        assert data["name"] == "Crispy Calamari"
        assert data["description"] == "Served with tartar sauce"

    async def test_accept_language_arabic_weighted_header(self, i18n_test_client: AsyncClient) -> None:
        """Accept-Language with Arabic priority resolves to Arabic."""
        resp = await i18n_test_client.get(
            "/api/v1/test-i18n/menu-item",
            headers={"Accept-Language": "ar-SA,ar;q=0.9,en;q=0.8"},
        )
        assert resp.status_code == 200
        assert resp.headers["Content-Language"] == "ar"
        data = resp.json()
        assert data["name"] == "كالاماري مقرمش"

    async def test_query_parameter_overrides_header(self, i18n_test_client: AsyncClient) -> None:
        """Query parameter ?lang=en must override Accept-Language: ar header."""
        resp = await i18n_test_client.get(
            "/api/v1/test-i18n/menu-item?lang=en",
            headers={"Accept-Language": "ar-EG,ar;q=0.9"},
        )
        assert resp.status_code == 200
        assert resp.headers["Content-Language"] == "en"
        data = resp.json()
        assert data["name"] == "Crispy Calamari"

    async def test_localized_http_exception_arabic(self, i18n_test_client: AsyncClient) -> None:
        """HTTPException with catalog key returns Arabic translation and Content-Language."""
        resp = await i18n_test_client.get(
            "/api/v1/test-i18n/error-geofence",
            headers={"Accept-Language": "ar"},
        )
        assert resp.status_code == 403
        assert resp.headers["Content-Language"] == "ar"
        data = resp.json()
        assert data["detail"] == SYSTEM_MESSAGES["OUT_OF_GEOFENCE"][SupportedLocale.AR]

    async def test_localized_http_exception_english(self, i18n_test_client: AsyncClient) -> None:
        """HTTPException with catalog key returns English translation and Content-Language."""
        resp = await i18n_test_client.get(
            "/api/v1/test-i18n/error-geofence",
            headers={"Accept-Language": "en"},
        )
        assert resp.status_code == 403
        assert resp.headers["Content-Language"] == "en"
        data = resp.json()
        assert data["detail"] == SYSTEM_MESSAGES["OUT_OF_GEOFENCE"][SupportedLocale.EN]

    async def test_uncatalogued_http_exception_passthrough(self, i18n_test_client: AsyncClient) -> None:
        """Uncatalogued error details pass through verbatim while keeping Content-Language."""
        resp = await i18n_test_client.get(
            "/api/v1/test-i18n/error-custom",
            headers={"Accept-Language": "en"},
        )
        assert resp.status_code == 400
        assert resp.headers["Content-Language"] == "en"
        assert resp.json()["detail"] == "Custom uncatalogued error message"

    async def test_request_validation_error_arabic_envelope(self, i18n_test_client: AsyncClient) -> None:
        """Validation errors in Arabic context return localized envelope message."""
        # Send invalid payload (empty name)
        resp = await i18n_test_client.post(
            "/api/v1/test-i18n/menu-item",
            headers={"Accept-Language": "ar"},
            json={"name": {"en": "Only English"}},  # missing 'ar'
        )
        assert resp.status_code == 422
        assert resp.headers["Content-Language"] == "ar"
        data = resp.json()
        assert data["detail"] == "بيانات الطلب المدخلة غير صحيحة"
        assert isinstance(data["errors"], list)
        assert len(data["errors"]) > 0

    async def test_request_validation_error_english_envelope(self, i18n_test_client: AsyncClient) -> None:
        """Validation errors in English context return localized envelope message."""
        resp = await i18n_test_client.post(
            "/api/v1/test-i18n/menu-item",
            headers={"Accept-Language": "en"},
            json={"name": {"ar": "فقط عربي"}},  # missing 'en'
        )
        assert resp.status_code == 422
        assert resp.headers["Content-Language"] == "en"
        data = resp.json()
        assert data["detail"] == "Input validation failed"
        assert isinstance(data["errors"], list)
        assert len(data["errors"]) > 0
