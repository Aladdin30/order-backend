"""Automated tests for Branch GPS coordinates, Geofence radius in meters, and dynamic financials."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import app.core.database  # Registers SQLite JSONB compiler extension
from app.api.deps import get_async_db
from app.core.security import create_access_token, get_password_hash
from app.main import create_app
from app.models.auth import Base, Branch, Tenant, User, UserBranchAccess
from app.models.enums import UserRole


@pytest_asyncio.fixture(scope="function")
async def async_test_engine() -> AsyncGenerator[AsyncEngine, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture(scope="function")
async def test_session(async_test_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    session_factory = async_sessionmaker(bind=async_test_engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session


@pytest_asyncio.fixture(scope="function")
async def test_client(test_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    app = create_app()
    app.dependency_overrides[get_async_db] = lambda: test_session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client
    app.dependency_overrides.clear()


@pytest_asyncio.fixture(scope="function")
async def seed_data(test_session: AsyncSession) -> dict:
    tenant = Tenant(name="Gourmet Burger", slug="gourmet-burger", is_active=True)
    test_session.add(tenant)
    await test_session.flush()

    branch = Branch(
        tenant_id=tenant.id,
        name={"en": "Olaya Branch", "ar": "فرع العليا"},
        slug="olaya",
        latitude=Decimal("24.7136000"),
        longitude=Decimal("46.6753000"),
        geofence_radius_meters=150,
        tax_rate=Decimal("0.1500"),
        service_fee_rate=Decimal("0.0000"),
        is_service_taxable=False,
        is_tax_inclusive=False,
        service_fee_dine_in_only=True,
        is_active=True,
    )
    test_session.add(branch)
    await test_session.flush()

    pw_hash = get_password_hash("Password123!")
    admin_user = User(
        tenant_id=tenant.id,
        email="branchadmin@gourmet.com",
        hashed_password=pw_hash,
        full_name="Branch Manager",
        role=UserRole.BRANCH_ADMIN,
        is_active=True,
    )
    cashier_user = User(
        tenant_id=tenant.id,
        email="cashier@gourmet.com",
        hashed_password=pw_hash,
        full_name="Cashier John",
        role=UserRole.CASHIER,
        is_active=True,
    )
    test_session.add_all([admin_user, cashier_user])
    await test_session.flush()

    test_session.add_all([
        UserBranchAccess(user_id=admin_user.id, branch_id=branch.id),
        UserBranchAccess(user_id=cashier_user.id, branch_id=branch.id),
    ])
    await test_session.commit()

    admin_token = create_access_token({
        "sub": str(admin_user.id),
        "tenant_id": str(tenant.id),
        "role": UserRole.BRANCH_ADMIN.value,
        "email": admin_user.email,
    })
    cashier_token = create_access_token({
        "sub": str(cashier_user.id),
        "tenant_id": str(tenant.id),
        "role": UserRole.CASHIER.value,
        "email": cashier_user.email,
    })

    return {
        "tenant": tenant,
        "branch": branch,
        "admin_user": admin_user,
        "cashier_user": cashier_user,
        "admin_token": admin_token,
        "cashier_token": cashier_token,
    }


@pytest.mark.asyncio
async def test_update_branch_location_and_geofence_radius(
    test_client: AsyncClient,
    seed_data: dict,
) -> None:
    """Verify branch admin can update coordinates and geofence radius in meters."""
    branch = seed_data["branch"]
    admin_token = seed_data["admin_token"]
    headers = {
        "Authorization": f"Bearer {admin_token}",
        "X-Branch-ID": str(branch.id),
    }

    payload = {
        "latitude": "24.7742650",
        "longitude": "46.7385860",
        "geofence_radius_meters": 250,
    }

    response = await test_client.put(
        f"/api/v1/branches/{branch.id}/location",
        json=payload,
        headers=headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert float(data["latitude"]) == pytest.approx(24.7742650)
    assert float(data["longitude"]) == pytest.approx(46.7385860)
    assert data["geofence_radius_meters"] == 250


@pytest.mark.asyncio
async def test_update_branch_location_validation_and_rbac(
    test_client: AsyncClient,
    seed_data: dict,
) -> None:
    """Verify bounds validation on coordinates and radius, and cashier RBAC rejection."""
    branch = seed_data["branch"]
    cashier_token = seed_data["cashier_token"]
    admin_token = seed_data["admin_token"]

    # 1. Cashier attempts to update location -> 403 Forbidden
    resp_cashier = await test_client.put(
        f"/api/v1/branches/{branch.id}/location",
        json={"latitude": "24.0", "longitude": "46.0", "geofence_radius_meters": 100},
        headers={"Authorization": f"Bearer {cashier_token}", "X-Branch-ID": str(branch.id)},
    )
    assert resp_cashier.status_code == 403

    # 2. Invalid radius (< 5 meters) -> 422 Unprocessable Entity
    resp_invalid_radius = await test_client.put(
        f"/api/v1/branches/{branch.id}/location",
        json={"latitude": "24.0", "longitude": "46.0", "geofence_radius_meters": 2},
        headers={"Authorization": f"Bearer {admin_token}", "X-Branch-ID": str(branch.id)},
    )
    assert resp_invalid_radius.status_code == 422

    # 3. Invalid latitude (> 90 degrees) -> 422 Unprocessable Entity
    resp_invalid_lat = await test_client.put(
        f"/api/v1/branches/{branch.id}/location",
        json={"latitude": "95.0", "longitude": "46.0", "geofence_radius_meters": 150},
        headers={"Authorization": f"Bearer {admin_token}", "X-Branch-ID": str(branch.id)},
    )
    assert resp_invalid_lat.status_code == 422


@pytest.mark.asyncio
async def test_update_and_get_branch_financial_settings(
    test_client: AsyncClient,
    seed_data: dict,
) -> None:
    """Verify branch dynamic financial configuration update and retrieval."""
    branch = seed_data["branch"]
    admin_token = seed_data["admin_token"]
    headers = {
        "Authorization": f"Bearer {admin_token}",
        "X-Branch-ID": str(branch.id),
    }

    # 1. Update financial settings: 14% tax, 12% service fee, compound tax-on-service
    update_payload = {
        "tax_rate": "0.1400",
        "service_fee_rate": "0.1200",
        "is_service_taxable": True,
        "is_tax_inclusive": False,
        "service_fee_dine_in_only": True,
    }
    put_resp = await test_client.put(
        f"/api/v1/branches/{branch.id}/financial-settings",
        json=update_payload,
        headers=headers,
    )
    assert put_resp.status_code == 200
    put_data = put_resp.json()
    assert put_data["tax_rate"] == "0.1400"
    assert put_data["service_fee_rate"] == "0.1200"
    assert put_data["is_service_taxable"] is True
    assert put_data["service_fee_dine_in_only"] is True

    # 2. Query financial settings via GET
    get_resp = await test_client.get(
        f"/api/v1/branches/{branch.id}/financial-settings",
        headers=headers,
    )
    assert get_resp.status_code == 200
    get_data = get_resp.json()
    assert get_data["tax_rate"] == "0.1400"
    assert get_data["service_fee_rate"] == "0.1200"
    assert get_data["is_service_taxable"] is True


@pytest.mark.asyncio
async def test_branch_default_financial_settings(
    test_client: AsyncClient,
    test_session: AsyncSession,
    seed_data: dict,
) -> None:
    """Verify that a newly created branch defaults to 0.0000 for tax_rate and service_fee_rate."""
    tenant = seed_data["tenant"]
    admin_user = seed_data["admin_user"]

    new_branch = Branch(
        tenant_id=tenant.id,
        name={"en": "Default Branch", "ar": "فرع افتراضي"},
        slug="default-branch",
        latitude=Decimal("24.7136000"),
        longitude=Decimal("46.6753000"),
        is_active=True,
    )
    test_session.add(new_branch)
    await test_session.flush()

    assert new_branch.tax_rate == Decimal("0.0000")
    assert new_branch.service_fee_rate == Decimal("0.0000")

    test_session.add(UserBranchAccess(user_id=admin_user.id, branch_id=new_branch.id))
    await test_session.commit()

    admin_token = seed_data["admin_token"]
    headers = {
        "Authorization": f"Bearer {admin_token}",
        "X-Branch-ID": str(new_branch.id),
    }

    get_resp = await test_client.get(
        f"/api/v1/branches/{new_branch.id}/financial-settings",
        headers=headers,
    )
    assert get_resp.status_code == 200
    data = get_resp.json()
    assert data["tax_rate"] == "0.0000"
    assert data["service_fee_rate"] == "0.0000"

