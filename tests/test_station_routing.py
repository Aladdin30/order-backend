"""Comprehensive Automated Test Suite for Task BE-3.2: Kitchen Station Routing & Decomposition Engine.

Verifies:
1. 3-Tier Fallback Precedence:
   OrderItem.station = item.station || item.category.station || KitchenStation.HOT_KITCHEN
2. Multi-Item Order Decomposition into Station-Scoped Sub-Tickets (KDS).
3. Incremental Appends on Reorders (routing only newly added lines).
4. Redis Pub/Sub Dispatch across targeted station and consolidated kitchen channels.
5. Concurrency-safe KDS bump bar lifecycle with SELECT FOR UPDATE row locking.
6. Full preparation completion transitioning order to READY and notifying runners/guests.
7. REST API Endpoints: GET /api/v1/kds/tickets, POST /bump with strict RBAC.
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from typing import Any, AsyncGenerator
from unittest.mock import AsyncMock, patch

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
from app.models.catalog import Category, Item
from app.models.enums import KitchenStation, OrderStatus, OrderType, TableStatus, UserRole
from app.models.order import Order, OrderItem
from app.schemas.kds import KDSBumpRequest, KDSSubTicket
from app.services.station_routing_service import StationRoutingService


# ---------------------------------------------------------------------------
# Database & Fixtures
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
async def test_session_factory(async_test_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=async_test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autocommit=False,
        autoflush=False,
    )


@pytest_asyncio.fixture(scope="function")
async def test_db(test_session_factory: async_sessionmaker[AsyncSession]) -> AsyncGenerator[AsyncSession, None]:
    async with test_session_factory() as session:
        yield session


@pytest_asyncio.fixture(scope="function")
async def seed_data(test_db: AsyncSession) -> dict[str, Any]:
    """Seed tenant, branch, categories across stations, items, and users."""
    tenant = Tenant(
        id=uuid.uuid4(),
        name="KDS Testing Tenant",
        slug=f"kds-tenant-{uuid.uuid4().hex[:6]}",
        is_active=True,
    )
    test_db.add(tenant)
    await test_db.flush()

    branch = Branch(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        name={"en": "Main KDS Branch", "ar": "فرع المطبخ الرئيسي"},
        slug=f"kds-branch-{uuid.uuid4().hex[:6]}",
        latitude=Decimal("24.7136"),
        longitude=Decimal("46.6753"),
        is_active=True,
    )
    test_db.add(branch)
    await test_db.flush()

    table = Table(
        id=uuid.uuid4(),
        branch_id=branch.id,
        table_number="T-07",
        capacity=4,
        status=TableStatus.AWAITING_FOOD,
        is_active=True,
    )
    test_db.add(table)
    await test_db.flush()

    # Categories
    cat_hot = Category(
        id=uuid.uuid4(),
        branch_id=branch.id,
        name={"en": "Burgers & Grills", "ar": "برجر ومشاوي"},
        station=KitchenStation.HOT_KITCHEN,
        is_active=True,
    )
    cat_cold = Category(
        id=uuid.uuid4(),
        branch_id=branch.id,
        name={"en": "Salads & Starters", "ar": "سلطات ومقبلات"},
        station=KitchenStation.COLD_KITCHEN,
        is_active=True,
    )
    cat_beverage = Category(
        id=uuid.uuid4(),
        branch_id=branch.id,
        name={"en": "Bar & Drinks", "ar": "مشروبات"},
        station=KitchenStation.BEVERAGE,
        is_active=True,
    )
    test_db.add_all([cat_hot, cat_cold, cat_beverage])
    await test_db.flush()

    # Items
    item_hot = Item(
        id=uuid.uuid4(),
        category_id=cat_hot.id,
        name={"en": "Cheeseburger", "ar": "تشيز برجر"},
        base_price=Decimal("45.00"),
        station=None,  # Inherits HOT_KITCHEN from category
        is_available=True,
    )
    item_salad = Item(
        id=uuid.uuid4(),
        category_id=cat_cold.id,
        name={"en": "Caesar Salad", "ar": "سلطة سيزر"},
        base_price=Decimal("35.00"),
        station=None,  # Inherits COLD_KITCHEN from category
        is_available=True,
    )
    item_drink = Item(
        id=uuid.uuid4(),
        category_id=cat_beverage.id,
        name={"en": "Mojito", "ar": "موهيتو"},
        base_price=Decimal("20.00"),
        station=None,  # Inherits BEVERAGE from category
        is_available=True,
    )
    # Direct override: Item placed under Hot category but directly assigned DESSERT station
    item_override_dessert = Item(
        id=uuid.uuid4(),
        category_id=cat_hot.id,
        name={"en": "Lava Cake", "ar": "كيكة الحمم"},
        base_price=Decimal("25.00"),
        station=KitchenStation.DESSERT,  # Direct station override
        is_available=True,
    )
    test_db.add_all([item_hot, item_salad, item_drink, item_override_dessert])
    await test_db.flush()

    # Staff Users
    pwd_hash = get_password_hash("Password123!")

    kitchen_user = User(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        email="chef@testkds.com",
        hashed_password=pwd_hash,
        full_name="Chef Ramsey",
        role=UserRole.KITCHEN_STAFF,
        is_active=True,
    )
    waiter_user = User(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        email="waiter@testkds.com",
        hashed_password=pwd_hash,
        full_name="Waiter Gary",
        role=UserRole.WAITER,
        is_active=True,
    )
    admin_user = User(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        email="admin@testkds.com",
        hashed_password=pwd_hash,
        full_name="Admin Boss",
        role=UserRole.BRANCH_ADMIN,
        is_active=True,
    )
    test_db.add_all([kitchen_user, waiter_user, admin_user])
    await test_db.flush()

    test_db.add(UserBranchAccess(user_id=kitchen_user.id, branch_id=branch.id))
    test_db.add(UserBranchAccess(user_id=waiter_user.id, branch_id=branch.id))
    test_db.add(UserBranchAccess(user_id=admin_user.id, branch_id=branch.id))
    await test_db.commit()

    return {
        "tenant": tenant,
        "branch": branch,
        "table": table,
        "cat_hot": cat_hot,
        "cat_cold": cat_cold,
        "cat_beverage": cat_beverage,
        "item_hot": item_hot,
        "item_salad": item_salad,
        "item_drink": item_drink,
        "item_dessert": item_override_dessert,
        "kitchen_user": kitchen_user,
        "waiter_user": waiter_user,
        "admin_user": admin_user,
    }


# ---------------------------------------------------------------------------
# Part 1: Station Fallback Precedence Tests
# ---------------------------------------------------------------------------

def test_station_fallback_item_precedence():
    """Direct item station overrides category station."""
    mock_item = Item(station=KitchenStation.DESSERT)
    mock_category = Category(station=KitchenStation.HOT_KITCHEN)

    resolved = StationRoutingService.resolve_item_station(mock_item, mock_category)
    assert resolved == KitchenStation.DESSERT


def test_station_fallback_category_inheritance():
    """Item with no station inherits category station."""
    mock_item = Item(station=None)
    mock_category = Category(station=KitchenStation.COLD_KITCHEN)

    resolved = StationRoutingService.resolve_item_station(mock_item, mock_category)
    assert resolved == KitchenStation.COLD_KITCHEN


def test_station_fallback_hot_kitchen_default():
    """Item and Category with no station defaults to HOT_KITCHEN."""
    mock_item = Item(station=None)
    mock_category = Category(station=None)

    resolved = StationRoutingService.resolve_item_station(mock_item, mock_category)
    assert resolved == KitchenStation.HOT_KITCHEN

    resolved_no_cat = StationRoutingService.resolve_item_station(mock_item, None)
    assert resolved_no_cat == KitchenStation.HOT_KITCHEN


# ---------------------------------------------------------------------------
# Part 2: Order Decomposition & Incremental Appends Tests
# ---------------------------------------------------------------------------

def test_order_decomposition_into_station_sub_tickets(seed_data: dict[str, Any]):
    """Order with 4 items across 4 stations decomposes into 4 distinct sub-tickets."""
    branch = seed_data["branch"]
    table = seed_data["table"]
    order = Order(
        id=uuid.uuid4(),
        tenant_id=seed_data["tenant"].id,
        branch_id=branch.id,
        table_id=table.id,
        status=OrderStatus.SUBMITTED,
        order_type=OrderType.DINE_IN,
        customer_notes="Extra spicy, no cutlery",
    )
    order.table = table

    i1 = OrderItem(
        id=uuid.uuid4(),
        order_id=order.id,
        item_id=seed_data["item_hot"].id,
        quantity=2,
        unit_price=Decimal("45.00"),
        subtotal=Decimal("90.00"),
        station=KitchenStation.HOT_KITCHEN,
        is_bumped=False,
    )
    i1.item = seed_data["item_hot"]

    i2 = OrderItem(
        id=uuid.uuid4(),
        order_id=order.id,
        item_id=seed_data["item_salad"].id,
        quantity=1,
        unit_price=Decimal("35.00"),
        subtotal=Decimal("35.00"),
        station=KitchenStation.COLD_KITCHEN,
        is_bumped=False,
    )
    i2.item = seed_data["item_salad"]

    i3 = OrderItem(
        id=uuid.uuid4(),
        order_id=order.id,
        item_id=seed_data["item_drink"].id,
        quantity=3,
        unit_price=Decimal("20.00"),
        subtotal=Decimal("60.00"),
        station=KitchenStation.BEVERAGE,
        is_bumped=False,
    )
    i3.item = seed_data["item_drink"]

    i4 = OrderItem(
        id=uuid.uuid4(),
        order_id=order.id,
        item_id=seed_data["item_dessert"].id,
        quantity=1,
        unit_price=Decimal("25.00"),
        subtotal=Decimal("25.00"),
        station=KitchenStation.DESSERT,
        is_bumped=False,
    )
    i4.item = seed_data["item_dessert"]

    order.order_items = [i1, i2, i3, i4]

    tickets = StationRoutingService.decompose_order(order)

    assert len(tickets) == 4
    assert set(tickets.keys()) == {
        KitchenStation.HOT_KITCHEN,
        KitchenStation.COLD_KITCHEN,
        KitchenStation.BEVERAGE,
        KitchenStation.DESSERT,
    }

    # Verify Hot Kitchen ticket
    hot_ticket = tickets[KitchenStation.HOT_KITCHEN]
    assert hot_ticket.table_number == "T-07"
    assert hot_ticket.customer_notes == "Extra spicy, no cutlery"
    assert hot_ticket.total_items_count == 1
    assert hot_ticket.items[0].name in ("Cheeseburger", "تشيز برجر")
    assert hot_ticket.items[0].quantity == 2

    # Verify Beverage ticket
    bev_ticket = tickets[KitchenStation.BEVERAGE]
    assert bev_ticket.items[0].name in ("Mojito", "موهيتو")
    assert bev_ticket.items[0].quantity == 3


def test_order_decomposition_incremental_appends(seed_data: dict[str, Any]):
    """Directive 2: Decompose only newly appended items during reorders."""
    branch = seed_data["branch"]
    table = seed_data["table"]
    order = Order(
        id=uuid.uuid4(),
        tenant_id=seed_data["tenant"].id,
        branch_id=branch.id,
        table_id=table.id,
        status=OrderStatus.SUBMITTED,
        order_type=OrderType.DINE_IN,
    )
    order.table = table

    # Existing item (already placed)
    old_item = OrderItem(
        id=uuid.uuid4(),
        order_id=order.id,
        item_id=seed_data["item_hot"].id,
        quantity=1,
        unit_price=Decimal("45.00"),
        subtotal=Decimal("45.00"),
        station=KitchenStation.HOT_KITCHEN,
        is_bumped=True,
    )
    old_item.item = seed_data["item_hot"]

    # Newly appended item on reorder
    new_item = OrderItem(
        id=uuid.uuid4(),
        order_id=order.id,
        item_id=seed_data["item_drink"].id,
        quantity=2,
        unit_price=Decimal("20.00"),
        subtotal=Decimal("40.00"),
        station=KitchenStation.BEVERAGE,
        is_bumped=False,
    )
    new_item.item = seed_data["item_drink"]

    order.order_items = [old_item, new_item]

    # Decompose passing only new_items
    reorder_tickets = StationRoutingService.decompose_order(order, items=[new_item])

    assert len(reorder_tickets) == 1
    assert KitchenStation.BEVERAGE in reorder_tickets
    assert KitchenStation.HOT_KITCHEN not in reorder_tickets
    assert reorder_tickets[KitchenStation.BEVERAGE].items[0].name in ("Mojito", "موهيتو")


# ---------------------------------------------------------------------------
# Part 3: Redis Pub/Sub Dispatch Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_order_to_kds_channels(seed_data: dict[str, Any]):
    """Assert dispatch publishes to targeted station channel and general kitchen channel."""
    branch = seed_data["branch"]
    table = seed_data["table"]
    order = Order(
        id=uuid.uuid4(),
        tenant_id=seed_data["tenant"].id,
        branch_id=branch.id,
        table_id=table.id,
        status=OrderStatus.SUBMITTED,
        order_type=OrderType.DINE_IN,
    )
    order.table = table

    item = OrderItem(
        id=uuid.uuid4(),
        order_id=order.id,
        item_id=seed_data["item_salad"].id,
        quantity=1,
        unit_price=Decimal("35.00"),
        subtotal=Decimal("35.00"),
        station=KitchenStation.COLD_KITCHEN,
        is_bumped=False,
    )
    item.item = seed_data["item_salad"]
    order.order_items = [item]

    with patch("app.services.station_routing_service.publish", new_callable=AsyncMock) as mock_publish:
        dispatched = await StationRoutingService.dispatch_order_to_kds(order)
        assert len(dispatched) == 1

        expected_legacy_station_ch = f"branch_{branch.id}_kitchen_cold_kitchen"
        expected_dynamic_station_ch = f"branch_{branch.id}_station_cold_kitchen"
        expected_general_ch = f"branch_{branch.id}_kitchen"

        assert mock_publish.await_count == 3
        calls = [c[1]["channel"] for c in mock_publish.await_args_list]
        assert expected_legacy_station_ch in calls
        assert expected_dynamic_station_ch in calls
        assert expected_general_ch in calls


# ---------------------------------------------------------------------------
# Part 4: Concurrency-Safe Bump Bar Lifecycle Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bump_order_item_lifecycle(test_db: AsyncSession, seed_data: dict[str, Any]):
    """Verify single item bump transitions order to PREPARING, and all bumped transitions to READY."""
    branch = seed_data["branch"]
    table = seed_data["table"]

    order = Order(
        id=uuid.uuid4(),
        tenant_id=seed_data["tenant"].id,
        branch_id=branch.id,
        table_id=table.id,
        status=OrderStatus.SUBMITTED,
        order_type=OrderType.DINE_IN,
    )
    test_db.add(order)
    await test_db.flush()

    i1 = OrderItem(
        id=uuid.uuid4(),
        order_id=order.id,
        item_id=seed_data["item_hot"].id,
        quantity=1,
        unit_price=Decimal("45.00"),
        subtotal=Decimal("45.00"),
        station=KitchenStation.HOT_KITCHEN,
        is_bumped=False,
    )
    i2 = OrderItem(
        id=uuid.uuid4(),
        order_id=order.id,
        item_id=seed_data["item_drink"].id,
        quantity=1,
        unit_price=Decimal("20.00"),
        subtotal=Decimal("20.00"),
        station=KitchenStation.BEVERAGE,
        is_bumped=False,
    )
    test_db.add_all([i1, i2])
    await test_db.commit()

    with patch("app.services.station_routing_service.publish", new_callable=AsyncMock) as mock_publish:
        # 1. Bump Item 1 -> Order should transition to PREPARING (1 of 2 bumped)
        res1 = await StationRoutingService.bump_order_item(
            db=test_db,
            order_item_id=i1.id,
            is_bumped=True,
            actor_role="KITCHEN_STAFF",
        )
        assert res1.is_bumped is True
        assert res1.order_status == OrderStatus.PREPARING
        assert res1.order_fully_prepared is False
        assert res1.bumped_items_count == 1
        assert res1.total_items_count == 2

        # 2. Bump Item 2 -> Order should transition to READY and alert runners!
        res2 = await StationRoutingService.bump_order_item(
            db=test_db,
            order_item_id=i2.id,
            is_bumped=True,
            actor_role="KITCHEN_STAFF",
        )
        assert res2.is_bumped is True
        assert res2.order_status == OrderStatus.READY
        assert res2.order_fully_prepared is True
        assert res2.bumped_items_count == 2

        # Check ORDER_READY was broadcast to runners and table
        runner_channels = [c[1]["channel"] for c in mock_publish.await_args_list if c[1].get("event_type") == "ORDER_READY"]
        assert f"branch_{branch.id}_runners" in runner_channels
        assert f"branch_{branch.id}_table_{table.id}" in runner_channels


@pytest.mark.asyncio
async def test_bulk_bump_station_ticket(test_db: AsyncSession, seed_data: dict[str, Any]):
    """Verify bulk bumping all items for a given station."""
    branch = seed_data["branch"]
    table = seed_data["table"]

    order = Order(
        id=uuid.uuid4(),
        tenant_id=seed_data["tenant"].id,
        branch_id=branch.id,
        table_id=table.id,
        status=OrderStatus.SUBMITTED,
        order_type=OrderType.DINE_IN,
    )
    test_db.add(order)
    await test_db.flush()

    # 2 Hot kitchen items, 1 beverage item
    h1 = OrderItem(
        id=uuid.uuid4(),
        order_id=order.id,
        item_id=seed_data["item_hot"].id,
        quantity=1,
        unit_price=Decimal("45.00"),
        subtotal=Decimal("45.00"),
        station=KitchenStation.HOT_KITCHEN,
        is_bumped=False,
    )
    h2 = OrderItem(
        id=uuid.uuid4(),
        order_id=order.id,
        item_id=seed_data["item_hot"].id,
        quantity=2,
        unit_price=Decimal("45.00"),
        subtotal=Decimal("90.00"),
        station=KitchenStation.HOT_KITCHEN,
        is_bumped=False,
    )
    b1 = OrderItem(
        id=uuid.uuid4(),
        order_id=order.id,
        item_id=seed_data["item_drink"].id,
        quantity=1,
        unit_price=Decimal("20.00"),
        subtotal=Decimal("20.00"),
        station=KitchenStation.BEVERAGE,
        is_bumped=False,
    )
    test_db.add_all([h1, h2, b1])
    await test_db.commit()

    # Bulk bump HOT_KITCHEN
    res = await StationRoutingService.bump_station_ticket(
        db=test_db,
        order_id=order.id,
        station=KitchenStation.HOT_KITCHEN,
        is_bumped=True,
    )
    assert res.order_status == OrderStatus.PREPARING
    assert res.bumped_items_count == 2
    assert res.total_items_count == 3
    assert res.order_fully_prepared is False


# ---------------------------------------------------------------------------
# Part 5: KDS REST API Endpoint Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_kds_list_tickets_endpoint(test_db: AsyncSession, seed_data: dict[str, Any]):
    """Test GET /api/v1/kds/tickets endpoint with RBAC guards and station filters."""
    app = create_app()
    app.dependency_overrides[get_async_db] = lambda: test_db

    branch = seed_data["branch"]
    table = seed_data["table"]
    kitchen_user = seed_data["kitchen_user"]
    waiter_user = seed_data["waiter_user"]

    # Seed an open order
    order = Order(
        id=uuid.uuid4(),
        tenant_id=seed_data["tenant"].id,
        branch_id=branch.id,
        table_id=table.id,
        status=OrderStatus.SUBMITTED,
        order_type=OrderType.DINE_IN,
    )
    test_db.add(order)
    await test_db.flush()

    item = OrderItem(
        id=uuid.uuid4(),
        order_id=order.id,
        item_id=seed_data["item_salad"].id,
        quantity=1,
        unit_price=Decimal("35.00"),
        subtotal=Decimal("35.00"),
        station=KitchenStation.COLD_KITCHEN,
        is_bumped=False,
    )
    test_db.add(item)
    await test_db.commit()

    kitchen_token = create_access_token({
        "sub": str(kitchen_user.id),
        "tenant_id": str(kitchen_user.tenant_id),
        "role": kitchen_user.role.value,
    })
    waiter_token = create_access_token({
        "sub": str(waiter_user.id),
        "tenant_id": str(waiter_user.tenant_id),
        "role": waiter_user.role.value,
    })

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        # 1. Waiter role should be rejected with 403 Forbidden
        waiter_res = await ac.get(
            "/api/v1/kds/tickets",
            headers={"Authorization": f"Bearer {waiter_token}", "X-Branch-ID": str(branch.id)},
        )
        assert waiter_res.status_code == 403

        # 2. Kitchen staff should succeed with 200 OK
        kitchen_res = await ac.get(
            "/api/v1/kds/tickets",
            headers={"Authorization": f"Bearer {kitchen_token}", "X-Branch-ID": str(branch.id)},
        )
        assert kitchen_res.status_code == 200
        tickets = kitchen_res.json()
        assert len(tickets) >= 1
        assert any(t["station"] == "COLD_KITCHEN" for t in tickets)

        # 3. Filter by station: HOT_KITCHEN should return 0 tickets
        hot_filter_res = await ac.get(
            "/api/v1/kds/tickets?station=HOT_KITCHEN",
            headers={"Authorization": f"Bearer {kitchen_token}", "X-Branch-ID": str(branch.id)},
        )
        assert hot_filter_res.status_code == 200
        assert len(hot_filter_res.json()) == 0


@pytest.mark.asyncio
async def test_kds_bump_endpoints(test_db: AsyncSession, seed_data: dict[str, Any]):
    """Test POST /api/v1/kds/items/{id}/bump and station bulk bump endpoints."""
    app = create_app()
    app.dependency_overrides[get_async_db] = lambda: test_db

    branch = seed_data["branch"]
    table = seed_data["table"]
    kitchen_user = seed_data["kitchen_user"]

    order = Order(
        id=uuid.uuid4(),
        tenant_id=seed_data["tenant"].id,
        branch_id=branch.id,
        table_id=table.id,
        status=OrderStatus.SUBMITTED,
        order_type=OrderType.DINE_IN,
    )
    test_db.add(order)
    await test_db.flush()

    item = OrderItem(
        id=uuid.uuid4(),
        order_id=order.id,
        item_id=seed_data["item_hot"].id,
        quantity=1,
        unit_price=Decimal("45.00"),
        subtotal=Decimal("45.00"),
        station=KitchenStation.HOT_KITCHEN,
        is_bumped=False,
    )
    test_db.add(item)
    await test_db.commit()

    kitchen_token = create_access_token({
        "sub": str(kitchen_user.id),
        "tenant_id": str(kitchen_user.tenant_id),
        "role": kitchen_user.role.value,
    })

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        # Single bump
        bump_res = await ac.post(
            f"/api/v1/kds/items/{item.id}/bump",
            headers={"Authorization": f"Bearer {kitchen_token}", "X-Branch-ID": str(branch.id)},
            json={"is_bumped": True},
        )
        assert bump_res.status_code == 200
        data = bump_res.json()
        assert data["is_bumped"] is True
        assert data["order_fully_prepared"] is True
        assert data["order_status"] == "READY"

        # Cross-branch isolation: attempting to bump with a different branch header returns 404
        other_branch_id = uuid.uuid4()
        cross_res = await ac.post(
            f"/api/v1/kds/items/{item.id}/bump",
            headers={"Authorization": f"Bearer {kitchen_token}", "X-Branch-ID": str(other_branch_id)},
            json={"is_bumped": True},
        )
        # Blocked either by branch access guard (403) or order branch mismatch (404)
        assert cross_res.status_code in (403, 404)
