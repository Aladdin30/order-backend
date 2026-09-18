"""Comprehensive automated test suite for POS Cashier ordering module, takeaway sequencing, and cancellation."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import AsyncGenerator
from unittest.mock import AsyncMock, patch

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

import app.core.database  # Registers SQLite JSONB compiler extension
from app.api.deps import get_async_db
from app.core.security import create_access_token, get_password_hash
from app.main import create_app
from app.models.auth import Base, Branch, Table, Tenant, User, UserBranchAccess
from app.models.catalog import Category, Item, ModifierGroup, ModifierOption
from app.models.enums import (
    KitchenStation,
    OrderSource,
    OrderStatus,
    OrderType,
    PaymentMethod,
    PaymentStatus,
    TableStatus,
    UserRole,
)
from app.models.order import Order, Payment


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
async def seed_pos_data(test_session: AsyncSession) -> dict:
    tenant = Tenant(name="Fast Feast", slug="fast-feast", is_active=True)
    test_session.add(tenant)
    await test_session.flush()

    branch = Branch(
        tenant_id=tenant.id,
        name={"en": "Al Malqa", "ar": "الملقا"},
        slug="al-malqa",
        latitude=Decimal("24.7136"),
        longitude=Decimal("46.6753"),
        geofence_radius_meters=150,
        tax_rate=Decimal("0.1500"),
        service_fee_rate=Decimal("0.1000"),
        is_service_taxable=False,
        service_fee_dine_in_only=True,
        is_active=True,
    )
    test_session.add(branch)
    await test_session.flush()

    table1 = Table(
        branch_id=branch.id,
        table_number="T-01",
        capacity=4,
        status=TableStatus.AVAILABLE,
        is_active=True,
    )
    test_session.add(table1)
    await test_session.flush()

    # Category and Item
    cat_burgers = Category(
        branch_id=branch.id,
        name={"en": "Burgers", "ar": "برجر"},
        display_order=0,
        station=KitchenStation.HOT_KITCHEN,
        is_active=True,
    )
    test_session.add(cat_burgers)
    await test_session.flush()

    item_classic = Item(
        category_id=cat_burgers.id,
        name={"en": "Classic Burger", "ar": "برجر كلاسيك"},
        base_price=Decimal("40.00"),
        is_available=True,
        station=KitchenStation.HOT_KITCHEN,
    )
    test_session.add(item_classic)
    await test_session.flush()

    # Staff users
    pw_hash = get_password_hash("Password123!")
    cashier = User(
        tenant_id=tenant.id,
        email="cashier1@fastfeast.com",
        hashed_password=pw_hash,
        full_name="Cashier Sarah",
        role=UserRole.CASHIER,
        is_active=True,
    )
    waiter = User(
        tenant_id=tenant.id,
        email="waiter1@fastfeast.com",
        hashed_password=pw_hash,
        full_name="Waiter Ali",
        role=UserRole.WAITER,
        is_active=True,
    )
    test_session.add_all([cashier, waiter])
    await test_session.flush()

    test_session.add_all([
        UserBranchAccess(user_id=cashier.id, branch_id=branch.id),
        UserBranchAccess(user_id=waiter.id, branch_id=branch.id),
    ])
    await test_session.commit()

    cashier_token = create_access_token({
        "sub": str(cashier.id),
        "tenant_id": str(tenant.id),
        "role": UserRole.CASHIER.value,
        "email": cashier.email,
    })

    return {
        "tenant": tenant,
        "branch": branch,
        "table": table1,
        "item": item_classic,
        "cashier": cashier,
        "cashier_token": cashier_token,
    }


@pytest.mark.asyncio
async def test_pos_dine_in_checkout_and_table_status_transition(
    test_client: AsyncClient,
    test_session: AsyncSession,
    seed_pos_data: dict,
) -> None:
    """Verify cashier creates dine-in order, assigns table, updates table to AWAITING_FOOD, and calculates dynamic financials."""
    branch = seed_pos_data["branch"]
    table = seed_pos_data["table"]
    item = seed_pos_data["item"]
    token = seed_pos_data["cashier_token"]
    headers = {"Authorization": f"Bearer {token}", "X-Branch-ID": str(branch.id)}

    payload = {
        "order_type": "DINE_IN",
        "table_id": str(table.id),
        "items": [
            {
                "item_id": str(item.id),
                "quantity": 2,
                "selected_option_ids": [],
            }
        ],
        "customer_notes": "Well done please",
    }

    resp = await test_client.post("/api/v1/pos/orders/checkout", json=payload, headers=headers)
    assert resp.status_code == 201
    data = resp.json()

    assert data["order_type"] == "DINE_IN"
    assert data["order_source"] == "CASHIER_POS"
    assert data["table_id"] == str(table.id)
    assert data["status"] == "SUBMITTED"
    assert data["pickup_number"] is None

    # Subtotal: 2 * 40 = 80.00
    # Service fee (10% dine-in): 8.00
    # Tax (15% simple): 80 * 0.15 = 12.00
    # Total: 80 + 8 + 12 = 100.00
    assert data["subtotal"] == "80.00"
    assert data["service_fee_total"] == "8.00"
    assert data["tax_total"] == "12.00"
    assert data["total_amount"] == "100.00"

    # Verify physical table status updated to AWAITING_FOOD
    await test_session.refresh(table)
    assert table.status == TableStatus.AWAITING_FOOD


@pytest.mark.asyncio
async def test_pos_dine_in_shared_bill_reorder_append(
    test_client: AsyncClient,
    test_session: AsyncSession,
    seed_pos_data: dict,
) -> None:
    """Verify cashier placing a second order on an active table appends items to the same bill."""
    branch = seed_pos_data["branch"]
    table = seed_pos_data["table"]
    item = seed_pos_data["item"]
    token = seed_pos_data["cashier_token"]
    headers = {"Authorization": f"Bearer {token}", "X-Branch-ID": str(branch.id)}

    payload1 = {
        "order_type": "DINE_IN",
        "table_id": str(table.id),
        "items": [{"item_id": str(item.id), "quantity": 1}],
    }
    r1 = await test_client.post("/api/v1/pos/orders/checkout", json=payload1, headers=headers)
    assert r1.status_code == 201
    order1_id = r1.json()["id"]

    # Place second order on the same table
    payload2 = {
        "order_type": "DINE_IN",
        "table_id": str(table.id),
        "items": [{"item_id": str(item.id), "quantity": 1}],
    }
    r2 = await test_client.post("/api/v1/pos/orders/checkout", json=payload2, headers=headers)
    assert r2.status_code == 201
    order2_id = r2.json()["id"]

    # Must append to the same Order ID
    assert order1_id == order2_id
    data2 = r2.json()
    assert len(data2["items"]) == 2
    assert data2["subtotal"] == "80.00"
    assert data2["total_amount"] == "100.00"


@pytest.mark.asyncio
async def test_pos_takeaway_checkout_pickup_sequence_and_payment(
    test_client: AsyncClient,
    test_session: AsyncSession,
    seed_pos_data: dict,
) -> None:
    """Verify takeaway order gets sequential pickup number between 100 and 1000, immediate payment, and no table."""
    branch = seed_pos_data["branch"]
    item = seed_pos_data["item"]
    token = seed_pos_data["cashier_token"]
    headers = {"Authorization": f"Bearer {token}", "X-Branch-ID": str(branch.id)}

    payload = {
        "order_type": "TAKEAWAY",
        "table_id": None,
        "immediate_payment": "CASH",
        "items": [{"item_id": str(item.id), "quantity": 1}],
    }

    resp = await test_client.post("/api/v1/pos/orders/checkout", json=payload, headers=headers)
    assert resp.status_code == 201
    data = resp.json()

    assert data["order_type"] == "TAKEAWAY"
    assert data["order_source"] == "CASHIER_POS"
    assert data["table_id"] is None
    assert data["is_paid"] is True
    assert 100 <= data["pickup_number"] <= 1000

    # Takeaway: Service fee is 0.00 (exempt)
    # Subtotal: 40.00, Tax: 40 * 0.15 = 6.00, Total = 46.00
    assert data["service_fee_total"] == "0.00"
    assert data["tax_total"] == "6.00"
    assert data["total_amount"] == "46.00"

    # Verify Payment row created with status COMPLETED
    order_id = uuid.UUID(data["id"])
    payment_stmt = select(Payment).where(Payment.order_id == order_id)
    payment_res = await test_session.execute(payment_stmt)
    payment = payment_res.scalar_one_or_none()
    assert payment is not None
    assert payment.status == PaymentStatus.COMPLETED
    assert payment.payment_method == PaymentMethod.CASH
    assert payment.amount == Decimal("46.00")


@pytest.mark.asyncio
async def test_pos_checkout_validation_guards(
    test_client: AsyncClient,
    seed_pos_data: dict,
) -> None:
    """Verify dine-in without table_id and takeaway without payment are rejected."""
    branch = seed_pos_data["branch"]
    item = seed_pos_data["item"]
    token = seed_pos_data["cashier_token"]
    headers = {"Authorization": f"Bearer {token}", "X-Branch-ID": str(branch.id)}

    # 1. Dine-in without table_id -> 400
    r1 = await test_client.post(
        "/api/v1/pos/orders/checkout",
        json={"order_type": "DINE_IN", "items": [{"item_id": str(item.id), "quantity": 1}]},
        headers=headers,
    )
    assert r1.status_code == 400
    assert r1.json()["detail"] == "TABLE_ID_REQUIRED_FOR_DINE_IN"

    # 2. Takeaway without immediate_payment -> 400
    r2 = await test_client.post(
        "/api/v1/pos/orders/checkout",
        json={"order_type": "TAKEAWAY", "items": [{"item_id": str(item.id), "quantity": 1}]},
        headers=headers,
    )
    assert r2.status_code == 400
    assert r2.json()["detail"] == "PAYMENT_REQUIRED_FOR_TAKEAWAY"


@pytest.mark.asyncio
async def test_pos_order_cancellation_refund_and_table_release(
    test_client: AsyncClient,
    test_session: AsyncSession,
    seed_pos_data: dict,
) -> None:
    """Verify cashier cancelling an order sets CANCELLED, saves reason, refunds payment, and frees table."""
    branch = seed_pos_data["branch"]
    table = seed_pos_data["table"]
    item = seed_pos_data["item"]
    token = seed_pos_data["cashier_token"]
    headers = {"Authorization": f"Bearer {token}", "X-Branch-ID": str(branch.id)}

    # 1. Create a Dine-In order with immediate payment
    create_resp = await test_client.post(
        "/api/v1/pos/orders/checkout",
        json={
            "order_type": "DINE_IN",
            "table_id": str(table.id),
            "immediate_payment": "POS_TERMINAL",
            "items": [{"item_id": str(item.id), "quantity": 1}],
        },
        headers=headers,
    )
    assert create_resp.status_code == 201
    order_id = create_resp.json()["id"]

    await test_session.refresh(table)
    assert table.status == TableStatus.AWAITING_FOOD

    # 2. Cancel the order from POS
    cancel_payload = {
        "reason": "Customer emergency departure before food prepared",
        "refund_payment": True,
    }
    cancel_resp = await test_client.post(
        f"/api/v1/pos/orders/{order_id}/cancel",
        json=cancel_payload,
        headers=headers,
    )
    assert cancel_resp.status_code == 200
    cancel_data = cancel_resp.json()
    assert cancel_data["status"] == "CANCELLED"
    assert cancel_data["is_refunded"] is True
    assert cancel_data["table_freed"] is True

    # 3. Verify Table is freed back to AVAILABLE
    await test_session.refresh(table)
    assert table.status == TableStatus.AVAILABLE

    # 4. Verify Payment is marked REFUNDED
    pay_stmt = select(Payment).where(Payment.order_id == uuid.UUID(order_id))
    pay_res = await test_session.execute(pay_stmt)
    payment = pay_res.scalar_one_or_none()
    assert payment is not None
    assert payment.status == PaymentStatus.REFUNDED

    # 5. Verify cancellation reason persisted on Order
    order_stmt = select(Order).where(Order.id == uuid.UUID(order_id))
    order_res = await test_session.execute(order_stmt)
    order = order_res.scalar_one_or_none()
    assert order is not None
    assert order.status == OrderStatus.CANCELLED
    assert order.cancellation_reason == "Customer emergency departure before food prepared"
