"""Dedicated Test Suite for Task BE-2.6: Phase 2 Hardening & Concurrency Remediation.

Asserts:
1. Global Lock Hierarchy & Deadlock Elimination (Table -> Order) under concurrent checkout & transitions.
2. Departed Diner Session Lockout on freed (AVAILABLE) or reclaimed tables (401 SESSION_TERMINATED_TABLE_AVAILABLE).
3. Table Teardown Cleanup: Active service requests are atomically dismissed on full settlement.
4. Service Request Race Condition: Simultaneous requests on the same table are serialized by Table row lock (1 succeeds, 1 gets 409).
5. Stripe Webhook Timestamp Freshness (rejects expired timestamp > 300s with 400).
6. Geodesic Antipodal Clamping: haversine_distance_meters handles exact 180° coordinates without math domain error.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import hmac
import time
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
from app.core.config import settings
from app.core.geo import haversine_distance_meters
from app.core.security import create_access_token, get_password_hash
from app.core.session_security import create_guest_session_jwt
from app.main import create_app
from app.models.auth import Base, Branch, Table, Tenant, User, UserBranchAccess
from app.models.catalog import Category, Item
from app.models.enums import (
    KitchenStation,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
    ServiceRequestStatus,
    ServiceRequestType,
    TableStatus,
    UserRole,
)
from app.models.order import Order, OrderItem, Payment
from app.models.service import ServiceRequest
from app.services.service_request_service import ServiceRequestService
from app.schemas.service_request import CreateServiceRequest
from app.schemas.session import GuestSessionContext


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
async def seed_remediation_data(test_session: AsyncSession) -> dict:
    """Seed tenant, branch, staff, tables, items, and dining guest sessions."""
    tenant = Tenant(name="Remediation Lounge", slug="remediation-lounge", is_active=True)
    test_session.add(tenant)
    await test_session.flush()

    branch = Branch(
        tenant_id=tenant.id,
        name={"en": "Capital Branch", "ar": "فرع العاصمة"},
        slug="capital-branch",
        latitude=24.7136,
        longitude=46.6753,
        geofence_radius_meters=150,
        is_active=True,
    )
    test_session.add(branch)
    await test_session.flush()

    # Table 1: In active browsing / dining session
    active_session_id = uuid.uuid4()
    table = Table(
        branch_id=branch.id,
        table_number="T-10",
        capacity=4,
        status=TableStatus.BROWSING,
        current_session_token=str(active_session_id),
        is_active=True,
    )
    test_session.add(table)
    await test_session.flush()

    # Staff Cashier & Admin
    pw_hash = get_password_hash("SecretPass123!")
    cashier = User(
        tenant_id=tenant.id,
        email="cashier@remediation.com",
        full_name="Remediation Cashier",
        hashed_password=pw_hash,
        role=UserRole.CASHIER,
        is_active=True,
    )
    admin = User(
        tenant_id=tenant.id,
        email="admin@remediation.com",
        full_name="Remediation Admin",
        hashed_password=pw_hash,
        role=UserRole.BRANCH_ADMIN,
        is_active=True,
    )
    test_session.add_all([cashier, admin])
    await test_session.flush()

    access_cashier = UserBranchAccess(user_id=cashier.id, branch_id=branch.id)
    access_admin = UserBranchAccess(user_id=admin.id, branch_id=branch.id)
    test_session.add_all([access_cashier, access_admin])
    await test_session.flush()

    # Menu item
    category = Category(
        branch_id=branch.id,
        name={"en": "Main Dishes", "ar": "الأطباق الرئيسية"},
        display_order=1,
        station=KitchenStation.HOT_KITCHEN,
        is_active=True,
    )
    test_session.add(category)
    await test_session.flush()

    item = Item(
        category_id=category.id,
        name={"en": "Club Sandwich", "ar": "كلوب ساندوتش"},
        base_price=Decimal("30.00"),
        station=KitchenStation.HOT_KITCHEN,
        is_available=True,
    )
    test_session.add(item)
    await test_session.commit()

    # JWT Tokens
    guest_token = create_guest_session_jwt(
        session_id=active_session_id,
        tenant_id=tenant.id,
        branch_id=branch.id,
        table_id=table.id,
        table_number=table.table_number,
        is_presence_verified=True,
    )
    cashier_token = create_access_token(
        data={"sub": str(cashier.id), "tenant_id": str(tenant.id), "role": cashier.role.value}
    )
    admin_token = create_access_token(
        data={"sub": str(admin.id), "tenant_id": str(tenant.id), "role": admin.role.value}
    )

    return {
        "tenant": tenant,
        "branch": branch,
        "table": table,
        "item": item,
        "active_session_id": active_session_id,
        "guest_token": guest_token,
        "cashier_token": cashier_token,
        "admin_token": admin_token,
    }


@pytest_asyncio.fixture(scope="function")
async def test_client(
    async_test_engine: AsyncEngine,
    test_session: AsyncSession,
) -> AsyncGenerator[AsyncClient, None]:
    app = create_app()

    session_factory = async_sessionmaker(
        bind=async_test_engine,
        class_=AsyncSession,
        autoflush=False,
        expire_on_commit=False,
    )

    async def override_get_async_db() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_async_db] = override_get_async_db

    transport = ASGITransport(app=app)
    with patch("app.core.database.async_session_factory", session_factory), \
         patch("app.services.audit_service.async_session_factory", session_factory):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Test Cases
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestPhase2Remediations:
    """Covers all 6 remediation checkpoints specified in Task BE-2.6."""

    async def test_lock_hierarchy_concurrency_no_deadlock(
        self,
        test_client: AsyncClient,
        seed_remediation_data: dict,
    ) -> None:
        """Checkpoint 1: Concurrent checkout_order and transition_order_status execute without deadlock.

        Standardized Table (Parent) -> Order (Child) acquisition hierarchy prevents AB-BA lock inversion.
        """
        guest_token = seed_remediation_data["guest_token"]
        admin_token = seed_remediation_data["admin_token"]
        branch_id = str(seed_remediation_data["branch"].id)
        item_id = str(seed_remediation_data["item"].id)

        # 1. Place initial order
        payload = {"items": [{"item_id": item_id, "quantity": 1, "selected_groups": []}]}
        res = await test_client.post(
            "/api/v1/orders/checkout",
            json=payload,
            headers={"Authorization": f"Bearer {guest_token}"},
        )
        assert res.status_code == 201
        order_id = res.json()["id"]

        # 2. Fire concurrent append checkout and status transition simultaneously
        append_payload = {"items": [{"item_id": item_id, "quantity": 2, "selected_groups": []}]}

        async def do_checkout():
            return await test_client.post(
                "/api/v1/orders/checkout",
                json=append_payload,
                headers={"Authorization": f"Bearer {guest_token}"},
            )

        async def do_transition():
            return await test_client.post(
                f"/api/v1/orders/{order_id}/transition",
                json={"target_status": "PREPARING"},
                headers={"Authorization": f"Bearer {admin_token}", "X-Branch-ID": branch_id},
            )

        res_checkout, res_transition = await asyncio.gather(do_checkout(), do_transition())

        # Assert neither failed with a deadlock / 500 error
        assert res_checkout.status_code in (200, 201)
        assert res_transition.status_code == 200
        assert res_transition.json()["to_status"] == "PREPARING"

    async def test_departed_diner_session_lockout(
        self,
        test_client: AsyncClient,
        test_session: AsyncSession,
        seed_remediation_data: dict,
    ) -> None:
        """Checkpoint 2: Departed diner is locked out once table is cleared to AVAILABLE or reclaimed."""
        guest_token = seed_remediation_data["guest_token"]
        item_id = str(seed_remediation_data["item"].id)
        table = seed_remediation_data["table"]

        # Scenario A: Table settled and reset to AVAILABLE with current_session_token = None
        table.status = TableStatus.AVAILABLE
        table.current_session_token = None
        await test_session.commit()

        payload = {"items": [{"item_id": item_id, "quantity": 1, "selected_groups": []}]}
        res_freed = await test_client.post(
            "/api/v1/orders/checkout",
            json=payload,
            headers={"Authorization": f"Bearer {guest_token}", "Accept-Language": "en"},
        )
        assert res_freed.status_code == 401
        assert "SESSION_TERMINATED_TABLE_AVAILABLE" in res_freed.json()["detail"] or "terminated" in res_freed.json()["detail"].lower()

        # Scenario B: Table reclaimed by a new dining party with a new session token
        table.status = TableStatus.BROWSING
        table.current_session_token = str(uuid.uuid4())  # New party token
        await test_session.commit()

        res_reclaimed = await test_client.post(
            "/api/v1/orders/checkout",
            json=payload,
            headers={"Authorization": f"Bearer {guest_token}", "Accept-Language": "en"},
        )
        assert res_reclaimed.status_code == 401
        assert "SESSION_TERMINATED_TABLE_AVAILABLE" in res_reclaimed.json()["detail"] or "terminated" in res_reclaimed.json()["detail"].lower()

    async def test_table_teardown_cleans_service_requests(
        self,
        test_client: AsyncClient,
        test_session: AsyncSession,
        seed_remediation_data: dict,
    ) -> None:
        """Checkpoint 3: Table teardown on payment settlement dismisses active service requests.

        A subsequent dining party can immediately submit a service request without encountering 409.
        """
        guest_token = seed_remediation_data["guest_token"]
        cashier_token = seed_remediation_data["cashier_token"]
        table = seed_remediation_data["table"]
        item = seed_remediation_data["item"]
        branch_id = seed_remediation_data["branch"].id
        tenant_id = seed_remediation_data["tenant"].id

        # 1. Create order
        order = Order(
            tenant_id=tenant_id,
            branch_id=branch_id,
            table_id=table.id,
            status=OrderStatus.SUBMITTED,
            subtotal=Decimal("30.00"),
            tax_total=Decimal("4.50"),
            total_amount=Decimal("34.50"),
        )
        test_session.add(order)
        await test_session.flush()

        # 2. Guest opens a PENDING water request (backdated beyond 60s cooldown)
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        water_req_id = uuid.uuid4()
        req_water = ServiceRequest(
            id=water_req_id,
            branch_id=branch_id,
            table_id=table.id,
            request_type=ServiceRequestType.WATER,
            status=ServiceRequestStatus.PENDING,
            created_at=now_utc - datetime.timedelta(seconds=120),
        )
        test_session.add(req_water)
        await test_session.flush()

        # 3. Create pending offline payment for full amount
        payment = Payment(
            tenant_id=tenant_id,
            branch_id=branch_id,
            order_id=order.id,
            payment_method=PaymentMethod.CASH,
            amount=Decimal("34.50"),
            status=PaymentStatus.PENDING_CASHIER_VERIFICATION,
        )
        test_session.add(payment)
        await test_session.commit()

        # 4. Cashier confirms settlement
        res_settle = await test_client.post(
            f"/api/v1/payments/offline/{payment.id}/verify",
            headers={"Authorization": f"Bearer {cashier_token}", "X-Branch-ID": str(branch_id)},
        )
        assert res_settle.status_code == 200
        assert res_settle.json()["order_status"] == "CLOSED"
        assert res_settle.json()["is_paid"] is True
        assert res_settle.json()["table_status"] == "AVAILABLE"

        # 5. Assert ServiceRequest transitioned to DISMISSED with timestamp
        test_session.expire_all()
        refreshed_req = (
            await test_session.execute(
                select(ServiceRequest).where(ServiceRequest.id == water_req_id)
            )
        ).scalar_one()
        assert refreshed_req.status == ServiceRequestStatus.DISMISSED
        assert refreshed_req.dismissed_at is not None

        # 6. New dining party scans table and requests WATER successfully without 409
        new_session_id = uuid.uuid4()
        table.current_session_token = str(new_session_id)
        table.status = TableStatus.BROWSING
        await test_session.commit()

        new_guest_token = create_guest_session_jwt(
            session_id=new_session_id,
            tenant_id=tenant_id,
            branch_id=branch_id,
            table_id=table.id,
            table_number=table.table_number,
            is_presence_verified=True,
        )

        res_new_water = await test_client.post(
            "/api/v1/service-requests",
            json={"request_type": "WATER"},
            headers={"Authorization": f"Bearer {new_guest_token}"},
        )
        assert res_new_water.status_code == 201
        assert res_new_water.json()["status"] == "PENDING"

    async def test_service_request_concurrency_race(
        self,
        test_client: AsyncClient,
        seed_remediation_data: dict,
    ) -> None:
        """Checkpoint 4: Simultaneous service requests on the same table are serialized by Table row lock.

        Exactly one succeeds (201) and the other is rejected with 409 Conflict (ACTIVE_REQUEST_EXISTS).
        """
        guest_token = seed_remediation_data["guest_token"]

        async def request_cutlery():
            return await test_client.post(
                "/api/v1/service-requests",
                json={"request_type": "CUTLERY"},
                headers={"Authorization": f"Bearer {guest_token}"},
            )

        # Fire two identical requests at the exact same moment
        res1, res2 = await asyncio.gather(request_cutlery(), request_cutlery())

        statuses = {res1.status_code, res2.status_code}
        assert statuses == {201, 409}, f"Expected one 201 and one 409, got {res1.status_code} and {res2.status_code}"

    async def test_stripe_webhook_timestamp_expiry(
        self,
        test_client: AsyncClient,
        seed_remediation_data: dict,
    ) -> None:
        """Checkpoint 5: Stripe webhook with timestamp older than 300 seconds is rejected with 400."""
        secret = settings.LOCAL_PAYMENT_WEBHOOK_SECRET
        expired_timestamp = int(time.time()) - 600  # 10 minutes ago
        raw_body = b'{"type": "payment_intent.succeeded", "data": {"object": {"id": "pi_expired"}}}'

        signed_payload = f"{expired_timestamp}.".encode("utf-8") + raw_body
        valid_hmac = hmac.new(
            secret.encode("utf-8"),
            signed_payload,
            hashlib.sha256,
        ).hexdigest()

        header_sig = f"t={expired_timestamp},v1={valid_hmac}"

        with patch("app.core.config.settings.STRIPE_WEBHOOK_SECRET", secret):
            res = await test_client.post(
                "/api/v1/payments/webhooks/stripe",
                content=raw_body,
                headers={"Stripe-Signature": header_sig, "Content-Type": "application/json"},
            )

        assert res.status_code == 400
        assert "timestamp expired" in res.json()["detail"].lower()

    async def test_haversine_antipodal_clamping(self) -> None:
        """Checkpoint 6: haversine_distance_meters evaluates antipodal coordinates without math domain error."""
        # 180 degrees difference on equator
        distance = haversine_distance_meters(0.0, 0.0, 0.0, 180.0)
        assert isinstance(distance, float)
        # Earth half-circumference is approx 20,015,087 meters
        assert 20_000_000 <= distance <= 20_100_000
