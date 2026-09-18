"""End-to-End Integration Verification Test for Phase 4 Complete Engine.

Verifies complete operational harmony across:
Brand Hierarchy -> Scoped Overrides -> Cryptographic QR -> Cash Drawer -> Z-Reports -> Analytics Aggregator.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import AsyncGenerator

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
from app.models.auth import Base, Branch, Table, Tenant, User, UserBranchAccess
from app.models.brand import Brand
from app.models.catalog import Category, Item
from app.models.enums import (
    DrawerStatus,
    KitchenStation,
    MenuItemScope,
    OrderSource,
    OrderStatus,
    OrderType,
    PaymentMethod,
    PaymentStatus,
    TableStatus,
    UserRole,
)
from app.models.financials import CashDrawerSession, FinancialBase, ZReport
from app.models.order import Order, OrderItem, Payment
from app.models.table import TableSession
from app.schemas.financials import ZReportGenerateRequest
from app.schemas.menu import ScopedItemCreateRequest
from app.services.financial_service import FinancialService
from app.services.menu_service import MenuService


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
    app.dependency_overrides[get_async_db] = lambda: test_session
    app.dependency_overrides[get_db] = lambda: test_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest.mark.asyncio
async def test_e2e_full_lifecycle_pipeline(
    test_client: AsyncClient,
    test_session: AsyncSession,
):
    """Execute complete end-to-end integration lifecycle test.

    Pipeline:
    1. Setup Hierarchy (Brand + 2 Branches + Users)
    2. Catalog & Scoped Overrides (ALL_BRANCHES + SPECIFIC_BRANCHES + Overrides)
    3. Cryptographic QR Flow (Generation + Signature Validation + Tamper Resistance)
    4. Operations & Financial Closing (Cash Drawer + Orders + Z-Report)
    5. Executive Analytics Aggregation (GMV, AOV, Top Items, Branch Rankings)
    """
    now = datetime.now(timezone.utc)

    # -----------------------------------------------------------------------
    # Step 1: Setup Hierarchy
    # -----------------------------------------------------------------------
    tenant = Tenant(name="Alpha Corp", slug=f"alpha-corp-{uuid.uuid4().hex[:6]}")
    test_session.add(tenant)
    await test_session.flush()

    brand = Brand(name="Alpha Enterprise", slug=f"alpha-ent-{uuid.uuid4().hex[:6]}", is_active=True)
    test_session.add(brand)
    await test_session.flush()

    branch_1 = Branch(
        tenant_id=tenant.id,
        brand_id=brand.id,
        name={"en": "City Center", "ar": "وسط المدينة"},
        slug=f"city-center-{uuid.uuid4().hex[:6]}",
        currency="EGP",
        timezone="Africa/Cairo",
        latitude=Decimal("30.0444"),
        longitude=Decimal("31.2357"),
        is_active=True,
    )
    branch_2 = Branch(
        tenant_id=tenant.id,
        brand_id=brand.id,
        name={"en": "Airport", "ar": "المطار"},
        slug=f"airport-{uuid.uuid4().hex[:6]}",
        currency="EGP",
        timezone="Africa/Cairo",
        latitude=Decimal("30.1111"),
        longitude=Decimal("31.4000"),
        is_active=True,
    )
    test_session.add_all([branch_1, branch_2])
    await test_session.flush()

    # Users
    brand_admin = User(
        tenant_id=tenant.id,
        brand_id=brand.id,
        email="brandadmin@alpha.com",
        hashed_password=get_password_hash("Secret123"),
        full_name="Brand Director",
        role=UserRole.BRAND_ADMIN,
        is_active=True,
    )
    branch_1_admin = User(
        tenant_id=tenant.id,
        brand_id=brand.id,
        email="admin@citycenter.com",
        hashed_password=get_password_hash("Secret123"),
        full_name="City Center Admin",
        role=UserRole.BRANCH_ADMIN,
        is_active=True,
    )
    branch_2_cashier = User(
        tenant_id=tenant.id,
        brand_id=brand.id,
        email="cashier@airport.com",
        hashed_password=get_password_hash("Secret123"),
        full_name="Airport Cashier",
        role=UserRole.CASHIER,
        is_active=True,
    )
    test_session.add_all([brand_admin, branch_1_admin, branch_2_cashier])
    await test_session.flush()

    access_1 = UserBranchAccess(user_id=branch_1_admin.id, branch_id=branch_1.id)
    access_2 = UserBranchAccess(user_id=branch_2_cashier.id, branch_id=branch_2.id)
    test_session.add_all([access_1, access_2])
    await test_session.commit()

    # Tokens
    brand_admin_token = create_access_token({
        "sub": str(brand_admin.id),
        "tenant_id": str(tenant.id),
        "role": UserRole.BRAND_ADMIN.value,
    })

    # -----------------------------------------------------------------------
    # Step 2: Catalog & Scoped Overrides
    # -----------------------------------------------------------------------
    category = Category(
        branch_id=branch_2.id,
        name={"en": "Burgers & Combos", "ar": "برجر وكومبو"},
        display_order=1,
        is_active=True,
    )
    test_session.add(category)
    await test_session.flush()

    # Global item: Burger (base_price=100.00, ALL_BRANCHES)
    burger = Item(
        category_id=category.id,
        brand_id=brand.id,
        name={"en": "Burger", "ar": "برجر"},
        base_price=Decimal("100.00"),
        scope=MenuItemScope.ALL_BRANCHES,
        is_active=True,
        is_available=True,
    )
    test_session.add(burger)
    await test_session.flush()

    # Scoped item: Airport Combo (base_price=200.00, SPECIFIC_BRANCHES linked only to Branch 2)
    combo_req = ScopedItemCreateRequest(
        name={"en": "Airport Combo", "ar": "كومبو المطار"},
        base_price=Decimal("200.00"),
        category_id=category.id,
        brand_id=brand.id,
        scope=MenuItemScope.SPECIFIC_BRANCHES,
        target_branch_ids=[branch_2.id],
    )
    combo = await MenuService.create_catalog_item(test_session, combo_req)

    # Set overrides:
    # 1. At Branch 2: Burger price_override = 140.00
    await MenuService.set_branch_override(
        test_session,
        branch_id=branch_2.id,
        menu_item_id=burger.id,
        price_override=Decimal("140.00"),
    )
    # 2. At Branch 1: Burger is_available = False (86'd)
    await MenuService.set_branch_override(
        test_session,
        branch_id=branch_1.id,
        menu_item_id=burger.id,
        is_available=False,
    )

    # Verify Branch 2 Dynamic Menu (via HTTP endpoint)
    b2_res = await test_client.get(f"/api/v1/menu/branch/{branch_2.id}")
    assert b2_res.status_code == 200
    b2_menu = b2_res.json()
    b2_items = {it["id"]: it for it in b2_menu["categories"][0]["items"]}

    # Burger in Branch 2: overridden price 140.00, available
    assert str(burger.id) in b2_items
    assert b2_items[str(burger.id)]["final_price"] == "140.00"
    assert b2_items[str(burger.id)]["is_available"] is True
    assert b2_items[str(burger.id)]["has_override"] is True

    # Airport Combo in Branch 2: price 200.00, available
    assert str(combo.id) in b2_items
    assert b2_items[str(combo.id)]["final_price"] == "200.00"
    assert b2_items[str(combo.id)]["is_available"] is True

    # Verify Branch 1 Dynamic Menu (via HTTP endpoint)
    b1_res = await test_client.get(f"/api/v1/menu/branch/{branch_1.id}")
    assert b1_res.status_code == 200
    b1_menu = b1_res.json()
    b1_items = {it["id"]: it for it in b1_menu["categories"][0]["items"]}

    # Burger in Branch 1: base price 100.00, is_available False (86'd)
    assert str(burger.id) in b1_items
    assert b1_items[str(burger.id)]["final_price"] == "100.00"
    assert b1_items[str(burger.id)]["is_available"] is False

    # Airport Combo in Branch 1: must NOT be present
    assert str(combo.id) not in b1_items

    # -----------------------------------------------------------------------
    # Step 3: Cryptographic QR Flow
    # -----------------------------------------------------------------------
    table_1 = Table(
        branch_id=branch_2.id,
        brand_id=brand.id,
        table_number="T1",
        capacity=4,
        status=TableStatus.AVAILABLE,
        is_active=True,
    )
    test_session.add(table_1)
    await test_session.commit()

    # Request signed URL metadata as Brand Admin
    qr_url_res = await test_client.get(
        f"/api/v1/qr-export/tables/{table_1.id}/url",
        headers={
            "Authorization": f"Bearer {brand_admin_token}",
            "X-Branch-ID": str(branch_2.id),
        },
    )
    assert qr_url_res.status_code == 200
    qr_data = qr_url_res.json()
    sig = qr_data["signature"]
    ts = qr_data["timestamp"]
    assert sig is not None
    assert ts is not None

    # Probe validation endpoint with valid signature -> 200 OK
    verify_valid_res = await test_client.get(
        f"/api/v1/qr-export/verify?table_id={table_1.id}&branch_id={branch_2.id}&ts={ts}&sig={sig}"
    )
    assert verify_valid_res.status_code == 200
    assert verify_valid_res.json()["valid"] is True

    # Probe validation endpoint with tampered signature -> 403 Forbidden
    verify_tampered_res = await test_client.get(
        f"/api/v1/qr-export/verify?table_id={table_1.id}&branch_id={branch_2.id}&ts={ts}&sig=deadbeef0011223344"
    )
    assert verify_tampered_res.status_code == 403
    assert "Invalid or tampered" in verify_tampered_res.json()["detail"]

    # -----------------------------------------------------------------------
    # Step 4: Operations & Financial Closing
    # -----------------------------------------------------------------------
    # 1. Open Cash Drawer at Branch 2 with balance 500.00
    drawer = await FinancialService.open_cash_drawer(
        branch_id=branch_2.id,
        user_id=branch_2_cashier.id,
        opening_balance=Decimal("500.00"),
        db=test_session,
    )
    assert drawer.status == DrawerStatus.OPEN
    assert drawer.opening_balance == Decimal("500.00")

    # 2. Place & Pay Order 1: 1x Burger @ 140 Cash
    order_time = datetime.now(timezone.utc)
    order_1 = Order(
        tenant_id=tenant.id,
        brand_id=brand.id,
        branch_id=branch_2.id,
        table_id=table_1.id,
        order_type=OrderType.DINE_IN,
        order_source=OrderSource.CASHIER_POS,
        status=OrderStatus.PAID,
        is_paid=True,
        subtotal=Decimal("140.00"),
        tax_total=Decimal("0.00"),
        service_fee_total=Decimal("0.00"),
        total_amount=Decimal("140.00"),
        created_at=order_time,
    )
    test_session.add(order_1)
    await test_session.flush()

    order_item_1 = OrderItem(
        order_id=order_1.id,
        item_id=burger.id,
        quantity=1,
        unit_price=Decimal("140.00"),
        subtotal=Decimal("140.00"),
        station=KitchenStation.HOT_KITCHEN,
    )
    payment_1 = Payment(
        tenant_id=tenant.id,
        brand_id=brand.id,
        branch_id=branch_2.id,
        order_id=order_1.id,
        payment_method=PaymentMethod.CASH,
        amount=Decimal("140.00"),
        currency="EGP",
        status=PaymentStatus.COMPLETED,
        created_at=order_time,
    )
    test_session.add_all([order_item_1, payment_1])
    await test_session.flush()

    # 3. Place & Pay Order 2: 1x Airport Combo @ 200 Card (POS_TERMINAL)
    order_2 = Order(
        tenant_id=tenant.id,
        brand_id=brand.id,
        branch_id=branch_2.id,
        table_id=table_1.id,
        order_type=OrderType.DINE_IN,
        order_source=OrderSource.CASHIER_POS,
        status=OrderStatus.PAID,
        is_paid=True,
        subtotal=Decimal("200.00"),
        tax_total=Decimal("0.00"),
        service_fee_total=Decimal("0.00"),
        total_amount=Decimal("200.00"),
        created_at=order_time,
    )
    test_session.add(order_2)
    await test_session.flush()

    order_item_2 = OrderItem(
        order_id=order_2.id,
        item_id=combo.id,
        quantity=1,
        unit_price=Decimal("200.00"),
        subtotal=Decimal("200.00"),
        station=KitchenStation.HOT_KITCHEN,
    )
    payment_2 = Payment(
        tenant_id=tenant.id,
        brand_id=brand.id,
        branch_id=branch_2.id,
        order_id=order_2.id,
        payment_method=PaymentMethod.POS_TERMINAL,
        amount=Decimal("200.00"),
        currency="EGP",
        status=PaymentStatus.COMPLETED,
        created_at=order_time,
    )


    test_session.add_all([order_item_2, payment_2])
    await test_session.commit()

    # 4. Close Cash Drawer declaring 640.00 (500 opening + 140 cash sales)
    closed_drawer = await FinancialService.close_cash_drawer(
        branch_id=branch_2.id,
        user_id=branch_2_cashier.id,
        declared_cash_amount=Decimal("640.00"),
        closing_notes="E2E Drawer closed balanced",
        db=test_session,
    )
    assert closed_drawer.status == DrawerStatus.CLOSED
    assert closed_drawer.calculated_cash_amount == Decimal("640.00")
    assert closed_drawer.cash_variance == Decimal("0.00")

    # 5. Generate Z-Report for Branch 2
    z_req = ZReportGenerateRequest(drawer_session_id=closed_drawer.id)
    z_report = await FinancialService.generate_z_report(
        branch_id=branch_2.id,
        user_id=branch_2_cashier.id,
        request=z_req,
        db=test_session,
    )
    assert z_report.gross_sales == Decimal("340.00")
    assert z_report.net_sales == Decimal("340.00")
    assert z_report.payment_channels.cash == Decimal("140.00")
    assert z_report.payment_channels.card_pos == Decimal("200.00")
    assert z_report.order_volumes.paid_orders == 2
    assert z_report.cash_reconciliation.cash_variance == Decimal("0.00")

    # -----------------------------------------------------------------------
    # Step 5: Executive Analytics Aggregation
    # -----------------------------------------------------------------------
    dash_res = await test_client.get(
        "/api/v1/analytics/dashboard?period=today&force_refresh=true",
        headers={"Authorization": f"Bearer {brand_admin_token}"},
    )
    assert dash_res.status_code == 200
    dash = dash_res.json()

    # Assert system GMV and financial KPIs
    kpis = dash["kpis"]
    assert Decimal(str(kpis["gmv"])) == Decimal("340.00")
    assert Decimal(str(kpis["net_revenue"])) == Decimal("340.00")
    assert kpis["paid_orders"] == 2
    assert Decimal(str(kpis["aov"])) == Decimal("170.00")  # 340.00 / 2 orders

    # Assert top_selling_items contains both items with correct revenues
    def _extract_name(name_val):
        if isinstance(name_val, dict):
            return name_val.get("en") or next(iter(name_val.values()))
        return name_val

    top_items = {_extract_name(it["item_name"]): it for it in dash["top_selling_items"]}
    assert "Airport Combo" in top_items
    assert Decimal(str(top_items["Airport Combo"]["gross_revenue"])) == Decimal("200.00")
    assert top_items["Airport Combo"]["total_quantity_sold"] == 1

    assert "Burger" in top_items
    assert Decimal(str(top_items["Burger"]["gross_revenue"])) == Decimal("140.00")
    assert top_items["Burger"]["total_quantity_sold"] == 1

    # Assert branches-matrix ranks Branch 2 at top (GMV 340.00) and Branch 1 with 0.00
    matrix_res = await test_client.get(
        "/api/v1/analytics/branches-matrix?period=today",
        headers={"Authorization": f"Bearer {brand_admin_token}"},
    )
    assert matrix_res.status_code == 200
    matrix = matrix_res.json()
    ranked_branches = matrix["branches"]
    assert len(ranked_branches) >= 2

    # Top ranked branch must be Branch 2 ("Airport") with GMV 340.00
    assert ranked_branches[0]["branch_id"] == str(branch_2.id)
    assert Decimal(str(ranked_branches[0]["gmv"])) == Decimal("340.00")
    assert ranked_branches[0]["total_paid_orders"] == 2

    # Branch 1 ("City Center") with GMV 0.00
    b1_rank = next(b for b in ranked_branches if b["branch_id"] == str(branch_1.id))
    assert Decimal(str(b1_rank["gmv"])) == Decimal("0.00")
    assert b1_rank["total_paid_orders"] == 0
