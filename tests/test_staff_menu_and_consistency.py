"""Comprehensive Automated Test Suite: Staff Menu CRUD & End-to-End Data Consistency.

Verifies:
- Scenario 1: Category Lifecycle & Station Inheritance Consistency (Staff Create/Patch -> Guest Tree).
- Scenario 2: Item Creation, Pricing Sync & Line Validation Consistency (Staff Create/Patch Price -> Guest Calculation).
- Scenario 3: Item 86 Kill-Switch Synchronization (Availability Lifecycle: Staff 86 -> Tree Preserved -> Checkout Rejected -> Restock Success).
- Scenario 4: Modifier Option 86 Toggle Synchronization (Staff Option 86 -> Guest Validation & Checkout Guarded).
- Scenario 5: Deactivation / Delete Boundary (Staff Delete -> Tree Disappearance -> Direct ID 404 Rejection).
- Scenario 6: Multi-Tenant & Branch Scoping Security (RBAC Role Guards & Cross-Branch Isolation).
"""

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
from app.core.i18n import SYSTEM_MESSAGES, SupportedLocale
from app.core.security import create_access_token, get_password_hash
from app.core.session_security import create_guest_session_jwt
from app.main import create_app
from app.models.auth import Base, Branch, Table, Tenant, User, UserBranchAccess
from app.models.catalog import Category, Item, KitchenStation, ModifierGroup, ModifierOption
from app.models.enums import TableStatus, UserRole


def assert_error_key(detail: str, expected_key: str):
    """Assert error detail matches either the raw message key or its localized translation."""
    allowed = {
        expected_key,
        SYSTEM_MESSAGES[expected_key][SupportedLocale.EN],
        SYSTEM_MESSAGES[expected_key][SupportedLocale.AR],
    }
    assert any(expected in detail for expected in allowed), (
        f"Expected key '{expected_key}' (or localized translation) in '{detail}'"
    )


# ---------------------------------------------------------------------------
# Test Database Engine & Client Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="function")
async def async_test_engine() -> AsyncGenerator[AsyncEngine, None]:
    """Isolated in-memory SQLite async engine per test."""
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
    """Seed comprehensive test environment with Tenant, 2 Branches, Table, and 3 Staff roles."""
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

    table_session_id = uuid.uuid4()
    table = Table(
        branch_id=branch_1.id,
        table_number="T-01",
        capacity=4,
        status=TableStatus.BROWSING,
        current_session_token=str(table_session_id),
        is_active=True,
    )
    test_session.add(table)
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
    waiter = User(
        tenant_id=tenant.id,
        email="waiter@gourmet.com",
        hashed_password=pw_hash,
        full_name="Walter (Waiter)",
        role=UserRole.WAITER,
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
    test_session.add_all([super_admin, branch_admin, waiter, cashier])
    await test_session.flush()

    ba_admin = UserBranchAccess(user_id=branch_admin.id, branch_id=branch_1.id)
    ba_waiter = UserBranchAccess(user_id=waiter.id, branch_id=branch_1.id)
    ba_cashier = UserBranchAccess(user_id=cashier.id, branch_id=branch_1.id)
    test_session.add_all([ba_admin, ba_waiter, ba_cashier])
    await test_session.commit()

    return {
        "tenant": tenant,
        "branch_1": branch_1,
        "branch_2": branch_2,
        "table": table,
        "table_session_id": table_session_id,
        "super_admin": super_admin,
        "branch_admin": branch_admin,
        "waiter": waiter,
        "cashier": cashier,
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


def get_staff_token(user: User) -> str:
    return create_access_token({
        "sub": str(user.id),
        "tenant_id": str(user.tenant_id),
        "role": user.role.value,
        "email": user.email,
    })


def get_guest_token(seed_data: dict) -> str:
    table = seed_data["table"]
    tenant = seed_data["tenant"]
    branch = seed_data["branch_1"]
    return create_guest_session_jwt(
        session_id=seed_data["table_session_id"],
        tenant_id=tenant.id,
        branch_id=branch.id,
        table_id=table.id,
        table_number=table.table_number,
        is_presence_verified=True,
    )


# ---------------------------------------------------------------------------
# Scenario 1: Category Lifecycle & Station Inheritance Consistency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_1_category_lifecycle_and_station_inheritance(client: AsyncClient, seed_data: dict):
    admin = seed_data["branch_admin"]
    branch = seed_data["branch_1"]
    staff_token = get_staff_token(admin)
    guest_token = get_guest_token(seed_data)

    staff_headers = {
        "Authorization": f"Bearer {staff_token}",
        "X-Branch-ID": str(branch.id),
    }

    # 1. Staff creates category with station BEVERAGE
    create_payload = {
        "name": {"en": "Cold Drinks", "ar": "مشروبات باردة"},
        "display_order": 1,
        "station": "BEVERAGE",
        "is_active": True,
    }
    create_res = await client.post(
        "/api/v1/staff/menu/categories",
        headers=staff_headers,
        json=create_payload,
    )
    assert create_res.status_code == 201
    cat_id = create_res.json()["id"]
    assert create_res.json()["station"] == "BEVERAGE"

    # 2. Guest inspects menu tree immediately
    tree_res = await client.get(
        "/api/v1/menu/tree",
        headers={"Authorization": f"Bearer {guest_token}", "Accept-Language": "en"},
    )
    assert tree_res.status_code == 200
    categories = tree_res.json()["categories"]
    created_cat = next((c for c in categories if c["id"] == cat_id), None)
    assert created_cat is not None
    assert created_cat["name"] == "Cold Drinks"
    assert created_cat["station"] == "BEVERAGE"

    # 3. Staff updates station to DESSERT and updates display_order
    patch_res = await client.patch(
        f"/api/v1/staff/menu/categories/{cat_id}",
        headers=staff_headers,
        json={"station": "DESSERT", "display_order": 10},
    )
    assert patch_res.status_code == 200
    assert patch_res.json()["station"] == "DESSERT"
    assert patch_res.json()["display_order"] == 10

    # 4. Guest inspects menu tree again - assert immediate synchronization
    tree_res_updated = await client.get(
        "/api/v1/menu/tree",
        headers={"Authorization": f"Bearer {guest_token}", "Accept-Language": "en"},
    )
    assert tree_res_updated.status_code == 200
    updated_cat = next((c for c in tree_res_updated.json()["categories"] if c["id"] == cat_id), None)
    assert updated_cat is not None
    assert updated_cat["station"] == "DESSERT"
    assert updated_cat["display_order"] == 10


# ---------------------------------------------------------------------------
# Scenario 2: Item Creation, Pricing Sync & Line Validation Consistency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_2_item_creation_pricing_sync_and_validation(client: AsyncClient, seed_data: dict):
    admin = seed_data["branch_admin"]
    branch = seed_data["branch_1"]
    staff_token = get_staff_token(admin)
    guest_token = get_guest_token(seed_data)

    staff_headers = {
        "Authorization": f"Bearer {staff_token}",
        "X-Branch-ID": str(branch.id),
    }

    # Setup category
    cat_res = await client.post(
        "/api/v1/staff/menu/categories",
        headers=staff_headers,
        json={"name": {"en": "Mains", "ar": "الرئيسية"}, "station": "HOT_KITCHEN"},
    )
    category_id = cat_res.json()["id"]

    # 1. Create Item with base_price 30.00
    item_res = await client.post(
        "/api/v1/staff/menu/items",
        headers=staff_headers,
        json={
            "category_id": category_id,
            "name": {"en": "Classic Burger", "ar": "كلاسيك برغر"},
            "base_price": "30.00",
            "station": "HOT_KITCHEN",
            "is_available": True,
        },
    )
    assert item_res.status_code == 201
    item_id = item_res.json()["id"]

    # Create Modifier Group (min=1, max=1)
    group_res = await client.post(
        f"/api/v1/staff/menu/items/{item_id}/modifier-groups",
        headers=staff_headers,
        json={"name": {"en": "Size", "ar": "الحجم"}, "min_choices": 1, "max_choices": 1, "is_required": True},
    )
    assert group_res.status_code == 201
    group_id = group_res.json()["id"]

    # Add Option with price_delta = 5.00
    opt_res = await client.post(
        f"/api/v1/staff/menu/modifier-groups/{group_id}/options",
        headers=staff_headers,
        json={"name": {"en": "Large", "ar": "كبير"}, "price_delta": "5.00", "is_available": True},
    )
    assert opt_res.status_code == 201
    opt_id = opt_res.json()["id"]

    # 2. Authoritative Calculation (Guest): Validate selection (quantity: 2)
    guest_headers = {
        "Authorization": f"Bearer {guest_token}",
        "Content-Type": "application/json",
    }
    val_payload = {
        "item_id": item_id,
        "quantity": 2,
        "selected_groups": [
            {"group_id": group_id, "option_ids": [opt_id]}
        ],
    }
    val_res = await client.post(
        "/api/v1/menu/validate-item-selection",
        headers=guest_headers,
        json=val_payload,
    )
    assert val_res.status_code == 200
    val_data = val_res.json()
    assert Decimal(str(val_data["unit_price"])) == Decimal("35.00")  # 30.00 + 5.00
    assert Decimal(str(val_data["subtotal"])) == Decimal("70.00")    # 35.00 * 2

    # 3. Staff updates base_price from 30.00 to 40.00
    patch_item = await client.patch(
        f"/api/v1/staff/menu/items/{item_id}",
        headers=staff_headers,
        json={"base_price": "40.00"},
    )
    assert patch_item.status_code == 200
    assert Decimal(str(patch_item.json()["base_price"])) == Decimal("40.00")

    # 4. Guest calls validate again - unit_price must immediately be 45.00 (40.00 + 5.00)
    val_res_2 = await client.post(
        "/api/v1/menu/validate-item-selection",
        headers=guest_headers,
        json=val_payload,
    )
    assert val_res_2.status_code == 200
    val_data_2 = val_res_2.json()
    assert Decimal(str(val_data_2["unit_price"])) == Decimal("45.00")  # 40.00 + 5.00
    assert Decimal(str(val_data_2["subtotal"])) == Decimal("90.00")    # 45.00 * 2


# ---------------------------------------------------------------------------
# Scenario 3: Item 86 Kill-Switch Synchronization (Availability Lifecycle)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_3_item_86_kill_switch_synchronization(client: AsyncClient, seed_data: dict):
    admin = seed_data["branch_admin"]
    branch = seed_data["branch_1"]
    staff_token = get_staff_token(admin)
    guest_token = get_guest_token(seed_data)

    staff_headers = {
        "Authorization": f"Bearer {staff_token}",
        "X-Branch-ID": str(branch.id),
    }
    guest_headers = {
        "Authorization": f"Bearer {guest_token}",
        "Content-Type": "application/json",
    }

    # Setup category & item
    cat_res = await client.post(
        "/api/v1/staff/menu/categories",
        headers=staff_headers,
        json={"name": {"en": "Specials", "ar": "المميز"}, "station": "HOT_KITCHEN"},
    )
    cat_id = cat_res.json()["id"]

    item_res = await client.post(
        "/api/v1/staff/menu/items",
        headers=staff_headers,
        json={
            "category_id": cat_id,
            "name": {"en": "Wagyu Ribeye", "ar": "واغيو ريب آي"},
            "base_price": "120.00",
            "is_available": True,
        },
    )
    item_id = item_res.json()["id"]

    # 1. Staff triggers 86 Kill-Switch: is_available = False
    toggle_res = await client.patch(
        f"/api/v1/staff/menu/items/{item_id}/availability",
        headers=staff_headers,
        json={"is_available": False},
    )
    assert toggle_res.status_code == 200
    assert toggle_res.json()["is_available"] is False

    # 2. Guest inspects menu tree - item must be preserved in tree with is_available == False
    tree_res = await client.get("/api/v1/menu/tree", headers=guest_headers)
    assert tree_res.status_code == 200
    all_items = [it for c in tree_res.json()["categories"] for it in c["items"]]
    wagyu_in_tree = next((it for it in all_items if it["id"] == item_id), None)
    assert wagyu_in_tree is not None
    assert wagyu_in_tree["is_available"] is False

    # 3. Guest attempts checkout with 86'd item -> MUST FAIL with 400 ITEM_UNAVAILABLE
    checkout_payload = {
        "items": [
            {"item_id": item_id, "quantity": 1, "selected_groups": []}
        ]
    }
    checkout_res = await client.post(
        "/api/v1/orders/checkout",
        headers=guest_headers,
        json=checkout_payload,
    )
    assert checkout_res.status_code == 400
    assert_error_key(checkout_res.json()["detail"], "ITEM_UNAVAILABLE")

    # 4. Staff restocks item: is_available = True
    restock_res = await client.patch(
        f"/api/v1/staff/menu/items/{item_id}/availability",
        headers=staff_headers,
        json={"is_available": True},
    )
    assert restock_res.status_code == 200
    assert restock_res.json()["is_available"] is True

    # 5. Guest re-attempts checkout -> MUST SUCCEED with 201 Created and SUBMITTED status
    checkout_success = await client.post(
        "/api/v1/orders/checkout",
        headers=guest_headers,
        json=checkout_payload,
    )
    assert checkout_success.status_code == 201
    assert checkout_success.json()["status"] == "SUBMITTED"


# ---------------------------------------------------------------------------
# Scenario 4: Modifier Option 86 Toggle Synchronization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_4_modifier_option_86_toggle_synchronization(client: AsyncClient, seed_data: dict):
    admin = seed_data["branch_admin"]
    branch = seed_data["branch_1"]
    staff_token = get_staff_token(admin)
    guest_token = get_guest_token(seed_data)

    staff_headers = {
        "Authorization": f"Bearer {staff_token}",
        "X-Branch-ID": str(branch.id),
    }
    guest_headers = {
        "Authorization": f"Bearer {guest_token}",
        "Content-Type": "application/json",
    }

    # Setup category, item, modifier group, and 2 options
    cat_res = await client.post(
        "/api/v1/staff/menu/categories",
        headers=staff_headers,
        json={"name": {"en": "Pizzas", "ar": "بيتزا"}, "station": "HOT_KITCHEN"},
    )
    cat_id = cat_res.json()["id"]

    item_res = await client.post(
        "/api/v1/staff/menu/items",
        headers=staff_headers,
        json={"category_id": cat_id, "name": {"en": "Margherita", "ar": "مارغريتا"}, "base_price": "50.00"},
    )
    item_id = item_res.json()["id"]

    group_res = await client.post(
        f"/api/v1/staff/menu/items/{item_id}/modifier-groups",
        headers=staff_headers,
        json={"name": {"en": "Extra Cheese", "ar": "جبنة إضافية"}, "min_choices": 1, "max_choices": 1, "is_required": True},
    )
    group_id = group_res.json()["id"]

    opt_res = await client.post(
        f"/api/v1/staff/menu/modifier-groups/{group_id}/options",
        headers=staff_headers,
        json={"name": {"en": "Bufala Mozzarella", "ar": "موزاريلا بوفالو"}, "price_delta": "8.00", "is_available": True},
    )
    option_id = opt_res.json()["id"]

    # 1. Staff toggles option out of stock: is_available = False
    opt_toggle_res = await client.patch(
        f"/api/v1/staff/menu/modifier-options/{option_id}/availability",
        headers=staff_headers,
        json={"is_available": False},
    )
    assert opt_toggle_res.status_code == 200
    assert opt_toggle_res.json()["is_available"] is False

    # 2. Guest validates selection with disabled option -> 400 MODIFIER_OPTION_UNAVAILABLE
    val_payload = {
        "item_id": item_id,
        "quantity": 1,
        "selected_groups": [
            {"group_id": group_id, "option_ids": [option_id]}
        ],
    }
    val_res = await client.post(
        "/api/v1/menu/validate-item-selection",
        headers=guest_headers,
        json=val_payload,
    )
    assert val_res.status_code == 400
    assert_error_key(val_res.json()["detail"], "MODIFIER_OPTION_UNAVAILABLE")

    # 3. Guest attempts checkout with disabled option -> 400 MODIFIER_OPTION_UNAVAILABLE
    checkout_payload = {
        "items": [
            {
                "item_id": item_id,
                "quantity": 1,
                "selected_groups": [
                    {"group_id": group_id, "option_ids": [option_id]}
                ],
            }
        ]
    }
    checkout_res = await client.post(
        "/api/v1/orders/checkout",
        headers=guest_headers,
        json=checkout_payload,
    )
    assert checkout_res.status_code == 400
    assert_error_key(checkout_res.json()["detail"], "MODIFIER_OPTION_UNAVAILABLE")


# ---------------------------------------------------------------------------
# Scenario 5: Soft-Delete & Deactivation Boundary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_5_delete_boundary_and_direct_access_rejection(client: AsyncClient, seed_data: dict):
    admin = seed_data["branch_admin"]
    branch = seed_data["branch_1"]
    staff_token = get_staff_token(admin)
    guest_token = get_guest_token(seed_data)

    staff_headers = {
        "Authorization": f"Bearer {staff_token}",
        "X-Branch-ID": str(branch.id),
    }
    guest_headers = {
        "Authorization": f"Bearer {guest_token}",
        "Content-Type": "application/json",
    }

    # Setup category & item
    cat_res = await client.post(
        "/api/v1/staff/menu/categories",
        headers=staff_headers,
        json={"name": {"en": "Desserts", "ar": "الحلويات"}, "station": "DESSERT"},
    )
    cat_id = cat_res.json()["id"]

    item_res = await client.post(
        "/api/v1/staff/menu/items",
        headers=staff_headers,
        json={"category_id": cat_id, "name": {"en": "Tiramisu", "ar": "تيراميسو"}, "base_price": "28.00"},
    )
    item_id = item_res.json()["id"]

    # 1. Staff permanently deletes item
    del_res = await client.delete(
        f"/api/v1/staff/menu/items/{item_id}",
        headers=staff_headers,
    )
    assert del_res.status_code == 200

    # 2. Guest inspects menu tree -> Deleted item MUST NOT appear anywhere
    tree_res = await client.get("/api/v1/menu/tree", headers=guest_headers)
    assert tree_res.status_code == 200
    all_items = [it for c in tree_res.json()["categories"] for it in c["items"]]
    assert not any(it["id"] == item_id for it in all_items)

    # 3. Direct ID access rejection: validate-item-selection returns 404 ITEM_NOT_FOUND
    val_res = await client.post(
        "/api/v1/menu/validate-item-selection",
        headers=guest_headers,
        json={"item_id": item_id, "quantity": 1, "selected_groups": []},
    )
    assert val_res.status_code == 404
    assert_error_key(val_res.json()["detail"], "ITEM_NOT_FOUND")

    # 4. Direct ID access rejection: checkout returns 404 ITEM_NOT_FOUND
    checkout_res = await client.post(
        "/api/v1/orders/checkout",
        headers=guest_headers,
        json={"items": [{"item_id": item_id, "quantity": 1, "selected_groups": []}]},
    )
    assert checkout_res.status_code == 404
    assert_error_key(checkout_res.json()["detail"], "ITEM_NOT_FOUND")



# ---------------------------------------------------------------------------
# Scenario 6: Multi-Tenant & Branch Scoping Security (RBAC Guard)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_6_rbac_guard_and_cross_branch_isolation(client: AsyncClient, seed_data: dict):
    waiter = seed_data["waiter"]
    cashier = seed_data["cashier"]
    admin = seed_data["branch_admin"]
    branch_1 = seed_data["branch_1"]
    branch_2 = seed_data["branch_2"]

    waiter_token = get_staff_token(waiter)
    cashier_token = get_staff_token(cashier)
    admin_token = get_staff_token(admin)

    # 1. Unauthorized roles (WAITER, CASHIER) attempting staff menu mutations -> 403 Forbidden
    for unauthorized_token in (waiter_token, cashier_token):
        res = await client.post(
            "/api/v1/staff/menu/categories",
            headers={"Authorization": f"Bearer {unauthorized_token}", "X-Branch-ID": str(branch_1.id)},
            json={"name": {"en": "Unauthorized", "ar": "غير مصرح"}},
        )
        assert res.status_code == 403

    # 2. Branch Admin assigned to Branch 1 attempts to pass Branch 2 header -> 403 Forbidden
    cross_header_res = await client.post(
        "/api/v1/staff/menu/categories",
        headers={"Authorization": f"Bearer {admin_token}", "X-Branch-ID": str(branch_2.id)},
        json={"name": {"en": "Branch 2 Cat", "ar": "قسم فرع 2"}},
    )
    assert cross_header_res.status_code == 403

    # 3. Super Admin creates item in Branch 2
    super_token = get_staff_token(seed_data["super_admin"])
    b2_cat = await client.post(
        "/api/v1/staff/menu/categories",
        headers={"Authorization": f"Bearer {super_token}", "X-Branch-ID": str(branch_2.id)},
        json={"name": {"en": "Branch 2 Drinks", "ar": "مشروبات فرع 2"}},
    )
    b2_cat_id = b2_cat.json()["id"]

    b2_item = await client.post(
        "/api/v1/staff/menu/items",
        headers={"Authorization": f"Bearer {super_token}", "X-Branch-ID": str(branch_2.id)},
        json={"category_id": b2_cat_id, "name": {"en": "Branch 2 Tea", "ar": "شاي فرع 2"}, "base_price": "15.00"},
    )
    b2_item_id = b2_item.json()["id"]

    # 4. Branch 1 Admin attempting to modify Branch 2 item using Branch 1 header -> 404 Not Found (branch isolation)
    cross_item_res = await client.patch(
        f"/api/v1/staff/menu/items/{b2_item_id}",
        headers={"Authorization": f"Bearer {admin_token}", "X-Branch-ID": str(branch_1.id)},
        json={"base_price": "20.00"},
    )
    assert cross_item_res.status_code == 404
    assert f"Item '{b2_item_id}' not found within authorized branch." in cross_item_res.json()["detail"]
