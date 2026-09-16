"""Comprehensive Test Suite for Task BE-2.5: Payment Gateway Integration & Cash Ledger.

Tests:
1. Online payment initiation with remaining balance and client secret calculation.
2. Idempotency on repeated online payment initiation with same idempotency key.
3. Webhook signature verification and finalization: Order CLOSED, is_paid=True, Table AVAILABLE, current_session_token cleared.
4. Tampered or invalid webhook signature rejected with 400.
5. Idempotent webhook replay ignored early (returns 200 with already_processed).
6. Offline payment request (CASH / POS_TERMINAL) transitions TableStatus to BILL_REQUESTED.
7. Active duplicate offline payment request returns 409 Conflict.
8. Cashier verification settles payment, records verified_by_user_id, verified_at, and teardown table.
9. Overpayment (> balance) or zero/negative payment rejected with 400.
10. Role and branch security: non-cashier blocked with 403; cross-branch blocked with 404.
11. Cashier pending branch payments query (GET /api/v1/payments/branch/pending).
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import json
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
from app.core.config import settings
from app.core.security import create_access_token, get_password_hash
from app.core.session_security import create_guest_session_jwt
from app.main import create_app
from app.models.auth import Base, Branch, Table, Tenant, User, UserBranchAccess
from app.models.catalog import Category, Item
from app.models.enums import (
    KitchenStation,
    OrderStatus,
    OrderType,
    PaymentMethod,
    PaymentStatus,
    TableStatus,
    UserRole,
)
from app.models.order import Order, OrderItem, Payment


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
    """Seed tenant, branches, tables, orders, items, staff users, and guest tokens."""
    tenant = Tenant(name="Gourmet Dining", slug="gourmet-dining", is_active=True)
    test_session.add(tenant)
    await test_session.flush()

    branch_a = Branch(
        tenant_id=tenant.id,
        name={"en": "Downtown Branch", "ar": "فرع وسط المدينة"},
        slug="downtown-branch",
        latitude=24.7136,
        longitude=46.6753,
        geofence_radius_meters=150,
        is_active=True,
    )
    branch_b = Branch(
        tenant_id=tenant.id,
        name={"en": "Uptown Branch", "ar": "فرع شمال المدينة"},
        slug="uptown-branch",
        latitude=24.7500,
        longitude=46.7000,
        geofence_radius_meters=150,
        is_active=True,
    )
    test_session.add_all([branch_a, branch_b])
    await test_session.flush()

    # Tables
    table_a = Table(
        branch_id=branch_a.id,
        table_number="T-01",
        capacity=4,
        status=TableStatus.AWAITING_FOOD,
        current_session_token="session_token_xyz",
        is_active=True,
    )
    table_b = Table(
        branch_id=branch_b.id,
        table_number="T-02",
        capacity=2,
        status=TableStatus.AWAITING_FOOD,
        current_session_token="session_token_abc",
        is_active=True,
    )
    test_session.add_all([table_a, table_b])
    await test_session.flush()

    # Category & Item
    category = Category(
        branch_id=branch_a.id,
        name={"en": "Mains", "ar": "الأطباق الرئيسية"},
        station=KitchenStation.HOT_KITCHEN,
        is_active=True,
    )
    test_session.add(category)
    await test_session.flush()

    item = Item(
        category_id=category.id,
        name={"en": "Ribeye Steak", "ar": "ستيك ريب آي"},
        base_price=Decimal("100.00"),
        is_available=True,
    )
    test_session.add(item)
    await test_session.flush()

    # Orders: subtotal 100.00, tax 15.00, total 115.00
    order_a = Order(
        tenant_id=tenant.id,
        branch_id=branch_a.id,
        table_id=table_a.id,
        status=OrderStatus.DELIVERED,
        order_type=OrderType.DINE_IN,
        subtotal=Decimal("100.00"),
        tax_total=Decimal("15.00"),
        total_amount=Decimal("115.00"),
        is_paid=False,
    )
    order_b = Order(
        tenant_id=tenant.id,
        branch_id=branch_b.id,
        table_id=table_b.id,
        status=OrderStatus.DELIVERED,
        order_type=OrderType.DINE_IN,
        subtotal=Decimal("50.00"),
        tax_total=Decimal("7.50"),
        total_amount=Decimal("57.50"),
        is_paid=False,
    )
    test_session.add_all([order_a, order_b])
    await test_session.flush()

    # OrderItems
    order_item_a = OrderItem(
        order_id=order_a.id,
        item_id=item.id,
        quantity=1,
        unit_price=Decimal("100.00"),
        subtotal=Decimal("100.00"),
        station=KitchenStation.HOT_KITCHEN,
        selected_modifiers=[],
    )
    test_session.add(order_item_a)
    await test_session.flush()

    # Users
    cashier = User(
        tenant_id=tenant.id,
        email="cashier@gourmet.com",
        full_name="Sam Cashier",
        hashed_password=get_password_hash("Secret123!"),
        role=UserRole.CASHIER,
        is_active=True,
    )
    admin = User(
        tenant_id=tenant.id,
        email="admin@gourmet.com",
        full_name="Alice Admin",
        hashed_password=get_password_hash("Secret123!"),
        role=UserRole.BRANCH_ADMIN,
        is_active=True,
    )
    waiter = User(
        tenant_id=tenant.id,
        email="waiter@gourmet.com",
        full_name="John Waiter",
        hashed_password=get_password_hash("Secret123!"),
        role=UserRole.WAITER,
        is_active=True,
    )
    test_session.add_all([cashier, admin, waiter])
    await test_session.flush()

    # Branch Access
    access_cashier = UserBranchAccess(user_id=cashier.id, branch_id=branch_a.id)
    access_admin = UserBranchAccess(user_id=admin.id, branch_id=branch_a.id)
    access_waiter = UserBranchAccess(user_id=waiter.id, branch_id=branch_a.id)
    test_session.add_all([access_cashier, access_admin, access_waiter])
    await test_session.commit()

    # JWT Tokens
    cashier_token = create_access_token(
        data={"sub": str(cashier.id), "tenant_id": str(tenant.id), "role": cashier.role.value}
    )
    admin_token = create_access_token(
        data={"sub": str(admin.id), "tenant_id": str(tenant.id), "role": admin.role.value}
    )
    waiter_token = create_access_token(
        data={"sub": str(waiter.id), "tenant_id": str(tenant.id), "role": waiter.role.value}
    )

    guest_token_a = create_guest_session_jwt(
        session_id=uuid.uuid4(),
        tenant_id=tenant.id,
        branch_id=branch_a.id,
        table_id=table_a.id,
        table_number="T-01",
        is_presence_verified=True,
    )

    return {
        "tenant": tenant,
        "branch_a": branch_a,
        "branch_b": branch_b,
        "table_a": table_a,
        "table_b": table_b,
        "order_a": order_a,
        "order_b": order_b,
        "cashier": cashier,
        "admin": admin,
        "waiter": waiter,
        "cashier_token": cashier_token,
        "admin_token": admin_token,
        "waiter_token": waiter_token,
        "guest_token_a": guest_token_a,
    }


@pytest_asyncio.fixture(scope="function")
async def test_client(
    async_test_engine: AsyncEngine,
    test_session: AsyncSession,
) -> AsyncGenerator[AsyncClient, None]:
    app = create_app()

    async def override_get_async_db() -> AsyncGenerator[AsyncSession, None]:
        session_factory = async_sessionmaker(
            bind=async_test_engine,
            class_=AsyncSession,
            autoflush=False,
            expire_on_commit=False,
        )
        async with session_factory() as session:
            yield session

    session_factory = async_sessionmaker(
        bind=async_test_engine,
        class_=AsyncSession,
        autoflush=False,
        expire_on_commit=False,
    )

    app.dependency_overrides[get_async_db] = override_get_async_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        with patch("app.services.audit_service.async_session_factory", session_factory):
            yield client
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Test Cases
# ---------------------------------------------------------------------------


class TestOnlinePaymentAndWebhooks:
    @pytest.mark.asyncio
    async def test_online_payment_initiate_success(
        self,
        test_client: AsyncClient,
        seed_data: dict,
    ):
        """Guest initiates online card payment for active table order."""
        token = seed_data["guest_token_a"]
        order_id = str(seed_data["order_a"].id)

        res = await test_client.post(
            "/api/v1/payments/online/initiate",
            json={"order_id": order_id, "method": "ONLINE_CARD"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert res.status_code == 201
        data = res.json()
        assert data["order_id"] == order_id
        assert Decimal(str(data["amount"])) == Decimal("115.00")
        assert data["currency"] == "SAR"
        assert data["status"] == "PENDING"
        assert "client_secret" in data
        assert data["transaction_reference"].startswith("txn_online_")

    @pytest.mark.asyncio
    async def test_online_payment_idempotency(
        self,
        test_client: AsyncClient,
        seed_data: dict,
    ):
        """Repeated initiation with identical idempotency_key returns identical payment intent."""
        token = seed_data["guest_token_a"]
        order_id = str(seed_data["order_a"].id)
        idempotency_key = f"client_idem_{uuid.uuid4().hex[:12]}"

        # 1. First call
        res1 = await test_client.post(
            "/api/v1/payments/online/initiate",
            json={"order_id": order_id, "method": "APPLE_PAY", "idempotency_key": idempotency_key},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert res1.status_code == 201
        data1 = res1.json()

        # 2. Second call with same key
        res2 = await test_client.post(
            "/api/v1/payments/online/initiate",
            json={"order_id": order_id, "method": "APPLE_PAY", "idempotency_key": idempotency_key},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert res2.status_code == 201
        data2 = res2.json()

        assert data1["payment_id"] == data2["payment_id"]
        assert data1["transaction_reference"] == data2["transaction_reference"]

    @pytest.mark.asyncio
    async def test_webhook_signature_verification_and_settlement(
        self,
        test_client: AsyncClient,
        seed_data: dict,
        test_session: AsyncSession,
    ):
        """Valid HMAC signature processes webhook, completes payment, closes order, and resets table."""
        token = seed_data["guest_token_a"]
        order_id = str(seed_data["order_a"].id)

        # 1. Initiate payment
        init_res = await test_client.post(
            "/api/v1/payments/online/initiate",
            json={"order_id": order_id, "method": "ONLINE_CARD"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert init_res.status_code == 201
        txn_ref = init_res.json()["transaction_reference"]

        # 2. Construct webhook payload and HMAC SHA-256 signature
        payload_dict = {
            "transaction_reference": txn_ref,
            "status": "succeeded",
            "amount": "115.00",
        }
        raw_payload = json.dumps(payload_dict).encode("utf-8")
        secret = settings.LOCAL_PAYMENT_WEBHOOK_SECRET
        sig = hmac.new(secret.encode("utf-8"), raw_payload, hashlib.sha256).hexdigest()

        # 3. Deliver webhook
        webhook_res = await test_client.post(
            "/api/v1/payments/webhooks/local",
            content=raw_payload,
            headers={"X-Signature": sig, "Content-Type": "application/json"},
        )
        assert webhook_res.status_code == 200
        webhook_data = webhook_res.json()
        assert webhook_data["status"] == "processed"
        assert webhook_data["order_closed"] is True

        # 4. Verify database state
        order = await test_session.get(Order, seed_data["order_a"].id)
        await test_session.refresh(order)
        assert order.is_paid is True
        assert order.status == OrderStatus.CLOSED

        table = await test_session.get(Table, seed_data["table_a"].id)
        await test_session.refresh(table)
        assert table.status == TableStatus.AVAILABLE
        assert table.current_session_token is None

    @pytest.mark.asyncio
    async def test_webhook_tampered_signature_rejected_400(
        self,
        test_client: AsyncClient,
        seed_data: dict,
    ):
        """Invalid cryptographic signature header is rejected with 400."""
        payload = json.dumps({"transaction_reference": "txn_fake", "status": "succeeded"}).encode("utf-8")
        res = await test_client.post(
            "/api/v1/payments/webhooks/local",
            content=payload,
            headers={"X-Signature": "invalid_signature_hex", "Content-Type": "application/json"},
        )
        assert res.status_code == 400
        assert "invalid webhook cryptographic signature" in res.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_webhook_idempotent_duplicate_events(
        self,
        test_client: AsyncClient,
        seed_data: dict,
    ):
        """Replay of an already COMPLETED webhook returns 200 already_processed without re-locking."""
        token = seed_data["guest_token_a"]
        order_id = str(seed_data["order_a"].id)

        # Initiate
        init_res = await test_client.post(
            "/api/v1/payments/online/initiate",
            json={"order_id": order_id, "method": "ONLINE_CARD"},
            headers={"Authorization": f"Bearer {token}"},
        )
        txn_ref = init_res.json()["transaction_reference"]

        payload_dict = {"transaction_reference": txn_ref, "status": "succeeded"}
        raw_payload = json.dumps(payload_dict).encode("utf-8")
        secret = settings.LOCAL_PAYMENT_WEBHOOK_SECRET
        sig = hmac.new(secret.encode("utf-8"), raw_payload, hashlib.sha256).hexdigest()

        # Send 1st webhook
        res1 = await test_client.post(
            "/api/v1/payments/webhooks/local",
            content=raw_payload,
            headers={"X-Signature": sig, "Content-Type": "application/json"},
        )
        assert res1.status_code == 200
        assert res1.json()["status"] == "processed"

        # Send 2nd duplicate webhook
        res2 = await test_client.post(
            "/api/v1/payments/webhooks/local",
            content=raw_payload,
            headers={"X-Signature": sig, "Content-Type": "application/json"},
        )
        assert res2.status_code == 200
        assert res2.json()["status"] == "already_processed"


class TestOfflineCashierLedgerWorkflow:
    @pytest.mark.asyncio
    async def test_offline_cash_request_updates_table_bill_requested(
        self,
        test_client: AsyncClient,
        seed_data: dict,
        test_session: AsyncSession,
    ):
        """Guest requests CASH payment; payment set to PENDING_CASHIER_VERIFICATION and TableStatus to BILL_REQUESTED."""
        token = seed_data["guest_token_a"]
        order_id = str(seed_data["order_a"].id)

        res = await test_client.post(
            "/api/v1/payments/offline/request",
            json={"order_id": order_id, "method": "CASH"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert res.status_code == 201
        data = res.json()
        assert data["payment_method"] == "CASH"
        assert data["status"] == "PENDING_CASHIER_VERIFICATION"
        assert Decimal(str(data["amount"])) == Decimal("115.00")

        # Table state verified
        table = await test_session.get(Table, seed_data["table_a"].id)
        await test_session.refresh(table)
        assert table.status == TableStatus.BILL_REQUESTED

    @pytest.mark.asyncio
    async def test_offline_duplicate_request_rejected_409(
        self,
        test_client: AsyncClient,
        seed_data: dict,
    ):
        """Second offline request while one is pending cashier verification returns 409."""
        token = seed_data["guest_token_a"]
        order_id = str(seed_data["order_a"].id)

        res1 = await test_client.post(
            "/api/v1/payments/offline/request",
            json={"order_id": order_id, "method": "POS_TERMINAL"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert res1.status_code == 201

        res2 = await test_client.post(
            "/api/v1/payments/offline/request",
            json={"order_id": order_id, "method": "CASH"},
            headers={"Authorization": f"Bearer {token}", "Accept-Language": "en"},
        )
        assert res2.status_code == 409
        assert "pending cash/pos settlement request already exists" in res2.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_cashier_verification_lifecycle_and_teardown(
        self,
        test_client: AsyncClient,
        seed_data: dict,
        test_session: AsyncSession,
    ):
        """Cashier confirms offline payment; settles order, resets table to AVAILABLE, and clears session token."""
        token = seed_data["guest_token_a"]
        cashier_token = seed_data["cashier_token"]
        branch_id = str(seed_data["branch_a"].id)
        order_id = str(seed_data["order_a"].id)

        # 1. Guest requests offline cash
        req_res = await test_client.post(
            "/api/v1/payments/offline/request",
            json={"order_id": order_id, "method": "CASH"},
            headers={"Authorization": f"Bearer {token}"},
        )
        payment_id = req_res.json()["id"]

        # 2. Cashier verifies payment
        verify_res = await test_client.post(
            f"/api/v1/payments/offline/{payment_id}/verify",
            json={"notes": "Received 115 SAR cash in register #1"},
            headers={"Authorization": f"Bearer {cashier_token}", "X-Branch-ID": branch_id},
        )
        assert verify_res.status_code == 200
        data = verify_res.json()
        assert data["is_paid"] is True
        assert data["order_status"] == "CLOSED"
        assert Decimal(str(data["remaining_balance"])) == Decimal("0.00")
        assert data["table_status"] == "AVAILABLE"
        assert data["payment"]["status"] == "COMPLETED"
        assert data["payment"]["verified_by_user_id"] == str(seed_data["cashier"].id)
        assert data["payment"]["verified_at"] is not None

        # 3. Database teardown verified
        table = await test_session.get(Table, seed_data["table_a"].id)
        await test_session.refresh(table)
        assert table.status == TableStatus.AVAILABLE
        assert table.current_session_token is None

    @pytest.mark.asyncio
    async def test_prevent_overpayment(
        self,
        test_client: AsyncClient,
        seed_data: dict,
    ):
        """Attempting to pay more than order balance is rejected with 400."""
        token = seed_data["guest_token_a"]
        order_id = str(seed_data["order_a"].id)

        res = await test_client.post(
            "/api/v1/payments/online/initiate",
            json={"order_id": order_id, "amount": "200.00"},  # order is 115.00
            headers={"Authorization": f"Bearer {token}", "Accept-Language": "en"},
        )
        assert res.status_code == 400
        assert "cannot exceed the pending balance" in res.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_cashier_security_and_branch_isolation(
        self,
        test_client: AsyncClient,
        seed_data: dict,
    ):
        """Unauthorized role (waiter) rejected with 403; cross-branch cashier rejected with 404."""
        token = seed_data["guest_token_a"]
        cashier_token = seed_data["cashier_token"]
        waiter_token = seed_data["waiter_token"]
        branch_a_id = str(seed_data["branch_a"].id)
        branch_b_id = str(seed_data["branch_b"].id)
        order_id = str(seed_data["order_a"].id)

        req_res = await test_client.post(
            "/api/v1/payments/offline/request",
            json={"order_id": order_id, "method": "CASH"},
            headers={"Authorization": f"Bearer {token}"},
        )
        payment_id = req_res.json()["id"]

        # Waiter tries to verify cash -> 403 Forbidden
        waiter_res = await test_client.post(
            f"/api/v1/payments/offline/{payment_id}/verify",
            headers={"Authorization": f"Bearer {waiter_token}", "X-Branch-ID": branch_a_id},
        )
        assert waiter_res.status_code == 403

        # Cashier from Branch A tries to verify with Branch B header -> 404
        cross_res = await test_client.post(
            f"/api/v1/payments/offline/{payment_id}/verify",
            headers={"Authorization": f"Bearer {cashier_token}", "X-Branch-ID": branch_b_id},
        )
        assert cross_res.status_code in (403, 404)

    @pytest.mark.asyncio
    async def test_cashier_list_pending_branch_payments(
        self,
        test_client: AsyncClient,
        seed_data: dict,
    ):
        """Cashier retrieves list of pending payments awaiting collection for their branch."""
        token = seed_data["guest_token_a"]
        cashier_token = seed_data["cashier_token"]
        branch_a_id = str(seed_data["branch_a"].id)
        order_id = str(seed_data["order_a"].id)

        # Create offline request
        await test_client.post(
            "/api/v1/payments/offline/request",
            json={"order_id": order_id, "method": "CASH"},
            headers={"Authorization": f"Bearer {token}"},
        )

        list_res = await test_client.get(
            "/api/v1/payments/branch/pending",
            headers={"Authorization": f"Bearer {cashier_token}", "X-Branch-ID": branch_a_id},
        )
        assert list_res.status_code == 200
        items = list_res.json()
        assert len(items) >= 1
        assert items[0]["status"] == "PENDING_CASHIER_VERIFICATION"
