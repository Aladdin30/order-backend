"""Dedicated Test Suite for Task BE-4.x: Audit Vulnerabilities Remediation.

Asserts:
1. Checkout 86 Override Enforcement: BranchMenuOverride(is_available=False) rejects with 400 ITEM_UNAVAILABLE.
2. Checkout Price Override Billing: BranchMenuOverride(price_override=140.00) charges 140 instead of base 100.
3. Checkout Specific Branches Enforcement: MenuItemScope.SPECIFIC_BRANCHES without branch override rejects with 404 ITEM_NOT_PERMITTED_FOR_BRANCH.
4. Brand Admin Unassigned & Cross-Brand Access Lockout: Unassigned (brand_id=None) or mismatched brand admin gets 403 ACCESS_FORBIDDEN_BRAND_MISMATCH.
5. Deactivated Branch Order Rejection: Orders on tables belonging to deactivated branches reject with 403 TABLE_INACTIVE.
6. Deactivated Branch QR Verification Rejection: QR verification on deactivated branch tables returns 404 TABLE_NOT_FOUND.
7. Brand Deletion Cascades Table Deactivation: Deleting brand sets Brand, Branch, and Table is_active to False.
8. Brand Slug Integrity Conflict Handling: Duplicate brand slug creation raises 409 BRAND_SLUG_ALREADY_EXISTS.
9. Multi-Tenant Analytics Timezone Bounds: Local branch timezone (Africa/Cairo) correctly aligns to UTC bounds.
10. QR Signature Canonical Normalization: UUID and timestamp normalization prevents delimiter injection.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import AsyncGenerator
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import app.core.database
from app.api.deps import get_async_db, get_db
from app.core.security import create_access_token, get_password_hash
from app.core.session_security import create_guest_session_jwt
from app.main import create_app
from app.models.auth import Base, Branch, Table, Tenant, User, UserBranchAccess
from app.models.brand import Brand
from app.models.catalog import Category, Item
from app.models.enums import (
    KitchenStation,
    MenuItemScope,
    TableStatus,
    UserRole,
)
from app.models.financials import FinancialBase
from app.models.menu import BranchMenuOverride
from app.models.table import TableSession
from app.schemas.brand import BrandCreate
from app.services.analytics_service import AnalyticsService
from app.services.brand_service import BrandService
from app.services.qr_generator_service import QRGeneratorService


@pytest_asyncio.fixture(scope="function")
async def async_test_engine() -> AsyncGenerator[AsyncEngine, None]:
    """In-memory SQLite async engine with all domain, floor, and financial tables created."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(TableSession.metadata.create_all)
        await conn.run_sync(FinancialBase.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(FinancialBase.metadata.drop_all)
        await conn.run_sync(TableSession.metadata.drop_all)
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture(scope="function")
async def test_session(async_test_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    session_factory = async_sessionmaker(
        bind=async_test_engine,
        class_=AsyncSession,
        autoflush=False,
        expire_on_commit=False,
    )
    async with session_factory() as session:
        yield session


@pytest_asyncio.fixture(scope="function")
async def test_client(
    async_test_engine: AsyncEngine,
    test_session: AsyncSession,
) -> AsyncGenerator[AsyncClient, None]:
    app = create_app()
    session_factory = async_sessionmaker(
        bind=async_test_engine,
        class_=AsyncSession,
        autoflush=False,
        expire_on_commit=False,
    )

    async def override_get_async_db() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_async_db] = override_get_async_db
    app.dependency_overrides[get_db] = override_get_async_db

    transport = ASGITransport(app=app)
    with patch("app.core.database.async_session_factory", session_factory), \
         patch("app.api.deps.async_session_factory", session_factory), \
         patch("app.services.audit_service.async_session_factory", session_factory), \
         patch("app.services.analytics_service.redis_pubsub.get_redis_client", AsyncMock(return_value=None)):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client

    app.dependency_overrides.clear()


@pytest_asyncio.fixture(scope="function")
async def seed_remediation_env(test_session: AsyncSession) -> dict:
    """Seed comprehensive hierarchy: Tenant, Brand, 2 Branches, Tables, Items, and Users."""
    tenant = Tenant(name="Enterprise Group", slug="enterprise-grp", is_active=True)
    test_session.add(tenant)
    await test_session.flush()

    brand = Brand(name="Gourmet Burgers", slug="gourmet-burgers", is_active=True)
    test_session.add(brand)
    await test_session.flush()

    other_brand = Brand(name="Pizza Haven", slug="pizza-haven", is_active=True)
    test_session.add(other_brand)
    await test_session.flush()

    branch_1 = Branch(
        tenant_id=tenant.id,
        brand_id=brand.id,
        name={"en": "Branch Downtown", "ar": "فرع وسط البلد"},
        slug="branch-downtown",
        latitude=Decimal("30.0444"),
        longitude=Decimal("31.2357"),
        geofence_radius_meters=200,
        is_active=True,
    )
    branch_2 = Branch(
        tenant_id=tenant.id,
        brand_id=brand.id,
        name={"en": "Branch Mall", "ar": "فرع المول"},
        slug="branch-mall",
        latitude=Decimal("30.0131"),
        longitude=Decimal("31.4320"),
        geofence_radius_meters=200,
        is_active=True,
    )
    deactivated_branch = Branch(
        tenant_id=tenant.id,
        brand_id=brand.id,
        name={"en": "Branch Closed", "ar": "فرع مغلق"},
        slug="branch-closed",
        latitude=Decimal("30.0500"),
        longitude=Decimal("31.2500"),
        geofence_radius_meters=200,
        is_active=False,
    )
    test_session.add_all([branch_1, branch_2, deactivated_branch])
    await test_session.flush()

    # Tables
    session_1_id = uuid.uuid4()
    table_1 = Table(
        branch_id=branch_1.id,
        table_number="T-01",
        capacity=4,
        status=TableStatus.BROWSING,
        current_session_token=str(session_1_id),
        is_active=True,
    )
    session_closed_id = uuid.uuid4()
    table_closed = Table(
        branch_id=deactivated_branch.id,
        table_number="T-CLOSED",
        capacity=2,
        status=TableStatus.BROWSING,
        current_session_token=str(session_closed_id),
        is_active=True,
    )
    test_session.add_all([table_1, table_closed])
    await test_session.flush()

    # Menu Category (scoped to Brand or Branch)
    category = Category(
        branch_id=branch_1.id,
        name={"en": "Burgers", "ar": "برجر"},
        display_order=1,
        station=KitchenStation.HOT_KITCHEN,
        is_active=True,
    )
    test_session.add(category)
    await test_session.flush()

    # Global Item (base price 100)
    item_global = Item(
        category_id=category.id,
        name={"en": "Classic Burger", "ar": "كلاسيك برجر"},
        base_price=Decimal("100.00"),
        station=KitchenStation.HOT_KITCHEN,
        scope=MenuItemScope.ALL_BRANCHES,
        is_available=True,
    )
    # 86 Item (base price 80, but unavailable in Branch 1 via override)
    item_86 = Item(
        category_id=category.id,
        name={"en": "Truffle Burger", "ar": "ترافل برجر"},
        base_price=Decimal("150.00"),
        station=KitchenStation.HOT_KITCHEN,
        scope=MenuItemScope.ALL_BRANCHES,
        is_available=True,
    )
    # Scoped Item (SPECIFIC_BRANCHES, unassigned to Branch 1)
    item_scoped = Item(
        category_id=category.id,
        name={"en": "Airport Exclusive", "ar": "حصري المطار"},
        base_price=Decimal("200.00"),
        station=KitchenStation.HOT_KITCHEN,
        scope=MenuItemScope.SPECIFIC_BRANCHES,
        is_available=True,
    )
    test_session.add_all([item_global, item_86, item_scoped])
    await test_session.flush()

    # Overrides
    # 1. Price override for item_global at branch_1 -> 140.00
    override_price = BranchMenuOverride(
        branch_id=branch_1.id,
        menu_item_id=item_global.id,
        price_override=Decimal("140.00"),
        is_available=True,
    )
    # 2. 86 override for item_86 at branch_1 -> is_available=False
    override_86 = BranchMenuOverride(
        branch_id=branch_1.id,
        menu_item_id=item_86.id,
        is_available=False,
    )
    test_session.add_all([override_price, override_86])

    # Users
    pw_hash = get_password_hash("Password123!")
    user_brand_admin = User(
        tenant_id=tenant.id,
        brand_id=brand.id,
        email="brand_admin@gourmet.com",
        full_name="Brand Admin",
        hashed_password=pw_hash,
        role=UserRole.BRAND_ADMIN,
        is_active=True,
    )
    user_unassigned_brand_admin = User(
        tenant_id=tenant.id,
        brand_id=None,
        email="unassigned_admin@gourmet.com",
        full_name="Unassigned Admin",
        hashed_password=pw_hash,
        role=UserRole.BRAND_ADMIN,
        is_active=True,
    )
    user_super_admin = User(
        tenant_id=tenant.id,
        email="super_admin@gourmet.com",
        full_name="Super Admin",
        hashed_password=pw_hash,
        role=UserRole.SUPER_ADMIN,
        is_active=True,
    )
    test_session.add_all([user_brand_admin, user_unassigned_brand_admin, user_super_admin])
    await test_session.commit()

    # Tokens
    guest_token_branch_1 = create_guest_session_jwt(
        session_id=session_1_id,
        tenant_id=tenant.id,
        branch_id=branch_1.id,
        table_id=table_1.id,
        table_number=table_1.table_number,
        is_presence_verified=True,
    )
    guest_token_closed = create_guest_session_jwt(
        session_id=session_closed_id,
        tenant_id=tenant.id,
        branch_id=deactivated_branch.id,
        table_id=table_closed.id,
        table_number=table_closed.table_number,
        is_presence_verified=True,
    )
    token_brand_admin = create_access_token(
        data={"sub": str(user_brand_admin.id), "tenant_id": str(tenant.id), "role": user_brand_admin.role.value, "brand_id": str(brand.id)}
    )
    token_unassigned_admin = create_access_token(
        data={"sub": str(user_unassigned_brand_admin.id), "tenant_id": str(tenant.id), "role": user_unassigned_brand_admin.role.value, "brand_id": None}
    )
    token_super_admin = create_access_token(
        data={"sub": str(user_super_admin.id), "tenant_id": str(tenant.id), "role": user_super_admin.role.value}
    )

    return {
        "tenant": tenant,
        "brand": brand,
        "other_brand": other_brand,
        "branch_1": branch_1,
        "branch_2": branch_2,
        "deactivated_branch": deactivated_branch,
        "table_1": table_1,
        "table_closed": table_closed,
        "item_global": item_global,
        "item_86": item_86,
        "item_scoped": item_scoped,
        "guest_token_branch_1": guest_token_branch_1,
        "guest_token_closed": guest_token_closed,
        "token_brand_admin": token_brand_admin,
        "token_unassigned_admin": token_unassigned_admin,
        "token_super_admin": token_super_admin,
    }


from app.core.i18n import SYSTEM_MESSAGES, SupportedLocale


def assert_error_key(detail: str, expected_key: str):
    """Assert error detail matches either the raw message key or its localized translation."""
    allowed = {
        expected_key,
        SYSTEM_MESSAGES.get(expected_key, {}).get(SupportedLocale.EN, expected_key),
        SYSTEM_MESSAGES.get(expected_key, {}).get(SupportedLocale.AR, expected_key),
    }
    assert any(expected in detail for expected in allowed), (
        f"Expected key '{expected_key}' (or localized translation) in '{detail}'"
    )


# ===========================================================================
# 1. Checkout Scoped Menu Validation Tests
# ===========================================================================

@pytest.mark.asyncio
async def test_checkout_86_override_unavailable(
    test_client: AsyncClient,
    seed_remediation_env: dict,
):
    """Assert item with BranchMenuOverride(is_available=False) is rejected with 400 ITEM_UNAVAILABLE."""
    token = seed_remediation_env["guest_token_branch_1"]
    item_86 = seed_remediation_env["item_86"]

    res = await test_client.post(
        "/api/v1/orders/checkout",
        json={"items": [{"item_id": str(item_86.id), "quantity": 1, "selected_groups": []}]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 400
    assert_error_key(res.json()["detail"], "ITEM_UNAVAILABLE")


@pytest.mark.asyncio
async def test_checkout_price_override_applied(
    test_client: AsyncClient,
    seed_remediation_env: dict,
):
    """Assert item with BranchMenuOverride(price_override=140.00) charges 140 instead of base 100."""
    token = seed_remediation_env["guest_token_branch_1"]
    item_global = seed_remediation_env["item_global"]

    res = await test_client.post(
        "/api/v1/orders/checkout",
        json={"items": [{"item_id": str(item_global.id), "quantity": 2, "selected_groups": []}]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 201
    body = res.json()
    assert Decimal(str(body["items"][0]["unit_price"])) == Decimal("140.00")
    assert Decimal(str(body["total_amount"])) == Decimal("280.00")


@pytest.mark.asyncio
async def test_checkout_specific_branches_unassigned_rejected(
    test_client: AsyncClient,
    seed_remediation_env: dict,
):
    """Assert item with SPECIFIC_BRANCHES without override at branch is rejected with 404 ITEM_NOT_PERMITTED_FOR_BRANCH."""
    token = seed_remediation_env["guest_token_branch_1"]
    item_scoped = seed_remediation_env["item_scoped"]

    res = await test_client.post(
        "/api/v1/orders/checkout",
        json={"items": [{"item_id": str(item_scoped.id), "quantity": 1, "selected_groups": []}]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 404
    assert res.json()["detail"] == "ITEM_NOT_PERMITTED_FOR_BRANCH"


@pytest.mark.asyncio
async def test_checkout_deactivated_branch_rejected(
    test_client: AsyncClient,
    seed_remediation_env: dict,
):
    """Assert checkout on a table belonging to an inactive branch rejects with 403 TABLE_INACTIVE."""
    token = seed_remediation_env["guest_token_closed"]
    item_global = seed_remediation_env["item_global"]

    res = await test_client.post(
        "/api/v1/orders/checkout",
        json={"items": [{"item_id": str(item_global.id), "quantity": 1, "selected_groups": []}]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 403
    assert_error_key(res.json()["detail"], "TABLE_INACTIVE")


# ===========================================================================
# 2. Brand Hierarchy & Multi-Tenant Access Control Tests
# ===========================================================================

@pytest.mark.asyncio
async def test_brand_admin_unassigned_forbidden(
    test_client: AsyncClient,
    seed_remediation_env: dict,
):
    """Assert BRAND_ADMIN with brand_id=None or brand mismatch cannot access brand endpoints."""
    token_unassigned = seed_remediation_env["token_unassigned_admin"]
    token_brand_admin = seed_remediation_env["token_brand_admin"]
    brand = seed_remediation_env["brand"]
    other_brand = seed_remediation_env["other_brand"]

    # 1. Unassigned brand admin (brand_id=None) accessing any brand -> 403
    res_unassigned = await test_client.get(
        f"/api/v1/brands/{brand.id}",
        headers={"Authorization": f"Bearer {token_unassigned}"},
    )
    assert res_unassigned.status_code == 403
    assert res_unassigned.json()["detail"] == "ACCESS_FORBIDDEN_BRAND_MISMATCH"

    # 2. Brand admin accessing a different brand -> 403
    res_mismatch = await test_client.get(
        f"/api/v1/brands/{other_brand.id}",
        headers={"Authorization": f"Bearer {token_brand_admin}"},
    )
    assert res_mismatch.status_code == 403
    assert res_mismatch.json()["detail"] == "ACCESS_FORBIDDEN_BRAND_MISMATCH"

    # 3. Brand admin accessing their own brand -> 200
    res_own = await test_client.get(
        f"/api/v1/brands/{brand.id}",
        headers={"Authorization": f"Bearer {token_brand_admin}"},
    )
    assert res_own.status_code == 200
    assert res_own.json()["id"] == str(brand.id)


@pytest.mark.asyncio
async def test_brand_slug_duplicate_returns_409_conflict(
    test_session: AsyncSession,
    seed_remediation_env: dict,
):
    """Assert creating a brand with duplicate slug rolls back and raises 409 BRAND_SLUG_ALREADY_EXISTS."""
    tenant = seed_remediation_env["tenant"]
    brand = seed_remediation_env["brand"]
    payload = BrandCreate(name="Duplicate Gourmet", slug=brand.slug)

    with pytest.raises(HTTPException) as exc_info:
        await BrandService.create_brand(tenant.id, payload, test_session)

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "BRAND_SLUG_ALREADY_EXISTS"


@pytest.mark.asyncio
async def test_brand_delete_cascades_table_deactivation(
    test_session: AsyncSession,
    seed_remediation_env: dict,
):
    """Assert deleting a brand deactivates Brand, Branches, and Tables in cascade."""
    tenant = seed_remediation_env["tenant"]
    brand = seed_remediation_env["brand"]
    branch_1 = seed_remediation_env["branch_1"]
    table_1 = seed_remediation_env["table_1"]

    await BrandService.delete_brand(brand.id, tenant.id, test_session)
    await test_session.commit()

    # Re-fetch branch and table
    res_branch = await test_session.execute(select(Branch).where(Branch.id == branch_1.id))
    b = res_branch.scalar_one()
    assert b.is_active is False

    res_table = await test_session.execute(select(Table).where(Table.id == table_1.id))
    t = res_table.scalar_one()
    assert t.is_active is False


# ===========================================================================
# 3. QR Verification & Hardening Tests
# ===========================================================================

@pytest.mark.asyncio
async def test_qr_export_verify_deactivated_branch_rejected(
    test_client: AsyncClient,
    seed_remediation_env: dict,
):
    """Assert QR verification on a table belonging to an inactive branch returns 404 TABLE_NOT_FOUND."""
    deactivated_branch = seed_remediation_env["deactivated_branch"]
    table_closed = seed_remediation_env["table_closed"]

    ts = int(datetime.now(timezone.utc).timestamp())
    sig = QRGeneratorService.compute_signature(table_closed.id, deactivated_branch.id, ts)

    res = await test_client.get(
        "/api/v1/qr-export/verify",
        params={
            "table_id": str(table_closed.id),
            "branch_id": str(deactivated_branch.id),
            "ts": ts,
            "sig": sig,
        },
    )
    assert res.status_code == 404
    assert res.json()["detail"] == "Table not found or inactive within specified branch."



def test_qr_signature_canonical_normalization():
    """Assert UUID and integer canonicalization prevents delimiter injection and string variations."""
    raw_table_id = uuid.uuid4()
    raw_branch_id = uuid.uuid4()
    raw_ts = 1758239000

    sig_canonical = QRGeneratorService.compute_signature(raw_table_id, raw_branch_id, raw_ts)
    sig_str = QRGeneratorService.compute_signature(str(raw_table_id), str(raw_branch_id), str(raw_ts))

    assert sig_canonical == sig_str
    assert len(sig_canonical) == 64


# ===========================================================================
# 4. Analytics Timezone Boundaries Alignment Test
# ===========================================================================

def test_analytics_timezone_bounds_alignment():
    """Assert resolve_time_bounds maps local Egypt calendar dates to correct UTC bounds."""
    start_utc, end_utc = AnalyticsService.resolve_time_bounds("today", "Africa/Cairo")
    assert start_utc.tzinfo == timezone.utc
    assert end_utc.tzinfo == timezone.utc
    assert end_utc >= start_utc
    # In Egypt timezone, midnight local is either 21:00 or 22:00 UTC depending on DST
    assert start_utc.hour in (21, 22)

    start_y, end_y = AnalyticsService.resolve_time_bounds("yesterday", "Africa/Cairo")
    assert start_y.tzinfo == timezone.utc
    assert end_y.tzinfo == timezone.utc
    assert end_y > start_y
    # Yesterday spans full calendar day in Egypt (almost 86400 seconds)
    assert 86399.0 <= (end_y - start_y).total_seconds() <= 86400.0

    start_7d, end_7d = AnalyticsService.resolve_time_bounds("last_7_days", "Africa/Cairo")
    assert start_7d.tzinfo == timezone.utc
    assert end_7d.tzinfo == timezone.utc
    assert (end_7d - start_7d).total_seconds() == 7 * 86400.0

    start_30d, end_30d = AnalyticsService.resolve_time_bounds("last_30_days", "Africa/Cairo")
    assert start_30d.tzinfo == timezone.utc
    assert end_30d.tzinfo == timezone.utc
    assert (end_30d - start_30d).total_seconds() == 30 * 86400.0

