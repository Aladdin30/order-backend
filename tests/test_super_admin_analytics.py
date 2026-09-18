"""Automated test suite for Task BE-4.3: Super Admin Analytics & Menu Performance Engine."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import AsyncGenerator
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

import app.core.database  # Registers SQLite JSONB compilation hook
from app.api.deps import get_async_db, get_db
from app.core.security import create_access_token, get_password_hash
from app.main import create_app
from app.models.auth import Base, Branch, Tenant, User, UserBranchAccess
from app.models.catalog import Category, Item
from app.models.enums import (
    KitchenStation,
    OrderSource,
    OrderStatus,
    OrderType,
    PaymentMethod,
    PaymentStatus,
    UserRole,
)
from app.models.financials import FinancialBase
from app.models.order import Order, OrderItem, Payment
from app.models.table import TableSession


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
    session_factory = async_sessionmaker(bind=async_test_engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session


@pytest_asyncio.fixture(scope="function")
async def test_client(
    async_test_engine: AsyncEngine,
    test_session: AsyncSession,
) -> AsyncGenerator[AsyncClient, None]:
    app = create_app()
    session_factory = async_sessionmaker(bind=async_test_engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_async_db() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_async_db] = override_get_async_db
    app.dependency_overrides[get_db] = override_get_async_db

    transport = ASGITransport(app=app)
    with patch("app.core.database.async_session_factory", session_factory), \
         patch("app.api.deps.async_session_factory", session_factory), \
         patch("app.services.analytics_service.redis_pubsub.get_redis_client", AsyncMock(return_value=None)):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client

    app.dependency_overrides.clear()


@pytest_asyncio.fixture(scope="function")
async def seed_analytics_data(test_session: AsyncSession) -> dict:
    """Seed tenant, two branches, users with distinct roles, categories, items, orders, and payments."""
    now = datetime.now(timezone.utc)

    # 1. Tenant & Branches
    tenant = Tenant(
        id=uuid.uuid4(),
        name="Artisan Group",
        slug="artisan-grp",
        is_active=True,
    )
    test_session.add(tenant)
    await test_session.flush()

    branch_1 = Branch(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        name={"en": "Downtown Branch", "ar": "فرع وسط المدينة"},
        slug="downtown",
        latitude=Decimal("24.7136"),
        longitude=Decimal("46.6753"),
        geofence_radius_meters=300,
        is_active=True,
    )
    branch_2 = Branch(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        name={"en": "Uptown Branch", "ar": "فرع شمال المدينة"},
        slug="uptown",
        latitude=Decimal("24.7200"),
        longitude=Decimal("46.6800"),
        geofence_radius_meters=300,
        is_active=True,
    )
    test_session.add_all([branch_1, branch_2])
    await test_session.flush()

    # 2. Users (Super Admin, Regional Manager, Branch Admin, Cashier, Waiter)
    pw = get_password_hash("SecretPassword123!")

    super_admin = User(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        email="super@artisan.com",
        hashed_password=pw,
        full_name="Enterprise Super Admin",
        role=UserRole.SUPER_ADMIN,
        is_active=True,
    )
    reg_manager = User(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        email="regional@artisan.com",
        hashed_password=pw,
        full_name="Regional Manager Central",
        role=UserRole.REGIONAL_MANAGER,
        is_active=True,
    )
    branch_admin = User(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        email="admin@downtown.com",
        hashed_password=pw,
        full_name="Downtown Admin",
        role=UserRole.BRANCH_ADMIN,
        is_active=True,
    )
    cashier = User(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        email="cashier@downtown.com",
        hashed_password=pw,
        full_name="Downtown Cashier",
        role=UserRole.CASHIER,
        is_active=True,
    )
    waiter = User(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        email="waiter@downtown.com",
        hashed_password=pw,
        full_name="Downtown Waiter",
        role=UserRole.WAITER,
        is_active=True,
    )
    test_session.add_all([super_admin, reg_manager, branch_admin, cashier, waiter])
    await test_session.flush()

    # Regional Manager only assigned to Branch 1
    acc_rm1 = UserBranchAccess(user_id=reg_manager.id, branch_id=branch_1.id)
    acc_ba1 = UserBranchAccess(user_id=branch_admin.id, branch_id=branch_1.id)
    acc_c1 = UserBranchAccess(user_id=cashier.id, branch_id=branch_1.id)
    acc_w1 = UserBranchAccess(user_id=waiter.id, branch_id=branch_1.id)
    test_session.add_all([acc_rm1, acc_ba1, acc_c1, acc_w1])
    await test_session.flush()

    # 3. Categories & Items
    cat_mains = Category(
        id=uuid.uuid4(),
        branch_id=branch_1.id,
        name={"en": "Main Courses", "ar": "أطباق رئيسية"},
        display_order=1,
        station=KitchenStation.HOT_KITCHEN,
        is_active=True,
    )
    cat_beverages = Category(
        id=uuid.uuid4(),
        branch_id=branch_1.id,
        name={"en": "Beverages", "ar": "مشروبات"},
        display_order=2,
        station=KitchenStation.BEVERAGE,
        is_active=True,
    )
    test_session.add_all([cat_mains, cat_beverages])
    await test_session.flush()

    item_a = Item(
        id=uuid.uuid4(),
        category_id=cat_mains.id,
        name={"en": "Truffle Burger", "ar": "برجر ترافل"},
        base_price=Decimal("50.00"),
        is_available=True,
    )
    item_b = Item(
        id=uuid.uuid4(),
        category_id=cat_mains.id,
        name={"en": "Loaded Fries", "ar": "بطاطس محملة"},
        base_price=Decimal("20.00"),
        is_available=True,
    )
    item_c = Item(
        id=uuid.uuid4(),
        category_id=cat_beverages.id,
        name={"en": "Sparkling Mint", "ar": "نعناع فوّار"},
        base_price=Decimal("10.00"),
        is_available=True,
    )
    test_session.add_all([item_a, item_b, item_c])
    await test_session.flush()

    # 4. Orders
    # Branch 1 - Order 1: $100.00 paid (Item A x2)
    b1_order1 = Order(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        branch_id=branch_1.id,
        order_type=OrderType.DINE_IN,
        order_source=OrderSource.QR_CUSTOMER,
        status=OrderStatus.PAID,
        is_paid=True,
        subtotal=Decimal("100.00"),
        tax_total=Decimal("0.00"),
        service_fee_total=Decimal("0.00"),
        total_amount=Decimal("100.00"),
        created_at=now,
    )
    oi_1 = OrderItem(
        order_id=b1_order1.id,
        item_id=item_a.id,
        quantity=2,
        unit_price=Decimal("50.00"),
        subtotal=Decimal("100.00"),
    )

    # Branch 1 - Order 2: $200.00 paid (Item A x2 ($100) + Item B x5 ($100))
    b1_order2 = Order(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        branch_id=branch_1.id,
        order_type=OrderType.TAKEAWAY,
        order_source=OrderSource.CASHIER_POS,
        status=OrderStatus.PAID,
        is_paid=True,
        subtotal=Decimal("200.00"),
        tax_total=Decimal("0.00"),
        service_fee_total=Decimal("0.00"),
        total_amount=Decimal("200.00"),
        created_at=now,
    )
    oi_2a = OrderItem(
        order_id=b1_order2.id,
        item_id=item_a.id,
        quantity=2,
        unit_price=Decimal("50.00"),
        subtotal=Decimal("100.00"),
    )
    oi_2b = OrderItem(
        order_id=b1_order2.id,
        item_id=item_b.id,
        quantity=5,
        unit_price=Decimal("20.00"),
        subtotal=Decimal("100.00"),
    )

    # Branch 2 - Order 3: $300.00 paid (Item A x6 = $300)
    b2_order3 = Order(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        branch_id=branch_2.id,
        order_type=OrderType.DINE_IN,
        order_source=OrderSource.QR_CUSTOMER,
        status=OrderStatus.PAID,
        is_paid=True,
        subtotal=Decimal("300.00"),
        tax_total=Decimal("0.00"),
        service_fee_total=Decimal("0.00"),
        total_amount=Decimal("300.00"),
        created_at=now,
    )
    oi_3 = OrderItem(
        order_id=b2_order3.id,
        item_id=item_a.id,
        quantity=6,
        unit_price=Decimal("50.00"),
        subtotal=Decimal("300.00"),
    )

    test_session.add_all([b1_order1, b1_order2, b2_order3])
    test_session.add_all([oi_1, oi_2a, oi_2b, oi_3])
    await test_session.flush()

    # 5. Payments
    p1 = Payment(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        branch_id=branch_1.id,
        order_id=b1_order1.id,
        payment_method=PaymentMethod.CARD_TERMINAL,
        amount=Decimal("100.00"),
        status=PaymentStatus.COMPLETED,
        created_at=now,
    )
    p2 = Payment(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        branch_id=branch_1.id,
        order_id=b1_order2.id,
        payment_method=PaymentMethod.CASH,
        amount=Decimal("200.00"),
        status=PaymentStatus.COMPLETED,
        created_at=now,
    )
    p3 = Payment(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        branch_id=branch_2.id,
        order_id=b2_order3.id,
        payment_method=PaymentMethod.ONLINE_CARD,
        amount=Decimal("300.00"),
        status=PaymentStatus.COMPLETED,
        created_at=now,
    )
    test_session.add_all([p1, p2, p3])
    await test_session.commit()

    # 6. Tokens
    super_token = create_access_token({"sub": str(super_admin.id), "tenant_id": str(tenant.id), "role": super_admin.role.value})
    reg_token = create_access_token({"sub": str(reg_manager.id), "tenant_id": str(tenant.id), "role": reg_manager.role.value})
    admin_token = create_access_token({"sub": str(branch_admin.id), "tenant_id": str(tenant.id), "role": branch_admin.role.value})
    cashier_token = create_access_token({"sub": str(cashier.id), "tenant_id": str(tenant.id), "role": cashier.role.value})
    waiter_token = create_access_token({"sub": str(waiter.id), "tenant_id": str(tenant.id), "role": waiter.role.value})

    return {
        "tenant": tenant,
        "branch_1": branch_1,
        "branch_2": branch_2,
        "super_admin": super_admin,
        "reg_manager": reg_manager,
        "branch_admin": branch_admin,
        "cashier": cashier,
        "waiter": waiter,
        "cat_mains": cat_mains,
        "cat_beverages": cat_beverages,
        "item_a": item_a,
        "item_b": item_b,
        "item_c": item_c,
        "super_token": super_token,
        "reg_token": reg_token,
        "admin_token": admin_token,
        "cashier_token": cashier_token,
        "waiter_token": waiter_token,
        "b1_order1": b1_order1,
        "b1_order2": b1_order2,
        "b2_order3": b2_order3,
    }


# ---------------------------------------------------------------------------
# Test 1: Multi-Branch GMV & AOV Validation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_multi_branch_gmv_and_aov(
    test_client: AsyncClient,
    seed_analytics_data: dict,
) -> None:
    """Validate enterprise-wide single-pass GMV and AOV computation across branches."""
    # Branch 1: 2 paid orders ($100, $200). Branch 2: 1 paid order ($300).
    # Expected: $100 + $200 + $300 = $600 GMV, 3 orders, AOV = $200.00.
    headers = {"Authorization": f"Bearer {seed_analytics_data['super_token']}"}
    resp = await test_client.get("/api/v1/analytics/dashboard", headers=headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()

    kpis = data["kpis"]
    assert kpis["gmv"] == "600.00"
    assert kpis["paid_orders"] == 3
    assert kpis["aov"] == "200.00"
    assert kpis["total_orders"] == 3


# ---------------------------------------------------------------------------
# Test 2: Top-Selling vs Bottom-Selling Items Accuracy
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_top_vs_bottom_selling_items(
    test_client: AsyncClient,
    seed_analytics_data: dict,
) -> None:
    """Verify Menu Engineering pipeline accurately ranks top vs dead-stock items and calculates revenue."""
    headers = {"Authorization": f"Bearer {seed_analytics_data['super_token']}"}
    resp = await test_client.get("/api/v1/analytics/menu-performance", headers=headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()

    top_items = data["top_selling_items"]
    bottom_items = data["bottom_selling_items"]
    category_breakdown = data["category_breakdown"]

    item_a_id = str(seed_analytics_data["item_a"].id)
    item_b_id = str(seed_analytics_data["item_b"].id)
    item_c_id = str(seed_analytics_data["item_c"].id)

    # Item A has 10 units sold (2 + 2 + 6) -> top seller
    assert len(top_items) >= 1
    assert top_items[0]["item_id"] == item_a_id
    assert top_items[0]["total_quantity_sold"] == 10
    assert top_items[0]["gross_revenue"] == "500.00"  # 10 * 50.00

    # Item B has 5 units sold -> second
    item_b_metric = next((it for it in top_items if it["item_id"] == item_b_id), None)
    assert item_b_metric is not None
    assert item_b_metric["total_quantity_sold"] == 5
    assert item_b_metric["gross_revenue"] == "100.00"  # 5 * 20.00

    # Item C has 0 units sold -> surfaces in bottom_selling_items (dead stock)
    item_c_metric = next((it for it in bottom_items if it["item_id"] == item_c_id), None)
    assert item_c_metric is not None
    assert item_c_metric["total_quantity_sold"] == 0
    assert item_c_metric["gross_revenue"] == "0.00"

    # Category breakdown verification
    assert len(category_breakdown) >= 1
    mains_cat = next((c for c in category_breakdown if c["category_id"] == str(seed_analytics_data["cat_mains"].id)), None)
    assert mains_cat is not None
    assert mains_cat["total_items_sold"] == 15  # 10 + 5
    assert mains_cat["total_revenue"] == "600.00"  # 500 + 100


# ---------------------------------------------------------------------------
# Test 3: Branch-Level Scoping
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_branch_level_scoping(
    test_client: AsyncClient,
    seed_analytics_data: dict,
) -> None:
    """Verify filtering by branch_id limits GMV, order counts, and menu items strictly to target branch."""
    branch_1_id = seed_analytics_data["branch_1"].id
    headers = {"Authorization": f"Bearer {seed_analytics_data['super_token']}"}

    resp = await test_client.get(f"/api/v1/analytics/dashboard?branch_id={branch_1_id}", headers=headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()

    # GMV must strictly equal Branch 1's sales ($100 + $200 = $300)
    assert data["kpis"]["gmv"] == "300.00"
    assert data["kpis"]["paid_orders"] == 2
    assert data["kpis"]["aov"] == "150.00"  # 300 / 2

    # When branch_id is supplied, branch_rankings should be empty
    assert data["branch_rankings"] == []


# ---------------------------------------------------------------------------
# Test 4: Cancellations & Refunds Isolation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cancellations_and_refunds_isolation(
    test_client: AsyncClient,
    test_session: AsyncSession,
    seed_analytics_data: dict,
) -> None:
    """Verify cancelled orders do not inflate GMV and refunds are accurately isolated."""
    branch_1 = seed_analytics_data["branch_1"]
    tenant = seed_analytics_data["tenant"]
    now = datetime.now(timezone.utc)

    # 1. Add a Cancelled Order of $150.00
    cancelled_order = Order(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        branch_id=branch_1.id,
        order_type=OrderType.DINE_IN,
        order_source=OrderSource.QR_CUSTOMER,
        status=OrderStatus.CANCELLED,
        is_paid=False,
        subtotal=Decimal("150.00"),
        total_amount=Decimal("150.00"),
        cancellation_reason="Customer walked out",
        created_at=now,
    )
    test_session.add(cancelled_order)

    # 2. Add a Refunded Payment of $60.00
    refunded_payment = Payment(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        branch_id=branch_1.id,
        order_id=seed_analytics_data["b1_order1"].id,
        payment_method=PaymentMethod.CARD_TERMINAL,
        amount=Decimal("60.00"),
        status=PaymentStatus.REFUNDED,
        created_at=now,
    )
    test_session.add(refunded_payment)
    await test_session.commit()

    headers = {"Authorization": f"Bearer {seed_analytics_data['super_token']}"}
    resp = await test_client.get(
        f"/api/v1/analytics/dashboard?branch_id={branch_1.id}&force_refresh=true",
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()

    # Cancelled orders must not inflate GMV (remains 300.00 for Branch 1)
    assert data["kpis"]["gmv"] == "300.00"
    assert data["kpis"]["cancelled_orders"] == 1
    assert data["kpis"]["total_refunds"] == "60.00"


# ---------------------------------------------------------------------------
# Test 5: Cross-Branch Matrix Order
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cross_branch_matrix_order(
    test_client: AsyncClient,
    test_session: AsyncSession,
    seed_analytics_data: dict,
) -> None:
    """Verify cross-branch ranking matrix sorts branches strictly descending by GMV."""
    # Add an additional order to Branch 2 ($150) so Branch 2 GMV = $450, Branch 1 GMV = $300
    tenant = seed_analytics_data["tenant"]
    branch_2 = seed_analytics_data["branch_2"]
    now = datetime.now(timezone.utc)

    extra_order = Order(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        branch_id=branch_2.id,
        order_type=OrderType.DINE_IN,
        order_source=OrderSource.QR_CUSTOMER,
        status=OrderStatus.PAID,
        is_paid=True,
        subtotal=Decimal("150.00"),
        total_amount=Decimal("150.00"),
        created_at=now,
    )
    extra_payment = Payment(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        branch_id=branch_2.id,
        order_id=extra_order.id,
        payment_method=PaymentMethod.ONLINE_CARD,
        amount=Decimal("150.00"),
        status=PaymentStatus.COMPLETED,
        created_at=now,
    )
    test_session.add_all([extra_order, extra_payment])
    await test_session.commit()

    headers = {"Authorization": f"Bearer {seed_analytics_data['super_token']}"}
    resp = await test_client.get("/api/v1/analytics/branches-matrix", headers=headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()

    branches = data["branches"]
    assert len(branches) >= 2

    # Branch 2 should be ranked 1st ($450.00), Branch 1 should be ranked 2nd ($300.00)
    assert branches[0]["branch_id"] == str(branch_2.id)
    assert branches[0]["gmv"] == "450.00"
    assert branches[1]["branch_id"] == str(seed_analytics_data["branch_1"].id)
    assert branches[1]["gmv"] == "300.00"

    # Verify payment breakdown split for Branch 1:
    # p1 is CARD_TERMINAL ($100), p2 is CASH ($200)
    b1_metric = next(b for b in branches if b["branch_id"] == str(seed_analytics_data["branch_1"].id))
    assert b1_metric["cash_revenue"] == "200.00"
    assert b1_metric["digital_revenue"] == "100.00"


# ---------------------------------------------------------------------------
# Test 6: RBAC Guardrails
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rbac_guardrails(
    test_client: AsyncClient,
    seed_analytics_data: dict,
) -> None:
    """Enforce strict RBAC: reject branch staff with 403, unauthenticated with 401, allow managers."""
    # 1. Unauthenticated -> 401
    res_unauth = await test_client.get("/api/v1/analytics/dashboard")
    assert res_unauth.status_code == 401

    # 2. Branch Admin -> 403 Forbidden
    res_admin = await test_client.get(
        "/api/v1/analytics/dashboard",
        headers={"Authorization": f"Bearer {seed_analytics_data['admin_token']}"},
    )
    assert res_admin.status_code == 403

    # 3. Cashier -> 403 Forbidden
    res_cashier = await test_client.get(
        "/api/v1/analytics/dashboard",
        headers={"Authorization": f"Bearer {seed_analytics_data['cashier_token']}"},
    )
    assert res_cashier.status_code == 403

    # 4. Waiter -> 403 Forbidden
    res_waiter = await test_client.get(
        "/api/v1/analytics/dashboard",
        headers={"Authorization": f"Bearer {seed_analytics_data['waiter_token']}"},
    )
    assert res_waiter.status_code == 403

    # 5. Super Admin -> 200 OK
    res_super = await test_client.get(
        "/api/v1/analytics/dashboard",
        headers={"Authorization": f"Bearer {seed_analytics_data['super_token']}"},
    )
    assert res_super.status_code == 200

    # 6. Regional Manager querying assigned Branch 1 -> 200 OK
    b1_id = seed_analytics_data["branch_1"].id
    res_rm_ok = await test_client.get(
        f"/api/v1/analytics/dashboard?branch_id={b1_id}",
        headers={"Authorization": f"Bearer {seed_analytics_data['reg_token']}"},
    )
    assert res_rm_ok.status_code == 200

    # 7. Regional Manager querying unassigned Branch 2 -> 403 Forbidden
    b2_id = seed_analytics_data["branch_2"].id
    res_rm_forbidden = await test_client.get(
        f"/api/v1/analytics/dashboard?branch_id={b2_id}",
        headers={"Authorization": f"Bearer {seed_analytics_data['reg_token']}"},
    )
    assert res_rm_forbidden.status_code == 403


# ---------------------------------------------------------------------------
# Test 7: Redis Caching & Force Refresh
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_redis_caching_and_force_refresh(
    test_client: AsyncClient,
    seed_analytics_data: dict,
) -> None:
    """Verify Redis caching layer returns cached response and force_refresh bypasses cache."""
    headers = {"Authorization": f"Bearer {seed_analytics_data['super_token']}"}

    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=None)
    mock_redis.set = AsyncMock(return_value=True)

    with patch("app.services.analytics_service.redis_pubsub.get_redis_client", return_value=mock_redis):
        # 1. First call: Cache miss -> calls redis.set
        resp1 = await test_client.get("/api/v1/analytics/dashboard?period=today", headers=headers)
        assert resp1.status_code == 200
        assert mock_redis.set.called

        # 2. Simulate cached payload in Redis
        cached_payload = resp1.json()
        cached_payload["kpis"]["gmv"] = "9999.00"  # Distinct marker
        mock_redis.get = AsyncMock(return_value=json.dumps(cached_payload))

        resp2 = await test_client.get("/api/v1/analytics/dashboard?period=today", headers=headers)
        assert resp2.status_code == 200
        assert resp2.json()["kpis"]["gmv"] == "9999.00"

        # 3. Force refresh: Bypasses cache lookup and executes fresh aggregation
        resp3 = await test_client.get("/api/v1/analytics/dashboard?period=today&force_refresh=true", headers=headers)
        assert resp3.status_code == 200
        assert resp3.json()["kpis"]["gmv"] != "9999.00"


# ---------------------------------------------------------------------------
# Test 8: Time Horizon Resolution & Custom Dates
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_time_horizon_periods(
    test_client: AsyncClient,
    seed_analytics_data: dict,
) -> None:
    """Verify analytics response for different time horizons and custom date windows."""
    headers = {"Authorization": f"Bearer {seed_analytics_data['super_token']}"}

    for period in ["today", "yesterday", "last_7_days", "last_30_days"]:
        resp = await test_client.get(f"/api/v1/analytics/dashboard?period={period}", headers=headers)
        assert resp.status_code == 200, f"Failed for period: {period}"
        data = resp.json()
        assert "period_start" in data
        assert "period_end" in data

    # Custom period with start_date and end_date
    start = datetime.now(timezone.utc) - timedelta(days=2)
    end = datetime.now(timezone.utc) + timedelta(days=1)
    resp_custom = await test_client.get(
        "/api/v1/analytics/dashboard",
        params={
            "period": "custom",
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
        },
        headers=headers,
    )
    assert resp_custom.status_code == 200, resp_custom.text
    assert resp_custom.json()["kpis"]["paid_orders"] >= 1
