"""Automated test suite for Task BE-4.2: Branch Financials & End-of-Day Z-Report Generator."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
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
    DrawerStatus,
    OrderSource,
    OrderStatus,
    OrderType,
    PaymentMethod,
    PaymentStatus,
    UserRole,
)
from app.models.financials import CashDrawerSession, FinancialBase, ZReport
from app.models.order import Order, Payment
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

    transport = ASGITransport(app=app)
    with patch("app.core.database.async_session_factory", session_factory), \
         patch("app.api.deps.async_session_factory", session_factory):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client

    app.dependency_overrides.clear()


@pytest_asyncio.fixture(scope="function")
async def seed_financial_data(test_session: AsyncSession) -> dict:
    """Seed tenant, branches, users with role assignments and tokens."""
    tenant = Tenant(
        id=uuid.uuid4(),
        name="Artisan Hospitality",
        slug="artisan-hosp",
        is_active=True,
    )
    test_session.add(tenant)
    await test_session.flush()

    branch_a = Branch(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        name={"en": "Branch Alpha", "ar": "فرع ألفا"},
        slug="branch-alpha",
        latitude=Decimal("24.7136"),
        longitude=Decimal("46.6753"),
        geofence_radius_meters=200,
        is_active=True,
    )
    branch_b = Branch(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        name={"en": "Branch Beta", "ar": "فرع بيتا"},
        slug="branch-beta",
        latitude=Decimal("24.7200"),
        longitude=Decimal("46.6800"),
        geofence_radius_meters=200,
        is_active=True,
    )
    test_session.add_all([branch_a, branch_b])
    await test_session.flush()

    pw = get_password_hash("Password123!")

    # Users
    cashier = User(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        email="cashier@alpha.com",
        hashed_password=pw,
        full_name="Cashier Alpha",
        role=UserRole.CASHIER,
        is_active=True,
    )
    admin = User(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        email="admin@alpha.com",
        hashed_password=pw,
        full_name="Admin Alpha",
        role=UserRole.BRANCH_ADMIN,
        is_active=True,
    )
    super_admin = User(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        email="super@hosp.com",
        hashed_password=pw,
        full_name="Super Administrator",
        role=UserRole.SUPER_ADMIN,
        is_active=True,
    )
    cashier_b = User(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        email="cashier@beta.com",
        hashed_password=pw,
        full_name="Cashier Beta",
        role=UserRole.CASHIER,
        is_active=True,
    )
    test_session.add_all([cashier, admin, super_admin, cashier_b])
    await test_session.flush()

    # Branch Access Wiring
    acc_c1 = UserBranchAccess(user_id=cashier.id, branch_id=branch_a.id)
    acc_a1 = UserBranchAccess(user_id=admin.id, branch_id=branch_a.id)
    acc_cb = UserBranchAccess(user_id=cashier_b.id, branch_id=branch_b.id)
    test_session.add_all([acc_c1, acc_a1, acc_cb])
    await test_session.commit()

    # Tokens
    cashier_token = create_access_token({"sub": str(cashier.id), "tenant_id": str(tenant.id), "role": cashier.role.value})
    admin_token = create_access_token({"sub": str(admin.id), "tenant_id": str(tenant.id), "role": admin.role.value})
    super_token = create_access_token({"sub": str(super_admin.id), "tenant_id": str(tenant.id), "role": super_admin.role.value})
    cashier_b_token = create_access_token({"sub": str(cashier_b.id), "tenant_id": str(tenant.id), "role": cashier_b.role.value})

    return {
        "tenant": tenant,
        "branch_a": branch_a,
        "branch_b": branch_b,
        "cashier": cashier,
        "admin": admin,
        "super_admin": super_admin,
        "cashier_b": cashier_b,
        "cashier_token": cashier_token,
        "admin_token": admin_token,
        "super_token": super_token,
        "cashier_b_token": cashier_b_token,
    }


# ---------------------------------------------------------------------------
# Test 1: Drawer Lifecycle & Concurrency
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_drawer_open_and_conflict(
    test_client: AsyncClient,
    seed_financial_data: dict,
) -> None:
    """Verify drawer opening with starting balance and 409 conflict on concurrent open."""
    branch = seed_financial_data["branch_a"]
    cashier_token = seed_financial_data["cashier_token"]

    headers = {
        "Authorization": f"Bearer {cashier_token}",
        "X-Branch-ID": str(branch.id),
    }

    # 1. Open drawer successfully
    open_res = await test_client.post(
        "/api/v1/financials/drawer/open",
        json={"opening_balance": "250.00"},
        headers=headers,
    )
    assert open_res.status_code == 201
    drawer_data = open_res.json()
    assert drawer_data["status"] == "OPEN"
    assert Decimal(str(drawer_data["opening_balance"])) == Decimal("250.00")
    assert drawer_data["branch_id"] == str(branch.id)

    # 2. Attempt to open a second drawer on the same branch -> 409 CONFLICT
    conflict_res = await test_client.post(
        "/api/v1/financials/drawer/open",
        json={"opening_balance": "100.00"},
        headers=headers,
    )
    assert conflict_res.status_code == 409
    assert "already open" in conflict_res.json()["detail"].lower()

    # 3. Query current drawer -> 200 with active session
    current_res = await test_client.get(
        "/api/v1/financials/drawer/current",
        headers=headers,
    )
    assert current_res.status_code == 200
    assert current_res.json()["id"] == drawer_data["id"]


# ---------------------------------------------------------------------------
# Test 2: Variance Calculation Verification (Shortage & Overage)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_drawer_close_variance_shortage_and_overage(
    test_client: AsyncClient,
    test_session: AsyncSession,
    seed_financial_data: dict,
) -> None:
    """Verify exact expected cash computation and negative/positive variance on drawer close."""
    branch = seed_financial_data["branch_a"]
    cashier_token = seed_financial_data["cashier_token"]
    headers = {"Authorization": f"Bearer {cashier_token}", "X-Branch-ID": str(branch.id)}

    # Open drawer with 200.00
    open_res = await test_client.post(
        "/api/v1/financials/drawer/open",
        json={"opening_balance": "200.00"},
        headers=headers,
    )
    assert open_res.status_code == 201

    # Simulate paid cash orders
    order1 = Order(
        id=uuid.uuid4(),
        tenant_id=branch.tenant_id,
        branch_id=branch.id,
        order_type=OrderType.TAKEAWAY,
        order_source=OrderSource.CASHIER_POS,
        status=OrderStatus.PAID,
        is_paid=True,
        subtotal=Decimal("100.00"),
        total_amount=Decimal("115.00"),
        created_at=datetime.now(timezone.utc),
    )
    order2 = Order(
        id=uuid.uuid4(),
        tenant_id=branch.tenant_id,
        branch_id=branch.id,
        order_type=OrderType.TAKEAWAY,
        order_source=OrderSource.CASHIER_POS,
        status=OrderStatus.PAID,
        is_paid=True,
        subtotal=Decimal("30.00"),
        total_amount=Decimal("35.00"),
        created_at=datetime.now(timezone.utc),
    )
    test_session.add_all([order1, order2])
    await test_session.flush()

    # Add completed CASH payments totaling 150.00 (115 + 35)
    pay1 = Payment(
        id=uuid.uuid4(),
        branch_id=branch.id,
        order_id=order1.id,
        payment_method=PaymentMethod.CASH,
        amount=Decimal("115.00"),
        status=PaymentStatus.COMPLETED,
        created_at=datetime.now(timezone.utc),
    )
    pay2 = Payment(
        id=uuid.uuid4(),
        branch_id=branch.id,
        order_id=order2.id,
        payment_method=PaymentMethod.CASH,
        amount=Decimal("35.00"),
        status=PaymentStatus.COMPLETED,
        created_at=datetime.now(timezone.utc),
    )
    test_session.add_all([pay1, pay2])
    await test_session.commit()

    # Scenario A: Shortage (Expected = 200 + 150 = 350. Declared = 330 -> Variance = -20.00)
    close_res = await test_client.post(
        "/api/v1/financials/drawer/close",
        json={"declared_cash_amount": "330.00", "closing_notes": "Cash shortage noted"},
        headers=headers,
    )
    assert close_res.status_code == 200
    res_data = close_res.json()
    assert res_data["status"] == "CLOSED"
    assert Decimal(str(res_data["calculated_cash_amount"])) == Decimal("350.00")
    assert Decimal(str(res_data["declared_cash_amount"])) == Decimal("330.00")
    assert Decimal(str(res_data["cash_variance"])) == Decimal("-20.00")

    # Scenario B: Re-open and test Overage
    open_res_b = await test_client.post(
        "/api/v1/financials/drawer/open",
        json={"opening_balance": "100.00"},
        headers=headers,
    )
    assert open_res_b.status_code == 201

    # Declare 125.00 when expected is 100.00 -> Variance = +25.00
    close_res_b = await test_client.post(
        "/api/v1/financials/drawer/close",
        json={"declared_cash_amount": "125.00", "closing_notes": "Cash overage found"},
        headers=headers,
    )
    assert close_res_b.status_code == 200
    b_data = close_res_b.json()
    assert Decimal(str(b_data["calculated_cash_amount"])) == Decimal("100.00")
    assert Decimal(str(b_data["cash_variance"])) == Decimal("25.00")


# ---------------------------------------------------------------------------
# Test 3: Multi-Channel & Multi-Source Itemization in Z-Report
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_z_report_multi_channel_and_multi_source(
    test_client: AsyncClient,
    test_session: AsyncSession,
    seed_financial_data: dict,
) -> None:
    """Verify gross sales, channel breakdown (Cash/Card/Online), and source breakdown (QR/POS/Takeaway)."""
    branch = seed_financial_data["branch_a"]
    admin_token = seed_financial_data["admin_token"]
    headers = {"Authorization": f"Bearer {admin_token}", "X-Branch-ID": str(branch.id)}

    now = datetime.now(timezone.utc)

    # 1. QR Customer Order (Online Card Payment): Total = 100.00
    o_qr = Order(
        id=uuid.uuid4(),
        tenant_id=branch.tenant_id,
        branch_id=branch.id,
        order_type=OrderType.DINE_IN,
        order_source=OrderSource.QR_CUSTOMER,
        status=OrderStatus.PAID,
        is_paid=True,
        subtotal=Decimal("85.00"),
        tax_total=Decimal("15.00"),
        total_amount=Decimal("100.00"),
        created_at=now,
    )
    p_qr = Payment(
        id=uuid.uuid4(),
        tenant_id=branch.tenant_id,
        branch_id=branch.id,
        order_id=o_qr.id,
        payment_method=PaymentMethod.ONLINE_CARD,
        amount=Decimal("100.00"),
        status=PaymentStatus.COMPLETED,
        created_at=now,
    )

    # 2. Cashier POS Order (Cash Payment): Total = 150.00
    o_pos = Order(
        id=uuid.uuid4(),
        tenant_id=branch.tenant_id,
        branch_id=branch.id,
        order_type=OrderType.DINE_IN,
        order_source=OrderSource.CASHIER_POS,
        status=OrderStatus.PAID,
        is_paid=True,
        subtotal=Decimal("130.00"),
        tax_total=Decimal("20.00"),
        total_amount=Decimal("150.00"),
        created_at=now,
    )
    p_pos = Payment(
        id=uuid.uuid4(),
        tenant_id=branch.tenant_id,
        branch_id=branch.id,
        order_id=o_pos.id,
        payment_method=PaymentMethod.CASH,
        amount=Decimal("150.00"),
        status=PaymentStatus.COMPLETED,
        created_at=now,
    )

    # 3. Takeaway App Order (Card POS Terminal Payment): Total = 80.00
    o_takeaway = Order(
        id=uuid.uuid4(),
        tenant_id=branch.tenant_id,
        branch_id=branch.id,
        order_type=OrderType.TAKEAWAY,
        order_source=OrderSource.TAKE_A_WAY_APP,
        status=OrderStatus.PAID,
        is_paid=True,
        subtotal=Decimal("70.00"),
        tax_total=Decimal("10.00"),
        total_amount=Decimal("80.00"),
        created_at=now,
    )
    p_takeaway = Payment(
        id=uuid.uuid4(),
        tenant_id=branch.tenant_id,
        branch_id=branch.id,
        order_id=o_takeaway.id,
        payment_method=PaymentMethod.POS_TERMINAL,
        amount=Decimal("80.00"),
        status=PaymentStatus.COMPLETED,
        created_at=now,
    )

    test_session.add_all([o_qr, o_pos, o_takeaway, p_qr, p_pos, p_takeaway])
    await test_session.commit()

    # Generate Z-Report
    gen_res = await test_client.post(
        "/api/v1/financials/z-report/generate",
        json={},
        headers=headers,
    )
    assert gen_res.status_code == 201
    zr = gen_res.json()

    # Gross = 100 + 150 + 80 = 330.00
    assert Decimal(str(zr["gross_sales"])) == Decimal("330.00")
    # Net = 85 + 130 + 70 = 285.00
    assert Decimal(str(zr["net_sales"])) == Decimal("285.00")
    # Tax = 15 + 20 + 10 = 45.00
    assert Decimal(str(zr["total_tax"])) == Decimal("45.00")

    # Payment Channels
    assert Decimal(str(zr["payment_channels"]["online"])) == Decimal("100.00")
    assert Decimal(str(zr["payment_channels"]["cash"])) == Decimal("150.00")
    assert Decimal(str(zr["payment_channels"]["card_pos"])) == Decimal("80.00")

    # Order Sources
    assert Decimal(str(zr["order_sources"]["qr_customer"])) == Decimal("100.00")
    assert Decimal(str(zr["order_sources"]["cashier_pos"])) == Decimal("150.00")
    assert Decimal(str(zr["order_sources"]["takeaway_app"])) == Decimal("80.00")

    # Order Volumes
    assert zr["order_volumes"]["total_orders"] == 3
    assert zr["order_volumes"]["paid_orders"] == 3
    assert zr["order_volumes"]["cancelled_orders"] == 0


# ---------------------------------------------------------------------------
# Test 4: Refund & Deduction Handling
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_refund_deduction_handling(
    test_client: AsyncClient,
    test_session: AsyncSession,
    seed_financial_data: dict,
) -> None:
    """Verify refund decreases calculated cash in drawer and is recorded in Z-Report."""
    branch = seed_financial_data["branch_a"]
    cashier_token = seed_financial_data["cashier_token"]
    admin_token = seed_financial_data["admin_token"]

    # 1. Open drawer with 300.00
    await test_client.post(
        "/api/v1/financials/drawer/open",
        json={"opening_balance": "300.00"},
        headers={"Authorization": f"Bearer {cashier_token}", "X-Branch-ID": str(branch.id)},
    )

    now = datetime.now(timezone.utc)
    order = Order(
        id=uuid.uuid4(),
        tenant_id=branch.tenant_id,
        branch_id=branch.id,
        order_type=OrderType.TAKEAWAY,
        order_source=OrderSource.CASHIER_POS,
        status=OrderStatus.PAID,
        is_paid=True,
        total_amount=Decimal("50.00"),
        created_at=now,
    )
    test_session.add(order)
    await test_session.flush()

    # Cash payment 50.00
    pay = Payment(
        id=uuid.uuid4(),
        tenant_id=branch.tenant_id,
        branch_id=branch.id,
        order_id=order.id,
        payment_method=PaymentMethod.CASH,
        amount=Decimal("50.00"),
        status=PaymentStatus.COMPLETED,
        created_at=now,
    )
    # Cash refund 20.00
    refund = Payment(
        id=uuid.uuid4(),
        tenant_id=branch.tenant_id,
        branch_id=branch.id,
        order_id=order.id,
        payment_method=PaymentMethod.CASH,
        amount=Decimal("20.00"),
        status=PaymentStatus.REFUNDED,
        created_at=now,
    )
    test_session.add_all([pay, refund])
    await test_session.commit()

    # 2. Close drawer: expected = 300 (opening) + 50 (cash sale) - 20 (cash refund) = 330.00
    close_res = await test_client.post(
        "/api/v1/financials/drawer/close",
        json={"declared_cash_amount": "330.00"},
        headers={"Authorization": f"Bearer {cashier_token}", "X-Branch-ID": str(branch.id)},
    )
    assert close_res.status_code == 200
    drawer_res = close_res.json()
    assert Decimal(str(drawer_res["calculated_cash_amount"])) == Decimal("330.00")
    assert Decimal(str(drawer_res["cash_variance"])) == Decimal("0.00")

    # 3. Z-Report records refund amount
    zr_res = await test_client.post(
        "/api/v1/financials/z-report/generate",
        json={},
        headers={"Authorization": f"Bearer {admin_token}", "X-Branch-ID": str(branch.id)},
    )
    assert zr_res.status_code == 201
    assert Decimal(str(zr_res.json()["total_refunds"])) == Decimal("20.00")


# ---------------------------------------------------------------------------
# Test 5: Multi-Tenant Branch Isolation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_multi_tenant_branch_isolation(
    test_client: AsyncClient,
    test_session: AsyncSession,
    seed_financial_data: dict,
) -> None:
    """Ensure orders in Branch A never pollute Branch B calculations."""
    branch_a = seed_financial_data["branch_a"]
    branch_b = seed_financial_data["branch_b"]
    super_token = seed_financial_data["super_token"]

    now = datetime.now(timezone.utc)

    # Order in Branch A: 500.00
    order_a = Order(
        id=uuid.uuid4(),
        tenant_id=branch_a.tenant_id,
        branch_id=branch_a.id,
        order_type=OrderType.TAKEAWAY,
        order_source=OrderSource.CASHIER_POS,
        status=OrderStatus.PAID,
        is_paid=True,
        total_amount=Decimal("500.00"),
        created_at=now,
    )
    # Order in Branch B: 100.00
    order_b = Order(
        id=uuid.uuid4(),
        tenant_id=branch_b.tenant_id,
        branch_id=branch_b.id,
        order_type=OrderType.TAKEAWAY,
        order_source=OrderSource.CASHIER_POS,
        status=OrderStatus.PAID,
        is_paid=True,
        total_amount=Decimal("100.00"),
        created_at=now,
    )
    test_session.add_all([order_a, order_b])
    await test_session.commit()

    # Generate Z-Report for Branch B
    zr_b_res = await test_client.post(
        "/api/v1/financials/z-report/generate",
        json={},
        headers={"Authorization": f"Bearer {super_token}", "X-Branch-ID": str(branch_b.id)},
    )
    assert zr_b_res.status_code == 201
    zr_b = zr_b_res.json()
    assert Decimal(str(zr_b["gross_sales"])) == Decimal("100.00")
    assert zr_b["order_volumes"]["total_orders"] == 1


# ---------------------------------------------------------------------------
# Test 6: RBAC Guardrails
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rbac_guardrails(
    test_client: AsyncClient,
    seed_financial_data: dict,
) -> None:
    """Verify 401 for unauthenticated, 403 for unauthorized roles and cross-branch access."""
    branch_a = seed_financial_data["branch_a"]
    branch_b = seed_financial_data["branch_b"]
    cashier_token = seed_financial_data["cashier_token"]
    admin_token = seed_financial_data["admin_token"]
    cashier_b_token = seed_financial_data["cashier_b_token"]

    # 1. Unauthenticated request -> 401
    unauth_res = await test_client.post(
        "/api/v1/financials/drawer/open",
        json={"opening_balance": "100.00"},
        headers={"X-Branch-ID": str(branch_a.id)},
    )
    assert unauth_res.status_code == 401

    # 2. Cashier attempting to generate Z-Report -> 403 FORBIDDEN (Manager role required)
    cashier_gen_res = await test_client.post(
        "/api/v1/financials/z-report/generate",
        json={},
        headers={"Authorization": f"Bearer {cashier_token}", "X-Branch-ID": str(branch_a.id)},
    )
    assert cashier_gen_res.status_code == 403

    # 3. Cashier B (Branch B) attempting to access Branch A -> 403 FORBIDDEN
    cross_branch_res = await test_client.post(
        "/api/v1/financials/drawer/open",
        json={"opening_balance": "100.00"},
        headers={"Authorization": f"Bearer {cashier_b_token}", "X-Branch-ID": str(branch_a.id)},
    )
    assert cross_branch_res.status_code == 403
    assert "access to this branch" in cross_branch_res.json()["detail"].lower()

    # 4. Missing X-Branch-ID -> 400
    missing_hdr_res = await test_client.post(
        "/api/v1/financials/drawer/open",
        json={"opening_balance": "100.00"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert missing_hdr_res.status_code == 400

    # 5. Malformed X-Branch-ID -> 400
    invalid_hdr_res = await test_client.post(
        "/api/v1/financials/drawer/open",
        json={"opening_balance": "100.00"},
        headers={"Authorization": f"Bearer {admin_token}", "X-Branch-ID": "invalid-uuid"},
    )
    assert invalid_hdr_res.status_code == 400


# ---------------------------------------------------------------------------
# Test 7: Drawer Current 404 & Historical Z-Report Queries
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_drawer_current_not_found_and_z_report_queries(
    test_client: AsyncClient,
    seed_financial_data: dict,
) -> None:
    """Verify 404 when no drawer is open, and verify get_by_id and list_z_reports."""
    branch = seed_financial_data["branch_a"]
    admin_token = seed_financial_data["admin_token"]
    headers = {"Authorization": f"Bearer {admin_token}", "X-Branch-ID": str(branch.id)}

    # 1. No drawer is open -> 404
    cur_res = await test_client.get("/api/v1/financials/drawer/current", headers=headers)
    assert cur_res.status_code == 404

    # 2. Generate Z-Report
    gen_res = await test_client.post("/api/v1/financials/z-report/generate", json={}, headers=headers)
    assert gen_res.status_code == 201
    report_id = gen_res.json()["id"]

    # 3. Get Z-Report by ID -> 200
    get_res = await test_client.get(f"/api/v1/financials/z-report/{report_id}", headers=headers)
    assert get_res.status_code == 200
    assert get_res.json()["id"] == report_id

    # 4. List Z-Reports -> 200
    list_res = await test_client.get("/api/v1/financials/z-reports", headers=headers)
    assert list_res.status_code == 200
    list_data = list_res.json()
    assert list_data["total"] >= 1
    assert any(r["id"] == report_id for r in list_data["items"])

