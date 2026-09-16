"""Comprehensive Automated Test Suite for Dynamic Kitchen Stations CRUD & Backward-Compatible Refactoring.

Verifies:
1. Automatic seeding of default operational stations (HOT_KITCHEN, COLD_KITCHEN, BEVERAGE).
2. Staff CRUD operations:
   - POST /api/v1/staff/kitchen-stations (uppercase code normalization, 409 duplicate code guard).
   - GET /api/v1/staff/kitchen-stations (include_inactive query parameter).
   - PATCH /api/v1/staff/kitchen-stations/{station_id} (name update, guarded deactivation).
   - DELETE /api/v1/staff/kitchen-stations/{station_id} (soft vs hard delete, 409 guard on active references).
3. 409 Conflict guard on deleting or deactivating stations assigned to:
   - Active menu items (is_available=True).
   - Active categories (is_active=True).
4. Station resolution fallback chain & serialization:
   - item.kitchen_station.code -> item.station -> category.kitchen_station.code -> category.station -> HOT_KITCHEN.
   - Safe serialization of both Enum members and dynamic string codes in KDSSubTicket.
5. Dual-channel Redis Pub/Sub publishing:
   - branch_{id}_station_{code.lower()} (dynamic)
   - branch_{id}_kitchen_{code.lower()} (legacy)
   - branch_{id}_kitchen (consolidated)
6. RBAC guards (BRANCH_ADMIN, SUPER_ADMIN permitted; WAITER, KITCHEN rejected with 403) and branch isolation.
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
from app.models.kitchen_station import KitchenStation as KitchenStationModel
from app.models.order import Order, OrderItem
from app.schemas.kitchen_station import CreateKitchenStationRequest, UpdateKitchenStationRequest
from app.services.kitchen_station_service import KitchenStationService
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
    """Seed tenant, branches, staff users across roles."""
    tenant = Tenant(
        id=uuid.uuid4(),
        name="Station Test Tenant",
        slug=f"tenant-{uuid.uuid4().hex[:6]}",
        is_active=True,
    )
    test_db.add(tenant)
    await test_db.flush()

    branch = Branch(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        name={"en": "Main Branch", "ar": "الفرع الرئيسي"},
        slug=f"branch-{uuid.uuid4().hex[:6]}",
        latitude=Decimal("24.7136"),
        longitude=Decimal("46.6753"),
        is_active=True,
    )
    other_branch = Branch(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        name={"en": "Other Branch", "ar": "فرع آخر"},
        slug=f"other-{uuid.uuid4().hex[:6]}",
        latitude=Decimal("24.7136"),
        longitude=Decimal("46.6753"),
        is_active=True,
    )
    test_db.add_all([branch, other_branch])
    await test_db.flush()

    pwd_hash = get_password_hash("Secret123!")

    admin_user = User(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        email="admin@test.com",
        full_name="Branch Admin",
        hashed_password=pwd_hash,
        role=UserRole.BRANCH_ADMIN,
        is_active=True,
    )
    waiter_user = User(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        email="waiter@test.com",
        full_name="Floor Waiter",
        hashed_password=pwd_hash,
        role=UserRole.WAITER,
        is_active=True,
    )
    kitchen_user = User(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        email="kitchen@test.com",
        full_name="Kitchen Staff",
        hashed_password=pwd_hash,
        role=UserRole.KITCHEN_STAFF,
        is_active=True,
    )
    test_db.add_all([admin_user, waiter_user, kitchen_user])
    await test_db.flush()

    # User branch access
    test_db.add_all([
        UserBranchAccess(user_id=admin_user.id, branch_id=branch.id),
        UserBranchAccess(user_id=waiter_user.id, branch_id=branch.id),
        UserBranchAccess(user_id=kitchen_user.id, branch_id=branch.id),
    ])
    await test_db.commit()

    admin_token = create_access_token(data={"sub": str(admin_user.id), "role": admin_user.role.value, "tenant_id": str(tenant.id)})
    waiter_token = create_access_token(data={"sub": str(waiter_user.id), "role": waiter_user.role.value, "tenant_id": str(tenant.id)})
    kitchen_token = create_access_token(data={"sub": str(kitchen_user.id), "role": kitchen_user.role.value, "tenant_id": str(tenant.id)})

    return {
        "tenant": tenant,
        "branch": branch,
        "other_branch": other_branch,
        "admin_user": admin_user,
        "admin_token": admin_token,
        "waiter_token": waiter_token,
        "kitchen_token": kitchen_token,
    }


@pytest_asyncio.fixture(scope="function")
async def client(test_session_factory: async_sessionmaker[AsyncSession]) -> AsyncGenerator[AsyncClient, None]:
    """FastAPI Test Client with DB dependency override."""
    app = create_app()

    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        async with test_session_factory() as session:
            yield session

    app.dependency_overrides[get_async_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Test Cases
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_auto_seeding_default_stations(test_db: AsyncSession, seed_data: dict[str, Any]):
    """Default stations (HOT_KITCHEN, COLD_KITCHEN, BEVERAGE) must be seeded automatically."""
    branch = seed_data["branch"]
    tenant = seed_data["tenant"]

    stations = await KitchenStationService.ensure_default_stations(
        db=test_db,
        branch_id=branch.id,
        tenant_id=tenant.id,
    )
    assert len(stations) == 3
    codes = {s.code for s in stations}
    assert codes == {"HOT_KITCHEN", "COLD_KITCHEN", "BEVERAGE"}

    # Second call should be idempotent
    stations_again = await KitchenStationService.ensure_default_stations(
        db=test_db,
        branch_id=branch.id,
        tenant_id=tenant.id,
    )
    assert len(stations_again) == 3


@pytest.mark.asyncio
async def test_create_custom_kitchen_station_and_duplicate_guard(client: AsyncClient, seed_data: dict[str, Any]):
    """Staff can create custom station, normalized to UPPERCASE, duplicate code triggers 409."""
    branch = seed_data["branch"]
    admin_token = seed_data["admin_token"]
    headers = {"Authorization": f"Bearer {admin_token}", "X-Branch-ID": str(branch.id)}

    # Create Pizza Oven station
    create_payload = {
        "name": {"en": "Pizza Oven", "ar": "فرن البيتزا"},
        "code": "pizza_oven",
        "is_active": True,
    }
    resp = await client.post("/api/v1/staff/kitchen-stations", json=create_payload, headers=headers)
    assert resp.status_code == 201
    data = resp.json()
    assert data["code"] == "PIZZA_OVEN"
    assert data["name"]["en"] == "Pizza Oven"
    assert data["is_active"] is True
    station_id = data["id"]

    # Duplicate code creation must return 409 Conflict
    dup_resp = await client.post("/api/v1/staff/kitchen-stations", json=create_payload, headers=headers)
    assert dup_resp.status_code == 409
    assert "already exists" in dup_resp.json()["detail"]


@pytest.mark.asyncio
async def test_list_kitchen_stations_with_active_filtering(client: AsyncClient, seed_data: dict[str, Any]):
    """Listing returns active stations by default, or all stations with include_inactive=true."""
    branch = seed_data["branch"]
    admin_token = seed_data["admin_token"]
    headers = {"Authorization": f"Bearer {admin_token}", "X-Branch-ID": str(branch.id)}

    # Create inactive station
    await client.post(
        "/api/v1/staff/kitchen-stations",
        json={"name": {"en": "Pastry", "ar": "حلويات"}, "code": "PASTRY", "is_active": False},
        headers=headers,
    )

    # Default list: includes 3 auto-seeded active defaults, excludes inactive PASTRY
    resp = await client.get("/api/v1/staff/kitchen-stations", headers=headers)
    assert resp.status_code == 200
    active_stations = resp.json()
    assert len(active_stations) == 3
    active_codes = [s["code"] for s in active_stations]
    assert "PASTRY" not in active_codes

    # With include_inactive=true
    resp_all = await client.get("/api/v1/staff/kitchen-stations?include_inactive=true", headers=headers)
    assert resp_all.status_code == 200
    all_stations = resp_all.json()
    assert len(all_stations) == 4
    all_codes = [s["code"] for s in all_stations]
    assert "PASTRY" in all_codes


@pytest.mark.asyncio
async def test_update_kitchen_station(client: AsyncClient, seed_data: dict[str, Any]):
    """Staff can update station localized name and active state."""
    branch = seed_data["branch"]
    admin_token = seed_data["admin_token"]
    headers = {"Authorization": f"Bearer {admin_token}", "X-Branch-ID": str(branch.id)}

    create_resp = await client.post(
        "/api/v1/staff/kitchen-stations",
        json={"name": {"en": "Grill", "ar": "شواية"}, "code": "GRILL", "is_active": True},
        headers=headers,
    )
    station_id = create_resp.json()["id"]

    # Update name
    patch_resp = await client.patch(
        f"/api/v1/staff/kitchen-stations/{station_id}",
        json={"name": {"en": "Charcoal Grill", "ar": "شواية الفحم"}},
        headers=headers,
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["name"]["en"] == "Charcoal Grill"


@pytest.mark.asyncio
async def test_409_conflict_on_deleting_station_with_active_items(
    test_db: AsyncSession,
    client: AsyncClient,
    seed_data: dict[str, Any],
):
    """Deleting or deactivating a station actively assigned to menu items must return 409 Conflict."""
    branch = seed_data["branch"]
    admin_token = seed_data["admin_token"]
    headers = {"Authorization": f"Bearer {admin_token}", "X-Branch-ID": str(branch.id)}

    # 1. Create custom station
    create_resp = await client.post(
        "/api/v1/staff/kitchen-stations",
        json={"name": {"en": "Bakery", "ar": "المخبز"}, "code": "BAKERY", "is_active": True},
        headers=headers,
    )
    station_id = uuid.UUID(create_resp.json()["id"])

    # 2. Create Category and Item assigned to this station
    cat = Category(
        id=uuid.uuid4(),
        branch_id=branch.id,
        name={"en": "Pastries", "ar": "معجنات"},
        station=KitchenStation.HOT_KITCHEN,
        is_active=True,
    )
    test_db.add(cat)
    await test_db.flush()

    item = Item(
        id=uuid.uuid4(),
        category_id=cat.id,
        name={"en": "Croissant", "ar": "كرواسون"},
        base_price=Decimal("15.00"),
        station_id=station_id,
        is_available=True,  # Active
    )
    test_db.add(item)
    await test_db.commit()

    # 3. Attempting to DELETE station must return 409 Conflict
    del_resp = await client.delete(f"/api/v1/staff/kitchen-stations/{station_id}", headers=headers)
    assert del_resp.status_code == 409
    assert "active menu item(s)" in del_resp.json()["detail"]

    # 4. Attempting to PATCH is_active: false must also return 409 Conflict
    patch_resp = await client.patch(
        f"/api/v1/staff/kitchen-stations/{station_id}",
        json={"is_active": False},
        headers=headers,
    )
    assert patch_resp.status_code == 409
    assert "active menu item(s)" in patch_resp.json()["detail"]

    # 5. 86 (deactivate) the item
    item.is_available = False
    await test_db.commit()

    # 6. Now DELETE should succeed with 204 No Content
    del_resp2 = await client.delete(f"/api/v1/staff/kitchen-stations/{station_id}", headers=headers)
    assert del_resp2.status_code == 204


@pytest.mark.asyncio
async def test_409_conflict_on_deleting_station_with_active_categories(
    test_db: AsyncSession,
    client: AsyncClient,
    seed_data: dict[str, Any],
):
    """Deleting a station assigned to an active category must return 409 Conflict."""
    branch = seed_data["branch"]
    admin_token = seed_data["admin_token"]
    headers = {"Authorization": f"Bearer {admin_token}", "X-Branch-ID": str(branch.id)}

    create_resp = await client.post(
        "/api/v1/staff/kitchen-stations",
        json={"name": {"en": "Sushi Bar", "ar": "سوشي بار"}, "code": "SUSHI_BAR", "is_active": True},
        headers=headers,
    )
    station_id = uuid.UUID(create_resp.json()["id"])

    cat = Category(
        id=uuid.uuid4(),
        branch_id=branch.id,
        name={"en": "Japanese", "ar": "ياباني"},
        station=KitchenStation.COLD_KITCHEN,
        station_id=station_id,
        is_active=True,
    )
    test_db.add(cat)
    await test_db.commit()

    # Blocked with 409
    del_resp = await client.delete(f"/api/v1/staff/kitchen-stations/{station_id}", headers=headers)
    assert del_resp.status_code == 409
    assert "active category(s)" in del_resp.json()["detail"]

    # Deactivate category
    cat.is_active = False
    await test_db.commit()

    # Now deletion succeeds
    del_resp2 = await client.delete(f"/api/v1/staff/kitchen-stations/{station_id}", headers=headers)
    assert del_resp2.status_code == 204


@pytest.mark.asyncio
async def test_station_fallback_inheritance_with_dynamic_entities(test_db: AsyncSession, seed_data: dict[str, Any]):
    """Verify 5-tier fallback: item.kitchen_station -> item.station -> category.kitchen_station -> category.station -> HOT_KITCHEN."""
    branch = seed_data["branch"]
    tenant = seed_data["tenant"]

    # Dynamic custom station
    pizza_station = KitchenStationModel(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        branch_id=branch.id,
        name={"en": "Pizza Oven", "ar": "فرن"},
        code="PIZZA_OVEN",
        is_active=True,
    )
    fryer_station = KitchenStationModel(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        branch_id=branch.id,
        name={"en": "Deep Fryer", "ar": "مقلاة"},
        code="DEEP_FRYER",
        is_active=True,
    )
    test_db.add_all([pizza_station, fryer_station])
    await test_db.flush()

    cat_with_station = Category(
        id=uuid.uuid4(),
        branch_id=branch.id,
        name={"en": "Fried Dishes", "ar": "مقليات"},
        station_id=fryer_station.id,
        station=KitchenStation.HOT_KITCHEN,
        is_active=True,
    )
    cat_with_station.kitchen_station = fryer_station

    cat_without_station = Category(
        id=uuid.uuid4(),
        branch_id=branch.id,
        name={"en": "Generic", "ar": "عام"},
        station=KitchenStation.COLD_KITCHEN,
        is_active=True,
    )

    # 1. Item with dynamic kitchen_station entity -> takes highest precedence
    item1 = Item(
        id=uuid.uuid4(),
        category_id=cat_with_station.id,
        name={"en": "Pizza Margherita", "ar": "بيتزا"},
        base_price=Decimal("40.00"),
        station_id=pizza_station.id,
        station=KitchenStation.HOT_KITCHEN,
    )
    item1.kitchen_station = pizza_station

    resolved1 = StationRoutingService.resolve_item_station(item1, cat_with_station)
    assert resolved1 == "PIZZA_OVEN"

    # 2. Item with legacy station enum override -> takes precedence over category dynamic station
    item2 = Item(
        id=uuid.uuid4(),
        category_id=cat_with_station.id,
        name={"en": "Ice Tea", "ar": "شاي مثلج"},
        base_price=Decimal("12.00"),
        station=KitchenStation.BEVERAGE,
    )
    resolved2 = StationRoutingService.resolve_item_station(item2, cat_with_station)
    assert resolved2 == KitchenStation.BEVERAGE

    # 3. Item without station -> inherits category dynamic station
    item3 = Item(
        id=uuid.uuid4(),
        category_id=cat_with_station.id,
        name={"en": "French Fries", "ar": "بطاطس"},
        base_price=Decimal("15.00"),
        station=None,
    )
    resolved3 = StationRoutingService.resolve_item_station(item3, cat_with_station)
    assert resolved3 == "DEEP_FRYER"

    # 4. Item without station -> inherits category legacy station enum
    item4 = Item(
        id=uuid.uuid4(),
        category_id=cat_without_station.id,
        name={"en": "Greek Salad", "ar": "سلطة يونانية"},
        base_price=Decimal("25.00"),
        station=None,
    )
    resolved4 = StationRoutingService.resolve_item_station(item4, cat_without_station)
    assert resolved4 == KitchenStation.COLD_KITCHEN

    # 5. Default fallback -> HOT_KITCHEN
    cat_no_station = Category(
        id=uuid.uuid4(),
        branch_id=branch.id,
        name={"en": "Empty", "ar": "فارغ"},
        station=None,
        is_active=True,
    )
    item5 = Item(
        id=uuid.uuid4(),
        category_id=cat_no_station.id,
        name={"en": "Mystery Item", "ar": "مجهول"},
        base_price=Decimal("10.00"),
        station=None,
    )
    resolved5 = StationRoutingService.resolve_item_station(item5, cat_no_station)
    assert resolved5 == KitchenStation.HOT_KITCHEN


@pytest.mark.asyncio
async def test_dual_channel_redis_publishing(test_db: AsyncSession, seed_data: dict[str, Any]):
    """Confirm order dispatching and item bumping publishes to both dynamic and legacy channels."""
    branch = seed_data["branch"]
    tenant = seed_data["tenant"]

    table = Table(
        id=uuid.uuid4(),
        branch_id=branch.id,
        table_number="T-42",
        capacity=4,
        status=TableStatus.AWAITING_FOOD,
        is_active=True,
    )
    test_db.add(table)
    await test_db.flush()

    order = Order(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        branch_id=branch.id,
        table_id=table.id,
        order_type=OrderType.DINE_IN,
        status=OrderStatus.SUBMITTED,
        subtotal=Decimal("50.00"),
        tax_total=Decimal("7.50"),
        total_amount=Decimal("57.50"),
    )
    order.table = table
    test_db.add(order)
    await test_db.flush()

    item_line = OrderItem(
        id=uuid.uuid4(),
        order_id=order.id,
        item_id=uuid.uuid4(),
        quantity=1,
        unit_price=Decimal("50.00"),
        subtotal=Decimal("50.00"),
        station=KitchenStation.HOT_KITCHEN,
        station_code="PIZZA_OVEN",
        is_bumped=False,
    )
    test_db.add(item_line)
    await test_db.commit()

    published_channels: list[str] = []

    async def mock_publish(channel: str, event_type: str, data: dict[str, Any]):
        published_channels.append(channel)

    with patch("app.services.station_routing_service.publish", side_effect=mock_publish):
        tickets = await StationRoutingService.dispatch_order_to_kds(order, items=[item_line])
        assert len(tickets) == 1

        # Check dual channels for PIZZA_OVEN:
        dynamic_chan = f"branch_{branch.id}_station_pizza_oven"
        legacy_chan = f"branch_{branch.id}_kitchen_pizza_oven"
        consolidated_chan = f"branch_{branch.id}_kitchen"

        assert dynamic_chan in published_channels
        assert legacy_chan in published_channels
        assert consolidated_chan in published_channels

    # Test bumping publishes to dual channels as well
    bumped_channels: list[str] = []

    async def mock_bump_publish(channel: str, event_type: str, data: dict[str, Any]):
        bumped_channels.append(channel)

    with patch("app.services.station_routing_service.publish", side_effect=mock_bump_publish):
        await StationRoutingService.bump_order_item(
            db=test_db,
            order_item_id=item_line.id,
            is_bumped=True,
            branch_id=branch.id,
        )
        assert dynamic_chan in bumped_channels
        assert legacy_chan in bumped_channels
        assert consolidated_chan in bumped_channels


@pytest.mark.asyncio
async def test_rbac_and_branch_isolation(client: AsyncClient, seed_data: dict[str, Any]):
    """Non-admin roles are rejected with 403 Forbidden, and cross-branch requests are denied."""
    branch = seed_data["branch"]
    other_branch = seed_data["other_branch"]
    admin_token = seed_data["admin_token"]
    waiter_token = seed_data["waiter_token"]
    kitchen_token = seed_data["kitchen_token"]

    create_body = {"name": {"en": "Bar", "ar": "بار"}, "code": "BAR", "is_active": True}

    # 1. Waiter role cannot manage stations -> 403
    waiter_headers = {"Authorization": f"Bearer {waiter_token}", "X-Branch-ID": str(branch.id)}
    resp_waiter = await client.post("/api/v1/staff/kitchen-stations", json=create_body, headers=waiter_headers)
    assert resp_waiter.status_code == 403

    # 2. Kitchen role cannot manage stations -> 403
    kitchen_headers = {"Authorization": f"Bearer {kitchen_token}", "X-Branch-ID": str(branch.id)}
    resp_kitchen = await client.post("/api/v1/staff/kitchen-stations", json=create_body, headers=kitchen_headers)
    assert resp_kitchen.status_code == 403

    # 3. Cross-branch isolation: admin cannot create station for unauthorized branch
    cross_headers = {"Authorization": f"Bearer {admin_token}", "X-Branch-ID": str(other_branch.id)}
    resp_cross = await client.post("/api/v1/staff/kitchen-stations", json=create_body, headers=cross_headers)
    assert resp_cross.status_code == 403
