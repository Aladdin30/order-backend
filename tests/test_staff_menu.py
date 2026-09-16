"""Comprehensive Automated Tests for Staff Menu Management CRUD Endpoints & Item 86 Toggle."""

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
from app.models.auth import Base, Branch, Table, Tenant, User, UserBranchAccess
from app.models.catalog import Category, Item, KitchenStation, ModifierGroup, ModifierOption
from app.models.enums import TableStatus, UserRole


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
    session_factory = async_sessionmaker(
        bind=async_test_engine,
        class_=AsyncSession,
        autoflush=False,
        expire_on_commit=False,
    )
    async with session_factory() as session:
        yield session


@pytest_asyncio.fixture(scope="function")
async def seed_data(test_session: AsyncSession) -> dict:
    tenant = Tenant(name="Gourmet Dining Group", slug="gourmet-dining", is_active=True)
    test_session.add(tenant)
    await test_session.flush()

    branch_1 = Branch(
        tenant_id=tenant.id,
        name={"en": "Downtown Branch", "ar": "فرع وسط المدينة"},
        slug="downtown",
        latitude=Decimal("24.7136"),
        longitude=Decimal("46.6753"),
        geofence_radius_meters=150,
        is_active=True,
    )
    branch_2 = Branch(
        tenant_id=tenant.id,
        name={"en": "Uptown Branch", "ar": "فرع أعلى المدينة"},
        slug="uptown",
        latitude=Decimal("24.7500"),
        longitude=Decimal("46.7000"),
        geofence_radius_meters=200,
        is_active=True,
    )
    test_session.add_all([branch_1, branch_2])
    await test_session.flush()

    pw_hash = get_password_hash("Admin123!")

    super_admin = User(
        tenant_id=tenant.id,
        email="superadmin@gourmet.com",
        hashed_password=pw_hash,
        full_name="Alice (Super Admin)",
        role=UserRole.SUPER_ADMIN,
        is_active=True,
    )
    branch_admin = User(
        tenant_id=tenant.id,
        email="branchadmin@gourmet.com",
        hashed_password=pw_hash,
        full_name="Bob (Branch Admin)",
        role=UserRole.BRANCH_ADMIN,
        is_active=True,
    )
    cashier = User(
        tenant_id=tenant.id,
        email="cashier@gourmet.com",
        hashed_password=pw_hash,
        full_name="Charlie (Cashier)",
        role=UserRole.CASHIER,
        is_active=True,
    )
    test_session.add_all([super_admin, branch_admin, cashier])
    await test_session.flush()

    ba_admin = UserBranchAccess(user_id=branch_admin.id, branch_id=branch_1.id)
    ba_cashier = UserBranchAccess(user_id=cashier.id, branch_id=branch_1.id)
    test_session.add_all([ba_admin, ba_cashier])

    # Initial category and item
    category = Category(
        branch_id=branch_1.id,
        name={"en": "Mains", "ar": "الأطباق الرئيسية"},
        display_order=1,
        station=KitchenStation.HOT_KITCHEN,
        is_active=True,
    )
    test_session.add(category)
    await test_session.flush()

    item = Item(
        category_id=category.id,
        name={"en": "Angus Steak", "ar": "ستيك أنغوس"},
        base_price=Decimal("85.00"),
        station=KitchenStation.HOT_KITCHEN,
        is_available=True,
    )
    test_session.add(item)
    await test_session.commit()

    return {
        "tenant": tenant,
        "branch_1": branch_1,
        "branch_2": branch_2,
        "super_admin": super_admin,
        "branch_admin": branch_admin,
        "cashier": cashier,
        "category": category,
        "item": item,
    }


@pytest_asyncio.fixture(scope="function")
async def client(async_test_engine: AsyncEngine) -> AsyncGenerator[AsyncClient, None]:
    session_factory = async_sessionmaker(
        bind=async_test_engine,
        class_=AsyncSession,
        autoflush=False,
        expire_on_commit=False,
    )

    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_async_db] = override_get_db

    with patch("app.core.database.async_session_factory", session_factory), \
         patch("app.services.audit_service.async_session_factory", session_factory):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


def make_staff_token(user: User) -> str:
    return create_access_token({
        "sub": str(user.id),
        "tenant_id": str(user.tenant_id),
        "role": user.role.value,
        "email": user.email,
    })


# ---------------------------------------------------------------------------
# Test Category Management Endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_staff_create_category_success(client: AsyncClient, seed_data: dict):
    admin = seed_data["branch_admin"]
    branch = seed_data["branch_1"]
    token = make_staff_token(admin)

    payload = {
        "name": {"en": "Appetizers", "ar": "المقبلات"},
        "display_order": 0,
        "station": "COLD_KITCHEN",
        "is_active": True,
    }
    response = await client.post(
        "/api/v1/staff/menu/categories",
        headers={"Authorization": f"Bearer {token}", "X-Branch-ID": str(branch.id)},
        json=payload,
    )
    assert response.status_code == 201
    data = response.json()
    assert data["name"] == {"en": "Appetizers", "ar": "المقبلات"}
    assert data["station"] == "COLD_KITCHEN"
    assert data["branch_id"] == str(branch.id)


@pytest.mark.asyncio
async def test_staff_update_and_delete_category(client: AsyncClient, seed_data: dict):
    admin = seed_data["branch_admin"]
    branch = seed_data["branch_1"]
    category = seed_data["category"]
    token = make_staff_token(admin)

    # 1. Update
    patch_payload = {
        "name": {"en": "Signature Mains", "ar": "الأطباق المميزة"},
        "display_order": 5,
    }
    update_res = await client.patch(
        f"/api/v1/staff/menu/categories/{category.id}",
        headers={"Authorization": f"Bearer {token}", "X-Branch-ID": str(branch.id)},
        json=patch_payload,
    )
    assert update_res.status_code == 200
    assert update_res.json()["name"]["en"] == "Signature Mains"
    assert update_res.json()["display_order"] == 5

    # 2. Soft Deactivate
    del_res = await client.delete(
        f"/api/v1/staff/menu/categories/{category.id}?soft=true",
        headers={"Authorization": f"Bearer {token}", "X-Branch-ID": str(branch.id)},
    )
    assert del_res.status_code == 200
    assert del_res.json()["status"] == "success"


# ---------------------------------------------------------------------------
# Test Item Management & Item 86 Kill-Switch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_staff_item_crud_and_86_toggle(client: AsyncClient, seed_data: dict):
    admin = seed_data["branch_admin"]
    branch = seed_data["branch_1"]
    category = seed_data["category"]
    token = make_staff_token(admin)

    # 1. Create Item
    create_payload = {
        "category_id": str(category.id),
        "name": {"en": "Truffle Burger", "ar": "برغر الترفل"},
        "description": {"en": "Juicy beef patty with black truffle sauce", "ar": "شريحة لحم بصوص الترفل"},
        "base_price": "45.00",
        "station": "HOT_KITCHEN",
        "is_available": True,
        "allergens": ["gluten", "dairy"],
        "dietary_badges": ["halal"],
    }
    create_res = await client.post(
        "/api/v1/staff/menu/items",
        headers={"Authorization": f"Bearer {token}", "X-Branch-ID": str(branch.id)},
        json=create_payload,
    )
    assert create_res.status_code == 201
    item_data = create_res.json()
    item_id = item_data["id"]
    assert item_data["name"]["en"] == "Truffle Burger"
    assert item_data["is_available"] is True

    # 2. Toggle Item 86 Kill-Switch (Mark unavailable)
    avail_res = await client.patch(
        f"/api/v1/staff/menu/items/{item_id}/availability",
        headers={"Authorization": f"Bearer {token}", "X-Branch-ID": str(branch.id)},
        json={"is_available": False},
    )
    assert avail_res.status_code == 200
    assert avail_res.json()["is_available"] is False

    # 3. Check public menu/tree preserves 86'd item
    tree_res = await client.get(
        f"/api/v1/menu/tree?branch_id={branch.id}",
        headers={"Authorization": f"Bearer {token}", "Accept-Language": "en"},
    )
    assert tree_res.status_code == 200
    tree = tree_res.json()
    truffle_item = None
    for cat in tree["categories"]:
        for it in cat["items"]:
            if it["id"] == item_id:
                truffle_item = it
    assert truffle_item is not None
    assert truffle_item["is_available"] is False

    # 4. Partial update (price increase)
    update_res = await client.patch(
        f"/api/v1/staff/menu/items/{item_id}",
        headers={"Authorization": f"Bearer {token}", "X-Branch-ID": str(branch.id)},
        json={"base_price": "48.00"},
    )
    assert update_res.status_code == 200
    assert Decimal(str(update_res.json()["base_price"])) == Decimal("48.00")

    # 5. Delete Item
    del_res = await client.delete(
        f"/api/v1/staff/menu/items/{item_id}?soft=false",
        headers={"Authorization": f"Bearer {token}", "X-Branch-ID": str(branch.id)},
    )
    assert del_res.status_code == 200


# ---------------------------------------------------------------------------
# Test Modifier Groups & Options Management
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_staff_modifier_group_and_option_flow(client: AsyncClient, seed_data: dict):
    admin = seed_data["branch_admin"]
    branch = seed_data["branch_1"]
    item = seed_data["item"]
    token = make_staff_token(admin)

    # 1. Create Modifier Group
    group_payload = {
        "name": {"en": "Meat Doneness", "ar": "درجة الاستواء"},
        "min_choices": 1,
        "max_choices": 1,
        "is_required": True,
    }
    group_res = await client.post(
        f"/api/v1/staff/menu/items/{item.id}/modifier-groups",
        headers={"Authorization": f"Bearer {token}", "X-Branch-ID": str(branch.id)},
        json=group_payload,
    )
    assert group_res.status_code == 201
    group_id = group_res.json()["id"]

    # 2. Add Option
    opt_payload = {
        "name": {"en": "Medium Rare", "ar": "نصف استواء"},
        "price_delta": "0.00",
        "is_available": True,
    }
    opt_res = await client.post(
        f"/api/v1/staff/menu/modifier-groups/{group_id}/options",
        headers={"Authorization": f"Bearer {token}", "X-Branch-ID": str(branch.id)},
        json=opt_payload,
    )
    assert opt_res.status_code == 201
    opt_id = opt_res.json()["id"]

    # 3. Toggle Option 86 Availability
    opt_86_res = await client.patch(
        f"/api/v1/staff/menu/modifier-options/{opt_id}/availability",
        headers={"Authorization": f"Bearer {token}", "X-Branch-ID": str(branch.id)},
        json={"is_available": False},
    )
    assert opt_86_res.status_code == 200
    assert opt_86_res.json()["is_available"] is False

    # 4. Delete Option & Group
    del_opt = await client.delete(
        f"/api/v1/staff/menu/modifier-options/{opt_id}",
        headers={"Authorization": f"Bearer {token}", "X-Branch-ID": str(branch.id)},
    )
    assert del_opt.status_code == 200

    del_grp = await client.delete(
        f"/api/v1/staff/menu/modifier-groups/{group_id}",
        headers={"Authorization": f"Bearer {token}", "X-Branch-ID": str(branch.id)},
    )
    assert del_grp.status_code == 200


# ---------------------------------------------------------------------------
# Test RBAC & Branch Boundary Isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_staff_menu_unauthorized_role_rejected(client: AsyncClient, seed_data: dict):
    cashier = seed_data["cashier"]
    branch = seed_data["branch_1"]
    token = make_staff_token(cashier)

    # Cashier role is NOT authorized for menu modifications
    response = await client.post(
        "/api/v1/staff/menu/categories",
        headers={"Authorization": f"Bearer {token}", "X-Branch-ID": str(branch.id)},
        json={"name": {"en": "Drinks", "ar": "مشروبات"}},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_staff_menu_cross_branch_access_rejected(client: AsyncClient, seed_data: dict):
    admin = seed_data["branch_admin"]
    branch_2 = seed_data["branch_2"]  # Admin is NOT assigned to branch 2
    token = make_staff_token(admin)

    response = await client.post(
        "/api/v1/staff/menu/categories",
        headers={"Authorization": f"Bearer {token}", "X-Branch-ID": str(branch_2.id)},
        json={"name": {"en": "Desserts", "ar": "حلويات"}},
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Test Health Readiness Check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_ready_endpoint(client: AsyncClient):
    response = await client.get("/health/ready")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] in ("ready", "degraded")
    assert "database" in data
