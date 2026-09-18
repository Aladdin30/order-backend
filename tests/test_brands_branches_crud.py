"""Test suite for Brand and nested Branch administrative CRUD and RBAC."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import AsyncGenerator
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.api.deps import get_async_db
from app.core.security import create_access_token, get_password_hash
from app.main import create_app
from app.models.auth import Base, Branch, Tenant, User, UserBranchAccess
from app.models.brand import Brand
from app.models.enums import UserRole


@pytest_asyncio.fixture(scope="function")
async def async_test_engine() -> AsyncGenerator[AsyncEngine, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(Brand.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Brand.metadata.drop_all)
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture(scope="function")
async def test_session(async_test_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    session_factory = async_sessionmaker(bind=async_test_engine, class_=AsyncSession, expire_on_commit=False)
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
    transport = ASGITransport(app=app)
    with patch("app.core.database.async_session_factory", session_factory), \
         patch("app.services.audit_service.async_session_factory", session_factory):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    app.dependency_overrides.clear()


@pytest_asyncio.fixture(scope="function")
async def seed_data(test_session: AsyncSession):
    tenant = Tenant(name="Global Food Group", slug="global-food", is_active=True)
    test_session.add(tenant)
    await test_session.flush()

    brand_1 = Brand(name="Burger King", slug="burger-king", is_active=True)
    brand_2 = Brand(name="Pizza Hut", slug="pizza-hut", is_active=True)
    test_session.add_all([brand_1, brand_2])
    await test_session.flush()

    branch_1 = Branch(
        tenant_id=tenant.id,
        brand_id=brand_1.id,
        name={"en": "Downtown", "ar": "وسط البلد"},
        slug="downtown",
        latitude=Decimal("30.0444"),
        longitude=Decimal("31.2357"),
        geofence_radius_meters=150,
        is_active=True,
    )
    test_session.add(branch_1)
    await test_session.flush()

    # Users
    super_admin = User(
        tenant_id=tenant.id,
        email="superadmin@test.com",
        full_name="Super Admin",
        hashed_password=get_password_hash("Secret123!"),
        role=UserRole.SUPER_ADMIN,
        is_active=True,
    )
    brand_1_admin = User(
        tenant_id=tenant.id,
        brand_id=brand_1.id,
        email="brand1admin@test.com",
        full_name="Brand 1 Admin",
        hashed_password=get_password_hash("Secret123!"),
        role=UserRole.BRAND_ADMIN,
        is_active=True,
    )
    branch_admin = User(
        tenant_id=tenant.id,
        branch_id=branch_1.id,
        email="branchadmin@test.com",
        full_name="Branch Admin",
        hashed_password=get_password_hash("Secret123!"),
        role=UserRole.BRANCH_ADMIN,
        is_active=True,
    )
    test_session.add_all([super_admin, brand_1_admin, branch_admin])
    await test_session.flush()

    test_session.add(UserBranchAccess(user_id=branch_admin.id, branch_id=branch_1.id))
    await test_session.commit()

    return {
        "tenant": tenant,
        "brand_1": brand_1,
        "brand_2": brand_2,
        "branch_1": branch_1,
        "super_admin_token": create_access_token({"sub": str(super_admin.id), "tenant_id": str(tenant.id)}),
        "brand_1_admin_token": create_access_token({"sub": str(brand_1_admin.id), "tenant_id": str(tenant.id)}),
        "branch_admin_token": create_access_token({"sub": str(branch_admin.id), "tenant_id": str(tenant.id)}),
    }


@pytest.mark.asyncio
async def test_create_brand_standalone(test_client: AsyncClient, seed_data: dict):
    """Super admin creates a brand without branches."""
    token = seed_data["super_admin_token"]
    res = await test_client.post(
        "/api/v1/brands",
        json={"name": "Taco Bell", "slug": "taco-bell"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 201
    data = res.json()
    assert data["name"] == "Taco Bell"
    assert data["slug"] == "taco-bell"
    assert data["branches"] == []


@pytest.mark.asyncio
async def test_create_brand_atomic_with_default_branch(test_client: AsyncClient, seed_data: dict):
    """Super admin creates a brand with atomic default branch onboarding."""
    token = seed_data["super_admin_token"]
    payload = {
        "name": "Subway Egypt",
        "slug": "subway-egypt",
        "default_branch": {
            "name": {"en": "Zamalek", "ar": "الزمالك"},
            "slug": "zamalek",
            "currency": "EGP",
            "timezone": "Africa/Cairo",
            "latitude": "30.0600",
            "longitude": "31.2200",
            "geofence_radius_meters": 200,
        },
    }
    res = await test_client.post(
        "/api/v1/brands",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 201
    data = res.json()
    assert data["name"] == "Subway Egypt"
    assert len(data["branches"]) == 1
    branch = data["branches"][0]
    assert branch["slug"] == "zamalek"
    assert branch["currency"] == "EGP"


@pytest.mark.asyncio
async def test_brand_slug_conflict(test_client: AsyncClient, seed_data: dict):
    """Attempt to create a duplicate brand slug returns 409 Conflict."""
    token = seed_data["super_admin_token"]
    res = await test_client.post(
        "/api/v1/brands",
        json={"name": "Duplicate Burger King", "slug": "burger-king"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 409
    assert "BRAND_SLUG_ALREADY_EXISTS" in res.json()["detail"]


@pytest.mark.asyncio
async def test_brand_rbac_isolation(test_client: AsyncClient, seed_data: dict):
    """Brand Admin can access their brand, but gets 403 on another brand. Branch Admin is denied."""
    super_token = seed_data["super_admin_token"]
    brand_1_admin_token = seed_data["brand_1_admin_token"]
    branch_admin_token = seed_data["branch_admin_token"]
    brand_1_id = seed_data["brand_1"].id
    brand_2_id = seed_data["brand_2"].id

    # 1. Super Admin sees all brands
    res_super = await test_client.get("/api/v1/brands", headers={"Authorization": f"Bearer {super_token}"})
    assert res_super.status_code == 200
    assert res_super.json()["total"] >= 2

    # 2. Brand 1 Admin sees only their brand on listing
    res_b1 = await test_client.get("/api/v1/brands", headers={"Authorization": f"Bearer {brand_1_admin_token}"})
    assert res_b1.status_code == 200
    items = res_b1.json()["items"]
    assert len(items) == 1
    assert items[0]["id"] == str(brand_1_id)

    # 3. Brand 1 Admin can get Brand 1 details
    res_b1_detail = await test_client.get(f"/api/v1/brands/{brand_1_id}", headers={"Authorization": f"Bearer {brand_1_admin_token}"})
    assert res_b1_detail.status_code == 200
    assert res_b1_detail.json()["id"] == str(brand_1_id)

    # 4. Brand 1 Admin gets 403 trying to access Brand 2 details
    res_forbidden = await test_client.get(f"/api/v1/brands/{brand_2_id}", headers={"Authorization": f"Bearer {brand_1_admin_token}"})
    assert res_forbidden.status_code == 403

    # 5. Branch Admin is denied access to brand management (403)
    res_branch_admin = await test_client.get("/api/v1/brands", headers={"Authorization": f"Bearer {branch_admin_token}"})
    assert res_branch_admin.status_code == 403


@pytest.mark.asyncio
async def test_update_and_deactivate_brand(test_client: AsyncClient, seed_data: dict):
    """Update brand metadata and soft-delete brand."""
    super_token = seed_data["super_admin_token"]
    brand_1_id = seed_data["brand_1"].id

    # Update
    patch_res = await test_client.patch(
        f"/api/v1/brands/{brand_1_id}",
        json={"name": "Burger King International"},
        headers={"Authorization": f"Bearer {super_token}"},
    )
    assert patch_res.status_code == 200
    assert patch_res.json()["name"] == "Burger King International"

    # Deactivate
    del_res = await test_client.delete(
        f"/api/v1/brands/{brand_1_id}",
        headers={"Authorization": f"Bearer {super_token}"},
    )
    assert del_res.status_code == 200
    assert del_res.json()["status"] == "success"

    # Verify deactivated
    get_res = await test_client.get(f"/api/v1/brands/{brand_1_id}", headers={"Authorization": f"Bearer {super_token}"})
    assert get_res.json()["is_active"] is False


@pytest.mark.asyncio
async def test_nested_branch_creation_and_listing(test_client: AsyncClient, seed_data: dict):
    """Add a branch under a brand and list brand branches."""
    token = seed_data["brand_1_admin_token"]
    brand_1_id = seed_data["brand_1"].id

    # Create nested branch
    res_add = await test_client.post(
        f"/api/v1/brands/{brand_1_id}/branches",
        json={
            "name": {"en": "Maadi", "ar": "المعادي"},
            "slug": "maadi",
            "currency": "EGP",
            "timezone": "Africa/Cairo",
            "latitude": "29.9602",
            "longitude": "31.2569",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res_add.status_code == 201
    branch_data = res_add.json()
    assert branch_data["slug"] == "maadi"
    assert branch_data["brand_id"] == str(brand_1_id)

    # List brand branches
    res_list = await test_client.get(
        f"/api/v1/brands/{brand_1_id}/branches",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res_list.status_code == 200
    branches = res_list.json()
    slugs = [b["slug"] for b in branches]
    assert "downtown" in slugs
    assert "maadi" in slugs


@pytest.mark.asyncio
async def test_branch_profile_endpoints(test_client: AsyncClient, seed_data: dict):
    """Verify GET and PATCH /api/v1/branches/{branch_id}."""
    token = seed_data["super_admin_token"]
    branch_1_id = seed_data["branch_1"].id

    # GET
    res_get = await test_client.get(
        f"/api/v1/branches/{branch_1_id}",
        headers={"Authorization": f"Bearer {token}", "X-Branch-ID": str(branch_1_id)},
    )
    assert res_get.status_code == 200
    assert res_get.json()["slug"] == "downtown"

    # PATCH
    res_patch = await test_client.patch(
        f"/api/v1/branches/{branch_1_id}",
        json={"name": {"en": "Downtown Cairo", "ar": "وسط البلد القاهرة"}},
        headers={"Authorization": f"Bearer {token}", "X-Branch-ID": str(branch_1_id)},
    )
    assert res_patch.status_code == 200
    assert res_patch.json()["name"]["en"] == "Downtown Cairo"


@pytest.mark.asyncio
async def test_route_inspection_completeness():
    """Verify that app.routes exposes at least 60 active endpoints for external introspection."""
    from app.main import app
    routes = [
        f"{','.join(r.methods - {'HEAD', 'OPTIONS'}):10} {r.path}"
        for r in app.routes
        if hasattr(r, "methods")
    ]
    assert len(routes) >= 60, f"Expected at least 60 registered routes, got {len(routes)}"
