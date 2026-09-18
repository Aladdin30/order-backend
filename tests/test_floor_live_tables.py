"""Automated test suite for Task BE-4.1: Live Floor Table State & Session Engine."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
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

import app.core.database  # Registers SQLite JSONB compilation hook
from app.api.deps import get_async_db
from app.core.security import create_access_token, get_password_hash
from app.main import create_app
from app.models.auth import Base, Branch, Table, Tenant, User, UserBranchAccess
from app.models.enums import (
    OrderStatus,
    OrderType,
    ServiceRequestStatus,
    ServiceRequestType,
    TableStatus,
    UserRole,
)
from app.models.order import Order
from app.models.service import ServiceRequest
from app.models.table import TableSession


@pytest_asyncio.fixture(scope="function")
async def async_test_engine() -> AsyncGenerator[AsyncEngine, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(TableSession.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
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


@pytest_asyncio.fixture(scope="function")
async def seed_floor_data(test_session: AsyncSession) -> dict:
    tenant = Tenant(name="Live Floor Dine", slug="live-floor-dine", is_active=True)
    test_session.add(tenant)
    await test_session.flush()

    branch_1 = Branch(
        tenant_id=tenant.id,
        name={"en": "Downtown", "ar": "وسط المدينة"},
        slug="downtown",
        latitude=Decimal("24.7136"),
        longitude=Decimal("46.6753"),
        geofence_radius_meters=150,
        is_active=True,
    )
    branch_2 = Branch(
        tenant_id=tenant.id,
        name={"en": "Uptown", "ar": "شمال المدينة"},
        slug="uptown",
        latitude=Decimal("24.8000"),
        longitude=Decimal("46.7000"),
        geofence_radius_meters=150,
        is_active=True,
    )
    test_session.add_all([branch_1, branch_2])
    await test_session.flush()

    pw_hash = get_password_hash("Password123!")

    waiter_user = User(
        tenant_id=tenant.id,
        email="waiter@floor.com",
        hashed_password=pw_hash,
        full_name="Walter Waiter",
        role=UserRole.WAITER,
        is_active=True,
    )
    cashier_user = User(
        tenant_id=tenant.id,
        email="cashier@floor.com",
        hashed_password=pw_hash,
        full_name="Carl Cashier",
        role=UserRole.CASHIER,
        is_active=True,
    )
    admin_user = User(
        tenant_id=tenant.id,
        email="admin@floor.com",
        hashed_password=pw_hash,
        full_name="Alice Admin",
        role=UserRole.BRANCH_ADMIN,
        is_active=True,
    )
    super_admin = User(
        tenant_id=tenant.id,
        email="super@floor.com",
        hashed_password=pw_hash,
        full_name="Sam Super",
        role=UserRole.SUPER_ADMIN,
        is_active=True,
    )
    other_branch_waiter = User(
        tenant_id=tenant.id,
        email="other_waiter@floor.com",
        hashed_password=pw_hash,
        full_name="Oscar Other",
        role=UserRole.WAITER,
        is_active=True,
    )
    kitchen_user = User(
        tenant_id=tenant.id,
        email="kitchen@floor.com",
        hashed_password=pw_hash,
        full_name="Kevin Kitchen",
        role=UserRole.KITCHEN_STAFF,
        is_active=True,
    )

    test_session.add_all([
        waiter_user,
        cashier_user,
        admin_user,
        super_admin,
        other_branch_waiter,
        kitchen_user,
    ])
    await test_session.flush()

    # Assign branch access
    test_session.add_all([
        UserBranchAccess(user_id=waiter_user.id, branch_id=branch_1.id),
        UserBranchAccess(user_id=cashier_user.id, branch_id=branch_1.id),
        UserBranchAccess(user_id=admin_user.id, branch_id=branch_1.id),
        UserBranchAccess(user_id=other_branch_waiter.id, branch_id=branch_2.id),
        UserBranchAccess(user_id=kitchen_user.id, branch_id=branch_1.id),
    ])
    await test_session.commit()

    waiter_token = create_access_token({
        "sub": str(waiter_user.id),
        "tenant_id": str(tenant.id),
        "role": UserRole.WAITER.value,
        "email": waiter_user.email,
    })
    admin_token = create_access_token({
        "sub": str(admin_user.id),
        "tenant_id": str(tenant.id),
        "role": UserRole.BRANCH_ADMIN.value,
        "email": admin_user.email,
    })
    super_admin_token = create_access_token({
        "sub": str(super_admin.id),
        "tenant_id": str(tenant.id),
        "role": UserRole.SUPER_ADMIN.value,
        "email": super_admin.email,
    })
    other_waiter_token = create_access_token({
        "sub": str(other_branch_waiter.id),
        "tenant_id": str(tenant.id),
        "role": UserRole.WAITER.value,
        "email": other_branch_waiter.email,
    })
    kitchen_token = create_access_token({
        "sub": str(kitchen_user.id),
        "tenant_id": str(tenant.id),
        "role": UserRole.KITCHEN_STAFF.value,
        "email": kitchen_user.email,
    })

    return {
        "tenant": tenant,
        "branch_1": branch_1,
        "branch_2": branch_2,
        "waiter_headers": {"Authorization": f"Bearer {waiter_token}", "X-Branch-ID": str(branch_1.id)},
        "admin_headers": {"Authorization": f"Bearer {admin_token}", "X-Branch-ID": str(branch_1.id)},
        "super_admin_headers": {"Authorization": f"Bearer {super_admin_token}", "X-Branch-ID": str(branch_1.id)},
        "other_waiter_headers": {"Authorization": f"Bearer {other_waiter_token}", "X-Branch-ID": str(branch_1.id)},
        "kitchen_headers": {"Authorization": f"Bearer {kitchen_token}", "X-Branch-ID": str(branch_1.id)},
    }


@pytest.mark.asyncio
async def test_empty_floor_state(
    test_client: AsyncClient,
    test_session: AsyncSession,
    seed_floor_data: dict,
) -> None:
    """Verify that clean tables return AVAILABLE state with 0 occupancy minutes."""
    branch = seed_floor_data["branch_1"]
    headers = seed_floor_data["waiter_headers"]

    t1 = Table(branch_id=branch.id, table_number="T1", capacity=4, is_active=True)
    t2 = Table(branch_id=branch.id, table_number="T2", capacity=2, is_active=True)
    test_session.add_all([t1, t2])
    await test_session.commit()

    resp = await test_client.get("/api/v1/floor/tables/live", headers=headers)
    assert resp.status_code == 200
    data = resp.json()

    assert data["total_tables"] >= 2
    assert data["available_tables"] >= 2
    assert data["occupied_tables"] == 0
    assert data["tables_awaiting_food"] == 0
    assert data["tables_with_pending_requests"] == 0

    t1_data = next(t for t in data["tables"] if t["table_id"] == str(t1.id))
    assert t1_data["current_state"] == "AVAILABLE"
    assert t1_data["occupancy_duration_minutes"] == 0
    assert t1_data["active_order_id"] is None
    assert t1_data["active_session_id"] is None


@pytest.mark.asyncio
async def test_seated_table_without_order(
    test_client: AsyncClient,
    test_session: AsyncSession,
    seed_floor_data: dict,
) -> None:
    """Verify active TableSession without order reports SEATED state and >=15 occupancy duration."""
    branch = seed_floor_data["branch_1"]
    headers = seed_floor_data["waiter_headers"]

    table1 = Table(branch_id=branch.id, table_number="T10", capacity=4, is_active=True)
    table2 = Table(branch_id=branch.id, table_number="T11", capacity=2, is_active=True)
    test_session.add_all([table1, table2])
    await test_session.commit()

    # Active session created 15 minutes ago on Table 1
    past_time = datetime.now(timezone.utc) - timedelta(minutes=15)
    session = TableSession(
        table_id=table1.id,
        branch_id=branch.id,
        is_active=True,
        created_at=past_time,
    )
    test_session.add(session)
    await test_session.commit()

    resp = await test_client.get("/api/v1/floor/tables/live", headers=headers)
    assert resp.status_code == 200
    data = resp.json()

    assert data["occupied_tables"] == 1
    assert data["available_tables"] == 1

    t1_data = next(t for t in data["tables"] if t["table_id"] == str(table1.id))
    assert t1_data["current_state"] == "SEATED"
    assert t1_data["occupancy_duration_minutes"] >= 15
    assert t1_data["active_session_id"] == str(session.id)
    assert t1_data["active_order_id"] is None


@pytest.mark.asyncio
async def test_order_progression_awaiting_food_to_served(
    test_client: AsyncClient,
    test_session: AsyncSession,
    seed_floor_data: dict,
) -> None:
    """Verify state transitions from AWAITING_FOOD to FOOD_SERVED as order status advances."""
    branch = seed_floor_data["branch_1"]
    headers = seed_floor_data["waiter_headers"]

    table = Table(branch_id=branch.id, table_number="T15", capacity=4, is_active=True)
    test_session.add(table)
    await test_session.commit()

    # 1. Place order -> AWAITING_FOOD
    order = Order(
        tenant_id=seed_floor_data["tenant"].id,
        branch_id=branch.id,
        table_id=table.id,
        order_type=OrderType.DINE_IN,
        status=OrderStatus.PREPARING,
        total_amount=Decimal("150.00"),
    )
    test_session.add(order)
    await test_session.commit()

    res1 = await test_client.get("/api/v1/floor/tables/live", headers=headers)
    assert res1.status_code == 200
    t_data1 = next(t for t in res1.json()["tables"] if t["table_id"] == str(table.id))
    assert t_data1["current_state"] == "AWAITING_FOOD"
    assert t_data1["active_order_id"] == str(order.id)
    assert Decimal(str(t_data1["order_total"])) == Decimal("150.00")
    assert res1.json()["tables_awaiting_food"] == 1

    # 2. Transition order to SERVED -> FOOD_SERVED
    order.status = OrderStatus.SERVED
    await test_session.commit()

    res2 = await test_client.get("/api/v1/floor/tables/live", headers=headers)
    assert res2.status_code == 200
    t_data2 = next(t for t in res2.json()["tables"] if t["table_id"] == str(table.id))
    assert t_data2["current_state"] == "FOOD_SERVED"
    assert res2.json()["tables_awaiting_food"] == 0


@pytest.mark.asyncio
async def test_pending_service_requests_aggregation(
    test_client: AsyncClient,
    test_session: AsyncSession,
    seed_floor_data: dict,
) -> None:
    """Verify pending service requests count aggregates correctly and decrements on resolution."""
    branch = seed_floor_data["branch_1"]
    headers = seed_floor_data["waiter_headers"]

    table = Table(branch_id=branch.id, table_number="T20", capacity=4, is_active=True)
    test_session.add(table)
    await test_session.commit()

    sr1 = ServiceRequest(
        branch_id=branch.id,
        table_id=table.id,
        request_type=ServiceRequestType.WAITER_CALL,
        status=ServiceRequestStatus.PENDING,
    )
    sr2 = ServiceRequest(
        branch_id=branch.id,
        table_id=table.id,
        request_type=ServiceRequestType.WATER_REFILL,
        status=ServiceRequestStatus.PENDING,
    )
    test_session.add_all([sr1, sr2])
    await test_session.commit()

    res1 = await test_client.get("/api/v1/floor/tables/live", headers=headers)
    assert res1.status_code == 200
    assert res1.json()["tables_with_pending_requests"] == 1
    t_data1 = next(t for t in res1.json()["tables"] if t["table_id"] == str(table.id))
    assert t_data1["pending_service_requests_count"] == 2

    # Resolve 1 request
    sr1.status = ServiceRequestStatus.COMPLETED
    await test_session.commit()

    res2 = await test_client.get("/api/v1/floor/tables/live", headers=headers)
    assert res2.status_code == 200
    t_data2 = next(t for t in res2.json()["tables"] if t["table_id"] == str(table.id))
    assert t_data2["pending_service_requests_count"] == 1


@pytest.mark.asyncio
async def test_table_cleared_on_payment_settlement(
    test_client: AsyncClient,
    test_session: AsyncSession,
    seed_floor_data: dict,
) -> None:
    """Verify that when an order is settled (PAID) and session is closed, table resets to AVAILABLE."""
    branch = seed_floor_data["branch_1"]
    headers = seed_floor_data["waiter_headers"]

    table = Table(branch_id=branch.id, table_number="T30", capacity=4, is_active=True)
    test_session.add(table)
    await test_session.commit()

    session = TableSession(
        table_id=table.id,
        branch_id=branch.id,
        is_active=True,
    )
    order = Order(
        tenant_id=seed_floor_data["tenant"].id,
        branch_id=branch.id,
        table_id=table.id,
        order_type=OrderType.DINE_IN,
        status=OrderStatus.SUBMITTED,
        total_amount=Decimal("85.00"),
    )
    test_session.add_all([session, order])
    await test_session.commit()

    # Verify table occupied
    res_before = await test_client.get("/api/v1/floor/tables/live", headers=headers)
    t_before = next(t for t in res_before.json()["tables"] if t["table_id"] == str(table.id))
    assert t_before["current_state"] == "AWAITING_FOOD"

    # Settle order and close session
    order.status = OrderStatus.PAID
    session.is_active = False
    table.status = TableStatus.AVAILABLE
    table.current_session_token = None
    await test_session.commit()

    res_after = await test_client.get("/api/v1/floor/tables/live", headers=headers)
    t_after = next(t for t in res_after.json()["tables"] if t["table_id"] == str(table.id))
    assert t_after["current_state"] == "AVAILABLE"
    assert t_after["occupancy_duration_minutes"] == 0


@pytest.mark.asyncio
async def test_multi_tenant_branch_isolation(
    test_client: AsyncClient,
    test_session: AsyncSession,
    seed_floor_data: dict,
) -> None:
    """Verify that querying Branch A returns ONLY Branch A tables without leaking Branch B tables."""
    branch_1 = seed_floor_data["branch_1"]
    branch_2 = seed_floor_data["branch_2"]
    headers_branch_1 = seed_floor_data["waiter_headers"]

    table_a = Table(branch_id=branch_1.id, table_number="A-101", capacity=4, is_active=True)
    table_b = Table(branch_id=branch_2.id, table_number="B-999", capacity=6, is_active=True)
    test_session.add_all([table_a, table_b])
    await test_session.commit()

    resp = await test_client.get("/api/v1/floor/tables/live", headers=headers_branch_1)
    assert resp.status_code == 200
    data = resp.json()

    table_ids = [t["table_id"] for t in data["tables"]]
    assert str(table_a.id) in table_ids
    assert str(table_b.id) not in table_ids


@pytest.mark.asyncio
async def test_rbac_and_branch_access_control(
    test_client: AsyncClient,
    seed_floor_data: dict,
) -> None:
    """Verify 401 unauthenticated, 403 unauthorized branch/role, and 200 for assigned staff and Super Admin."""
    branch = seed_floor_data["branch_1"]

    # 1. Unauthenticated request -> 401
    unauth_resp = await test_client.get(
        "/api/v1/floor/tables/live",
        headers={"X-Branch-ID": str(branch.id)},
    )
    assert unauth_resp.status_code == 401

    # 2. Authenticated user assigned to Branch 2 accessing Branch 1 -> 403
    other_waiter_headers = seed_floor_data["other_waiter_headers"]
    cross_resp = await test_client.get(
        "/api/v1/floor/tables/live",
        headers=other_waiter_headers,
    )
    assert cross_resp.status_code == 403

    # 3. Unauthorized staff role (Kitchen staff) accessing floor -> 403
    kitchen_headers = seed_floor_data["kitchen_headers"]
    kitchen_resp = await test_client.get(
        "/api/v1/floor/tables/live",
        headers=kitchen_headers,
    )
    assert kitchen_resp.status_code == 403

    # 4. Super Admin accessing Branch 1 -> 200
    super_headers = seed_floor_data["super_admin_headers"]
    super_resp = await test_client.get(
        "/api/v1/floor/tables/live",
        headers=super_headers,
    )
    assert super_resp.status_code == 200
