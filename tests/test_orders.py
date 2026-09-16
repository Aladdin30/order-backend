"""Comprehensive Test Suite for Task BE-2.3: Order Ingestion & State Machine Engine.

Tests:
1. Category-driven kitchen station inheritance (Category.station vs Item.station override).
2. ACID checkout pipeline with row-level locking (SELECT ... FOR UPDATE).
3. Presence-driven initial order status (SUBMITTED vs PENDING_STAFF_CONFIRMATION).
4. Live shared cart re-ordering / appending and authoritative ROUND_HALF_UP tax math.
5. Deterministic Finite State Machine (FSM) validation and 409 conflict on illegal transitions.
6. Table status synchronization (AWAITING_FOOD, EATING, AVAILABLE).
7. Strict cancellation guard rules (customer vs branch admin, mandatory audit reason).
8. Customer live tracker (GET /api/v1/orders/active).
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import AsyncGenerator
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.api.deps import get_async_db
from app.core.security import create_access_token, get_password_hash
from app.core.session_security import create_guest_session_jwt
from app.main import create_app
from app.models.auth import Base, Branch, Table, Tenant, User, UserBranchAccess
from app.models.catalog import Category, Item, ModifierGroup, ModifierOption
from app.models.enums import (
    KitchenStation,
    OrderStatus,
    TableStatus,
    UserRole,
)
from app.models.order import Order, OrderItem


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
async def seed_order_data(test_session: AsyncSession) -> dict:
    """Seed tenant, branch, staff users, tables, catalog categories, items, and modifiers."""
    tenant = Tenant(name="Royal Hospitality", slug="royal-hosp", is_active=True)
    test_session.add(tenant)
    await test_session.flush()

    branch = Branch(
        tenant_id=tenant.id,
        name={"en": "Downtown Branch", "ar": "فرع وسط المدينة"},
        slug="downtown",
        latitude=24.7136,
        longitude=46.6753,
        geofence_radius_meters=150,
        is_active=True,
    )
    test_session.add(branch)
    await test_session.flush()

    # Tables
    table = Table(
        branch_id=branch.id,
        table_number="T-01",
        capacity=4,
        status=TableStatus.BROWSING,
        is_active=True,
    )
    table_inactive = Table(
        branch_id=branch.id,
        table_number="T-99",
        capacity=2,
        status=TableStatus.AVAILABLE,
        is_active=False,
    )
    test_session.add_all([table, table_inactive])
    await test_session.flush()

    # Staff Users: Branch Admin and Kitchen Staff
    pw_hash = get_password_hash("Password123!")
    branch_admin = User(
        tenant_id=tenant.id,
        email="admin@royal.com",
        hashed_password=pw_hash,
        full_name="Bob Admin",
        role=UserRole.BRANCH_ADMIN,
        is_active=True,
    )
    kitchen_staff = User(
        tenant_id=tenant.id,
        email="chef@royal.com",
        hashed_password=pw_hash,
        full_name="Chef Gordon",
        role=UserRole.KITCHEN_STAFF,
        is_active=True,
    )
    test_session.add_all([branch_admin, kitchen_staff])
    await test_session.flush()

    access_admin = UserBranchAccess(user_id=branch_admin.id, branch_id=branch.id)
    access_chef = UserBranchAccess(user_id=kitchen_staff.id, branch_id=branch.id)
    test_session.add_all([access_admin, access_chef])
    await test_session.flush()

    # Categories with Stations
    cat_beverages = Category(
        branch_id=branch.id,
        name={"en": "Beverages", "ar": "المشروبات"},
        display_order=0,
        station=KitchenStation.BEVERAGE,
        is_active=True,
    )
    cat_hot_kitchen = Category(
        branch_id=branch.id,
        name={"en": "Mains", "ar": "الأطباق الرئيسية"},
        display_order=1,
        station=KitchenStation.HOT_KITCHEN,
        is_active=True,
    )
    test_session.add_all([cat_beverages, cat_hot_kitchen])
    await test_session.flush()

    # Items:
    # 1. Lemonade: station=None -> inherits BEVERAGE from cat_beverages
    item_drink = Item(
        category_id=cat_beverages.id,
        name={"en": "Fresh Lemonade", "ar": "ليموناضة طازجة"},
        description={"en": "Cold pressed with mint", "ar": "طازج بالنعناع"},
        base_price=Decimal("12.00"),
        station=None,  # Should inherit BEVERAGE
        is_available=True,
    )
    # 2. Burger: explicit station=HOT_KITCHEN
    item_burger = Item(
        category_id=cat_hot_kitchen.id,
        name={"en": "Classic Burger", "ar": "برجر كلاسيك"},
        description=None,
        base_price=Decimal("40.00"),
        station=KitchenStation.HOT_KITCHEN,
        is_available=True,
    )
    # 3. Dessert item inside hot category: explicit station=DESSERT (overrides HOT_KITCHEN)
    item_dessert = Item(
        category_id=cat_hot_kitchen.id,
        name={"en": "Lava Cake", "ar": "كيكة الشوكولاتة"},
        description=None,
        base_price=Decimal("25.00"),
        station=KitchenStation.DESSERT,  # Overrides category station
        is_available=True,
    )
    # 4. Out of stock item (86 switch)
    item_sold_out = Item(
        category_id=cat_hot_kitchen.id,
        name={"en": "Wagyu Steak", "ar": "ستيك واغيو"},
        description=None,
        base_price=Decimal("150.00"),
        station=KitchenStation.HOT_KITCHEN,
        is_available=False,
    )
    test_session.add_all([item_drink, item_burger, item_dessert, item_sold_out])
    await test_session.flush()

    # Modifier group for Burger: Cheese
    group_cheese = ModifierGroup(
        item_id=item_burger.id,
        name={"en": "Cheese Choice", "ar": "نوع الجبن"},
        min_choices=1,
        max_choices=1,
        is_required=True,
    )
    test_session.add(group_cheese)
    await test_session.flush()

    opt_cheddar = ModifierOption(
        modifier_group_id=group_cheese.id,
        name={"en": "Cheddar", "ar": "شيدر"},
        price_delta=Decimal("3.00"),
        is_available=True,
    )
    test_session.add(opt_cheddar)
    await test_session.commit()

    # Tokens
    verified_guest_token = create_guest_session_jwt(
        session_id=uuid.uuid4(),
        tenant_id=tenant.id,
        branch_id=branch.id,
        table_id=table.id,
        table_number=table.table_number,
        is_presence_verified=True,
    )
    unverified_guest_token = create_guest_session_jwt(
        session_id=uuid.uuid4(),
        tenant_id=tenant.id,
        branch_id=branch.id,
        table_id=table.id,
        table_number=table.table_number,
        is_presence_verified=False,
    )
    admin_token = create_access_token(data={"sub": str(branch_admin.id), "tenant_id": str(tenant.id)})
    chef_token = create_access_token(data={"sub": str(kitchen_staff.id), "tenant_id": str(tenant.id)})

    return {
        "tenant": tenant,
        "branch": branch,
        "table": table,
        "table_inactive": table_inactive,
        "branch_admin": branch_admin,
        "kitchen_staff": kitchen_staff,
        "item_drink": item_drink,
        "item_burger": item_burger,
        "item_dessert": item_dessert,
        "item_sold_out": item_sold_out,
        "group_cheese": group_cheese,
        "opt_cheddar": opt_cheddar,
        "verified_guest_token": verified_guest_token,
        "unverified_guest_token": unverified_guest_token,
        "admin_token": admin_token,
        "chef_token": chef_token,
    }


@pytest_asyncio.fixture(scope="function")
async def test_client(
    async_test_engine: AsyncEngine,
    test_session: AsyncSession,
) -> AsyncGenerator[AsyncClient, None]:
    session_factory = async_sessionmaker(
        bind=async_test_engine,
        class_=AsyncSession,
        autoflush=False,
        expire_on_commit=False,
    )
    app = create_app()
    app.dependency_overrides[get_async_db] = lambda: test_session

    with patch("app.core.database.async_session_factory", session_factory), \
         patch("app.services.audit_service.async_session_factory", session_factory):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Test Cases
# ---------------------------------------------------------------------------

class TestOrderCheckoutPipeline:
    """ACID checkout pipeline, category station inheritance, and live shared cart."""

    @pytest.mark.asyncio
    async def test_category_station_inheritance(
        self,
        test_client: AsyncClient,
        seed_order_data: dict,
    ):
        """Verify item inherits Category.station when item.station is null, and overrides when set."""
        token = seed_order_data["verified_guest_token"]
        headers = {"Authorization": f"Bearer {token}", "Accept-Language": "en"}

        payload = {
            "items": [
                {
                    "item_id": str(seed_order_data["item_drink"].id),
                    "quantity": 1,
                    "selected_groups": [],
                },
                {
                    "item_id": str(seed_order_data["item_dessert"].id),
                    "quantity": 1,
                    "selected_groups": [],
                },
            ],
            "customer_notes": "Please serve cold first",
        }

        response = await test_client.post("/api/v1/orders/checkout", json=payload, headers=headers)
        assert response.status_code == 201
        data = response.json()

        items = data["items"]
        assert len(items) == 2

        # Drink item had station=None in category BEVERAGE -> should resolve to BEVERAGE
        drink_item = next(i for i in items if i["item_id"] == str(seed_order_data["item_drink"].id))
        assert drink_item["station"] == "BEVERAGE"

        # Dessert item had explicit station=DESSERT in category MAINS -> should resolve to DESSERT
        dessert_item = next(i for i in items if i["item_id"] == str(seed_order_data["item_dessert"].id))
        assert dessert_item["station"] == "DESSERT"

    @pytest.mark.asyncio
    async def test_verified_checkout_transitions_to_submitted_and_awaiting_food(
        self,
        test_client: AsyncClient,
        test_session: AsyncSession,
        seed_order_data: dict,
    ):
        """Verified presence session creates Order in SUBMITTED and table transitions to AWAITING_FOOD."""
        token = seed_order_data["verified_guest_token"]
        headers = {"Authorization": f"Bearer {token}"}

        payload = {
            "items": [
                {
                    "item_id": str(seed_order_data["item_burger"].id),
                    "quantity": 2,
                    "selected_groups": [
                        {
                            "group_id": str(seed_order_data["group_cheese"].id),
                            "option_ids": [str(seed_order_data["opt_cheddar"].id)],
                        }
                    ],
                }
            ]
        }

        response = await test_client.post("/api/v1/orders/checkout", json=payload, headers=headers)
        assert response.status_code == 201
        data = response.json()

        assert data["status"] == "SUBMITTED"

        # Pricing math:
        # Base: 40.00 + Cheddar: 3.00 = 43.00
        # Subtotal: 43.00 * 2 = 86.00
        # Tax: 86.00 * 0.15 = 12.90
        # Total: 86.00 + 12.90 = 98.90
        assert float(data["subtotal"]) == 86.00
        assert float(data["tax_total"]) == 12.90
        assert float(data["total_amount"]) == 98.90

        # Check Table status in DB
        table = await test_session.get(Table, seed_order_data["table"].id)
        assert table.status == TableStatus.AWAITING_FOOD

    @pytest.mark.asyncio
    async def test_unverified_checkout_routes_to_pending_staff_confirmation(
        self,
        test_client: AsyncClient,
        seed_order_data: dict,
    ):
        """Tier 3 unverified session creates Order in PENDING_STAFF_CONFIRMATION."""
        token = seed_order_data["unverified_guest_token"]
        headers = {"Authorization": f"Bearer {token}"}

        payload = {
            "items": [
                {
                    "item_id": str(seed_order_data["item_drink"].id),
                    "quantity": 1,
                    "selected_groups": [],
                }
            ]
        }

        response = await test_client.post("/api/v1/orders/checkout", json=payload, headers=headers)
        assert response.status_code == 201
        data = response.json()
        assert data["status"] == "PENDING_STAFF_CONFIRMATION"

    @pytest.mark.asyncio
    async def test_live_shared_cart_reordering_appends_to_existing_order(
        self,
        test_client: AsyncClient,
        seed_order_data: dict,
    ):
        """Consecutive checkouts on same table append items and accurately recompute VAT totals."""
        token = seed_order_data["verified_guest_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # 1. First order: 1 Drink (12.00)
        p1 = {
            "items": [
                {
                    "item_id": str(seed_order_data["item_drink"].id),
                    "quantity": 1,
                    "selected_groups": [],
                }
            ]
        }
        res1 = await test_client.post("/api/v1/orders/checkout", json=p1, headers=headers)
        assert res1.status_code == 201
        data1 = res1.json()
        order_id = data1["id"]
        assert float(data1["subtotal"]) == 12.00
        assert float(data1["tax_total"]) == 1.80
        assert float(data1["total_amount"]) == 13.80

        # 2. Second order from same table: 1 Dessert (25.00)
        p2 = {
            "items": [
                {
                    "item_id": str(seed_order_data["item_dessert"].id),
                    "quantity": 1,
                    "selected_groups": [],
                }
            ]
        }
        res2 = await test_client.post("/api/v1/orders/checkout", json=p2, headers=headers)
        assert res2.status_code == 201
        data2 = res2.json()

        # Must append to the same live order
        assert data2["id"] == order_id
        assert len(data2["items"]) == 2

        # Subtotal: 12.00 + 25.00 = 37.00
        # Tax: 37.00 * 0.15 = 5.55
        # Total: 37.00 + 5.55 = 42.55
        assert float(data2["subtotal"]) == 37.00
        assert float(data2["tax_total"]) == 5.55
        assert float(data2["total_amount"]) == 42.55

    @pytest.mark.asyncio
    async def test_checkout_sold_out_item_rejected_400(
        self,
        test_client: AsyncClient,
        seed_order_data: dict,
    ):
        """Checkout with item 86'd out-of-stock is rejected with 400 ITEM_UNAVAILABLE."""
        token = seed_order_data["verified_guest_token"]
        headers = {"Authorization": f"Bearer {token}", "Accept-Language": "en"}

        payload = {
            "items": [
                {
                    "item_id": str(seed_order_data["item_sold_out"].id),
                    "quantity": 1,
                    "selected_groups": [],
                }
            ]
        }
        res = await test_client.post("/api/v1/orders/checkout", json=payload, headers=headers)
        assert res.status_code == 400
        assert "unavailable" in res.json()["detail"].lower()


class TestOrderStateMachineAndTransitions:
    """FSM transition matrix, cancellation guards, staff transition API, and live tracker."""

    @pytest.mark.asyncio
    async def test_full_happy_path_fsm_lifecycle(
        self,
        test_client: AsyncClient,
        test_session: AsyncSession,
        seed_order_data: dict,
    ):
        """SUBMITTED -> PREPARING -> READY -> DELIVERED -> CLOSED synchronizes table status."""
        guest_token = seed_order_data["verified_guest_token"]
        admin_token = seed_order_data["admin_token"]
        branch_id = seed_order_data["branch"].id

        # 1. Checkout order -> SUBMITTED
        p = {
            "items": [
                {
                    "item_id": str(seed_order_data["item_drink"].id),
                    "quantity": 1,
                    "selected_groups": [],
                }
            ]
        }
        res = await test_client.post("/api/v1/orders/checkout", json=p, headers={"Authorization": f"Bearer {guest_token}"})
        assert res.status_code == 201
        order_id = res.json()["id"]

        staff_headers = {
            "Authorization": f"Bearer {admin_token}",
            "X-Branch-ID": str(branch_id),
        }

        # 2. SUBMITTED -> PREPARING
        t1 = await test_client.post(
            f"/api/v1/orders/{order_id}/transition",
            json={"target_status": "PREPARING"},
            headers=staff_headers,
        )
        assert t1.status_code == 200
        assert t1.json()["to_status"] == "PREPARING"

        # 3. PREPARING -> READY
        t2 = await test_client.post(
            f"/api/v1/orders/{order_id}/transition",
            json={"target_status": "READY"},
            headers=staff_headers,
        )
        assert t2.status_code == 200
        assert t2.json()["to_status"] == "READY"

        # 4. READY -> DELIVERED (table becomes EATING)
        t3 = await test_client.post(
            f"/api/v1/orders/{order_id}/transition",
            json={"target_status": "DELIVERED"},
            headers=staff_headers,
        )
        assert t3.status_code == 200
        assert t3.json()["to_status"] == "DELIVERED"
        assert t3.json()["table_status"] == "EATING"

        # 5. DELIVERED -> CLOSED (table becomes AVAILABLE)
        t4 = await test_client.post(
            f"/api/v1/orders/{order_id}/transition",
            json={"target_status": "CLOSED"},
            headers=staff_headers,
        )
        assert t4.status_code == 200
        assert t4.json()["to_status"] == "CLOSED"
        assert t4.json()["table_status"] == "AVAILABLE"

    @pytest.mark.asyncio
    async def test_illegal_fsm_transition_rejected_with_409(
        self,
        test_client: AsyncClient,
        seed_order_data: dict,
    ):
        """Direct jump from SUBMITTED to CLOSED violates FSM and is rejected with 409."""
        guest_token = seed_order_data["verified_guest_token"]
        admin_token = seed_order_data["admin_token"]
        branch_id = seed_order_data["branch"].id

        p = {
            "items": [
                {
                    "item_id": str(seed_order_data["item_drink"].id),
                    "quantity": 1,
                    "selected_groups": [],
                }
            ]
        }
        res = await test_client.post("/api/v1/orders/checkout", json=p, headers={"Authorization": f"Bearer {guest_token}"})
        order_id = res.json()["id"]

        staff_headers = {
            "Authorization": f"Bearer {admin_token}",
            "X-Branch-ID": str(branch_id),
            "Accept-Language": "en",
        }

        # Attempt illegal jump
        bad_res = await test_client.post(
            f"/api/v1/orders/{order_id}/transition",
            json={"target_status": "CLOSED"},
            headers=staff_headers,
        )
        assert bad_res.status_code == 409
        assert "Invalid order status transition" in bad_res.json()["detail"]

    @pytest.mark.asyncio
    async def test_customer_cancellation_guards(
        self,
        test_client: AsyncClient,
        seed_order_data: dict,
    ):
        """Customer can cancel unconfirmed orders, but cannot cancel in-progress (SUBMITTED/PREPARING)."""
        # Case A: Unverified order in PENDING_STAFF_CONFIRMATION can be cancelled by customer
        unverified_token = seed_order_data["unverified_guest_token"]
        p = {
            "items": [
                {"item_id": str(seed_order_data["item_drink"].id), "quantity": 1, "selected_groups": []}
            ]
        }
        res_unverified = await test_client.post(
            "/api/v1/orders/checkout",
            json=p,
            headers={"Authorization": f"Bearer {unverified_token}"},
        )
        unverified_order_id = res_unverified.json()["id"]

        cancel_res = await test_client.post(
            f"/api/v1/orders/{unverified_order_id}/cancel",
            headers={"Authorization": f"Bearer {unverified_token}"},
        )
        assert cancel_res.status_code == 200
        assert cancel_res.json()["to_status"] == "CANCELLED"

        # Case B: Verified order in SUBMITTED cannot be cancelled by customer
        verified_token = seed_order_data["verified_guest_token"]
        res_verified = await test_client.post(
            "/api/v1/orders/checkout",
            json=p,
            headers={"Authorization": f"Bearer {verified_token}"},
        )
        verified_order_id = res_verified.json()["id"]

        blocked_cancel = await test_client.post(
            f"/api/v1/orders/{verified_order_id}/cancel",
            headers={"Authorization": f"Bearer {verified_token}", "Accept-Language": "en"},
        )
        assert blocked_cancel.status_code == 403
        assert "cancelled by a branch administrator" in blocked_cancel.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_branch_admin_cancellation_with_reason(
        self,
        test_client: AsyncClient,
        seed_order_data: dict,
    ):
        """Branch admin can cancel in-progress orders with mandatory reason; missing reason returns 400."""
        guest_token = seed_order_data["verified_guest_token"]
        admin_token = seed_order_data["admin_token"]
        chef_token = seed_order_data["chef_token"]
        branch_id = seed_order_data["branch"].id

        p = {
            "items": [
                {"item_id": str(seed_order_data["item_drink"].id), "quantity": 1, "selected_groups": []}
            ]
        }
        res = await test_client.post("/api/v1/orders/checkout", json=p, headers={"Authorization": f"Bearer {guest_token}"})
        order_id = res.json()["id"]

        admin_headers = {"Authorization": f"Bearer {admin_token}", "X-Branch-ID": str(branch_id)}
        chef_headers = {"Authorization": f"Bearer {chef_token}", "X-Branch-ID": str(branch_id)}

        # 1. Non-admin staff (Chef) cannot cancel SUBMITTED order -> 403
        chef_res = await test_client.post(
            f"/api/v1/orders/{order_id}/transition",
            json={"target_status": "CANCELLED", "reason": "Customer walked out"},
            headers=chef_headers,
        )
        assert chef_res.status_code == 403

        # 2. Branch Admin missing cancellation reason -> 400
        admin_no_reason = await test_client.post(
            f"/api/v1/orders/{order_id}/transition",
            json={"target_status": "CANCELLED", "reason": ""},
            headers=admin_headers,
        )
        assert admin_no_reason.status_code == 400

        # 3. Branch Admin with reason -> 200
        admin_success = await test_client.post(
            f"/api/v1/orders/{order_id}/transition",
            json={"target_status": "CANCELLED", "reason": "Accidental duplicate order by guest"},
            headers=admin_headers,
        )
        assert admin_success.status_code == 200
        assert admin_success.json()["to_status"] == "CANCELLED"

    @pytest.mark.asyncio
    async def test_customer_live_tracker_get_active(
        self,
        test_client: AsyncClient,
        seed_order_data: dict,
    ):
        """GET /api/v1/orders/active retrieves live table order progress, and 404 when none."""
        guest_token = seed_order_data["verified_guest_token"]
        headers = {"Authorization": f"Bearer {guest_token}"}

        # Before ordering -> 404
        r0 = await test_client.get("/api/v1/orders/active", headers=headers)
        assert r0.status_code == 404

        # Place order
        p = {
            "items": [
                {"item_id": str(seed_order_data["item_drink"].id), "quantity": 1, "selected_groups": []}
            ]
        }
        r_create = await test_client.post("/api/v1/orders/checkout", json=p, headers=headers)
        order_id = r_create.json()["id"]

        # Active order now returned
        r_active = await test_client.get("/api/v1/orders/active", headers=headers)
        assert r_active.status_code == 200
        data = r_active.json()
        assert data["id"] == order_id
        assert data["status"] == "SUBMITTED"
        assert len(data["items"]) == 1
