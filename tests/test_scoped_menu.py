"""Automated test suite for Scoped Menu Engine: Hierarchical Tenancy and Branch Overrides."""

from __future__ import annotations

import uuid
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
from app.api.deps import get_async_db
from app.core.security import create_access_token, get_password_hash
from app.main import create_app
from app.models.auth import Base, Branch, Table, Tenant, User, UserBranchAccess
from app.models.brand import Brand
from app.models.catalog import Category, Item
from app.models.enums import MenuItemScope, UserRole
from app.models.menu import BranchMenuOverride
from app.schemas.menu import (
    BranchMenuOverrideUpdate,
    ScopedItemCreateRequest,
)
from app.services.menu_service import MenuService


@pytest_asyncio.fixture(scope="function")
async def async_test_engine() -> AsyncGenerator[AsyncEngine, None]:
    """In-memory SQLite async engine with all domain tables created."""
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
    app = create_app()
    app.dependency_overrides[get_async_db] = lambda: test_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest.mark.asyncio
async def test_brand_creation_with_multiple_branches(test_session: AsyncSession):
    """Test Brand hierarchy: Brand organization owning multiple branches with currencies and timezones."""
    tenant = Tenant(name="Burgers Global", slug=f"burgers-global-{uuid.uuid4().hex[:6]}")
    test_session.add(tenant)
    await test_session.flush()

    brand = Brand(
        name="Artisan Burger Co",
        slug=f"artisan-burger-{uuid.uuid4().hex[:6]}",
        is_active=True,
    )
    test_session.add(brand)
    await test_session.flush()

    branch_cairo = Branch(
        tenant_id=tenant.id,
        brand_id=brand.id,
        name={"en": "Cairo Branch", "ar": "فرع القاهرة"},
        slug=f"cairo-{uuid.uuid4().hex[:6]}",
        currency="EGP",
        timezone="Africa/Cairo",
        latitude=Decimal("30.0444"),
        longitude=Decimal("31.2357"),
    )
    branch_riyadh = Branch(
        tenant_id=tenant.id,
        brand_id=brand.id,
        name={"en": "Riyadh Branch", "ar": "فرع الرياض"},
        slug=f"riyadh-{uuid.uuid4().hex[:6]}",
        currency="SAR",
        timezone="Asia/Riyadh",
        latitude=Decimal("24.7136"),
        longitude=Decimal("46.6753"),
    )
    test_session.add_all([branch_cairo, branch_riyadh])
    await test_session.commit()

    # Query back and verify relationships
    await test_session.refresh(brand, attribute_names=["branches"])
    assert len(brand.branches) == 2
    branch_names = [b.currency for b in brand.branches]
    assert "EGP" in branch_names
    assert "SAR" in branch_names


@pytest.mark.asyncio
async def test_all_branches_catalog_inheritance(test_session: AsyncSession):
    """Test inheritance of ALL_BRANCHES catalog items across all brand branches."""
    tenant = Tenant(name="Global Food", slug=f"tenant-{uuid.uuid4().hex[:6]}")
    test_session.add(tenant)
    await test_session.flush()

    brand = Brand(name="Burger Hub", slug=f"burger-hub-{uuid.uuid4().hex[:6]}")
    test_session.add(brand)
    await test_session.flush()

    branch_1 = Branch(
        tenant_id=tenant.id,
        brand_id=brand.id,
        name={"en": "Branch 1"},
        slug=f"b1-{uuid.uuid4().hex[:6]}",
        latitude=Decimal("30.0"),
        longitude=Decimal("31.0"),
    )
    branch_2 = Branch(
        tenant_id=tenant.id,
        brand_id=brand.id,
        name={"en": "Branch 2"},
        slug=f"b2-{uuid.uuid4().hex[:6]}",
        latitude=Decimal("30.1"),
        longitude=Decimal("31.1"),
    )
    test_session.add_all([branch_1, branch_2])
    await test_session.flush()

    category = Category(
        branch_id=branch_1.id,
        name={"en": "Main Dishes", "ar": "الأطباق الرئيسية"},
        display_order=1,
    )
    test_session.add(category)
    await test_session.flush()

    # Create catalog item with ALL_BRANCHES scope
    item_all = Item(
        category_id=category.id,
        brand_id=brand.id,
        name={"en": "Classic Burger", "ar": "برجر كلاسيك"},
        base_price=Decimal("120.00"),
        scope=MenuItemScope.ALL_BRANCHES,
        is_active=True,
        is_available=True,
    )
    test_session.add(item_all)
    await test_session.commit()

    # Query effective menu for branch 1
    menu_1 = await MenuService.get_branch_menu(test_session, branch_1.id)
    assert len(menu_1.categories) == 1
    assert len(menu_1.categories[0].items) == 1
    resolved_1 = menu_1.categories[0].items[0]
    assert resolved_1.id == item_all.id
    assert resolved_1.final_price == Decimal("120.00")
    assert resolved_1.is_available is True
    assert resolved_1.has_override is False

    # Query effective menu for branch 2
    menu_2 = await MenuService.get_branch_menu(test_session, branch_2.id)
    assert len(menu_2.categories) == 1
    assert len(menu_2.categories[0].items) == 1
    resolved_2 = menu_2.categories[0].items[0]
    assert resolved_2.id == item_all.id
    assert resolved_2.final_price == Decimal("120.00")
    assert resolved_2.is_available is True
    assert resolved_2.has_override is False


@pytest.mark.asyncio
async def test_branch_price_and_availability_overrides(test_session: AsyncSession):
    """Test branch overrides: price_override and 86 out-of-stock reflect exclusively in the target branch."""
    tenant = Tenant(name="Diner Corp", slug=f"diner-{uuid.uuid4().hex[:6]}")
    test_session.add(tenant)
    await test_session.flush()

    brand = Brand(name="Pizza Express", slug=f"pizza-{uuid.uuid4().hex[:6]}")
    test_session.add(brand)
    await test_session.flush()

    branch_alex = Branch(
        tenant_id=tenant.id,
        brand_id=brand.id,
        name={"en": "Alexandria"},
        slug=f"alex-{uuid.uuid4().hex[:6]}",
        latitude=Decimal("31.2"),
        longitude=Decimal("29.9"),
    )
    branch_cairo = Branch(
        tenant_id=tenant.id,
        brand_id=brand.id,
        name={"en": "Cairo"},
        slug=f"cairo-{uuid.uuid4().hex[:6]}",
        latitude=Decimal("30.0"),
        longitude=Decimal("31.2"),
    )
    test_session.add_all([branch_alex, branch_cairo])
    await test_session.flush()

    cat = Category(branch_id=branch_alex.id, name={"en": "Pizzas"}, display_order=1)
    test_session.add(cat)
    await test_session.flush()

    item_margherita = Item(
        category_id=cat.id,
        brand_id=brand.id,
        name={"en": "Margherita"},
        base_price=Decimal("150.00"),
        scope=MenuItemScope.ALL_BRANCHES,
        is_active=True,
        is_available=True,
    )
    item_pepperoni = Item(
        category_id=cat.id,
        brand_id=brand.id,
        name={"en": "Pepperoni"},
        base_price=Decimal("180.00"),
        scope=MenuItemScope.ALL_BRANCHES,
        is_active=True,
        is_available=True,
    )
    test_session.add_all([item_margherita, item_pepperoni])
    await test_session.flush()

    # Alexandria branch sets overrides:
    # Margherita has a promotional price of 130.00
    # Pepperoni is out of stock (86'd)
    await MenuService.set_branch_override(
        test_session,
        branch_id=branch_alex.id,
        menu_item_id=item_margherita.id,
        price_override=Decimal("130.00"),
    )
    await MenuService.set_branch_override(
        test_session,
        branch_id=branch_alex.id,
        menu_item_id=item_pepperoni.id,
        is_available=False,
    )

    # 1. Verify Alexandria Branch Menu
    alex_menu = await MenuService.get_branch_menu(test_session, branch_alex.id)
    items_by_name = {it.name: it for it in alex_menu.categories[0].items}

    # Margherita in Alex: price overridden to 130, available
    assert items_by_name["Margherita"].base_price == Decimal("150.00")
    assert items_by_name["Margherita"].final_price == Decimal("130.00")
    assert items_by_name["Margherita"].has_override is True
    assert items_by_name["Margherita"].is_available is True

    # Pepperoni in Alex: base price 180, available False
    assert items_by_name["Pepperoni"].final_price == Decimal("180.00")
    assert items_by_name["Pepperoni"].is_available is False
    assert items_by_name["Pepperoni"].has_override is True

    # 2. Verify Cairo Branch Menu remains completely unaffected (Zero leakage)
    cairo_menu = await MenuService.get_branch_menu(test_session, branch_cairo.id)
    cairo_items = {it.name: it for it in cairo_menu.categories[0].items}

    # Margherita in Cairo: original price 150.00, no override
    assert cairo_items["Margherita"].final_price == Decimal("150.00")
    assert cairo_items["Margherita"].has_override is False
    assert cairo_items["Margherita"].is_available is True

    # Pepperoni in Cairo: original price 180.00, available True
    assert cairo_items["Pepperoni"].final_price == Decimal("180.00")
    assert cairo_items["Pepperoni"].has_override is False
    assert cairo_items["Pepperoni"].is_available is True


@pytest.mark.asyncio
async def test_specific_branches_isolation(test_session: AsyncSession):
    """Test SPECIFIC_BRANCHES isolation: Item is visible only in assigned branches."""
    tenant = Tenant(name="Retail Group", slug=f"retail-{uuid.uuid4().hex[:6]}")
    test_session.add(tenant)
    await test_session.flush()

    brand = Brand(name="Taco World", slug=f"taco-{uuid.uuid4().hex[:6]}")
    test_session.add(brand)
    await test_session.flush()

    branch_east = Branch(
        tenant_id=tenant.id,
        brand_id=brand.id,
        name={"en": "East Branch"},
        slug=f"east-{uuid.uuid4().hex[:6]}",
        latitude=Decimal("30.0"),
        longitude=Decimal("31.0"),
    )
    branch_west = Branch(
        tenant_id=tenant.id,
        brand_id=brand.id,
        name={"en": "West Branch"},
        slug=f"west-{uuid.uuid4().hex[:6]}",
        latitude=Decimal("30.0"),
        longitude=Decimal("31.1"),
    )
    test_session.add_all([branch_east, branch_west])
    await test_session.flush()

    cat = Category(branch_id=branch_east.id, name={"en": "Specials"}, display_order=1)
    test_session.add(cat)
    await test_session.flush()

    # Create catalog item assigned ONLY to branch_east
    create_req = ScopedItemCreateRequest(
        name={"en": "East Special Burrito"},
        base_price=Decimal("95.00"),
        category_id=cat.id,
        brand_id=brand.id,
        scope=MenuItemScope.SPECIFIC_BRANCHES,
        target_branch_ids=[branch_east.id],
    )
    item = await MenuService.create_catalog_item(test_session, create_req)
    assert item.scope == MenuItemScope.SPECIFIC_BRANCHES

    # Query East Branch: item must be present
    east_menu = await MenuService.get_branch_menu(test_session, branch_east.id)
    east_item_ids = [it.id for it in east_menu.categories[0].items]
    assert item.id in east_item_ids

    # Query West Branch: item must NOT be present
    west_menu = await MenuService.get_branch_menu(test_session, branch_west.id)
    west_items = west_menu.categories[0].items if west_menu.categories else []
    west_item_ids = [it.id for it in west_items]
    assert item.id not in west_item_ids


@pytest.mark.asyncio
async def test_branch_visibility_toggle(test_session: AsyncSession):
    """Test is_visible = False completely hides the item in the branch."""
    tenant = Tenant(name="Visibility Tenant", slug=f"vis-{uuid.uuid4().hex[:6]}")
    test_session.add(tenant)
    await test_session.flush()

    brand = Brand(name="Vis Brand", slug=f"vis-b-{uuid.uuid4().hex[:6]}")
    test_session.add(brand)
    await test_session.flush()

    branch = Branch(
        tenant_id=tenant.id,
        brand_id=brand.id,
        name={"en": "Branch"},
        slug=f"vis-branch-{uuid.uuid4().hex[:6]}",
        latitude=Decimal("30.0"),
        longitude=Decimal("31.0"),
    )
    test_session.add(branch)
    await test_session.flush()

    cat = Category(branch_id=branch.id, name={"en": "Drinks"}, display_order=1)
    test_session.add(cat)
    await test_session.flush()

    item = Item(
        category_id=cat.id,
        brand_id=brand.id,
        name={"en": "Soda"},
        base_price=Decimal("25.00"),
        scope=MenuItemScope.ALL_BRANCHES,
        is_active=True,
    )
    test_session.add(item)
    await test_session.commit()

    # Hide item in this branch
    await MenuService.set_branch_override(
        test_session,
        branch_id=branch.id,
        menu_item_id=item.id,
        is_visible=False,
    )

    menu = await MenuService.get_branch_menu(test_session, branch.id)
    items = menu.categories[0].items if menu.categories else []
    assert len(items) == 0


@pytest.mark.asyncio
async def test_scoped_menu_http_endpoints(
    test_client: AsyncClient,
    test_session: AsyncSession,
):
    """Test REST API endpoints for scoped menu retrieval and branch overrides."""
    tenant = Tenant(name="API Tenant", slug=f"api-{uuid.uuid4().hex[:6]}")
    test_session.add(tenant)
    await test_session.flush()

    brand = Brand(name="API Brand", slug=f"api-brand-{uuid.uuid4().hex[:6]}")
    test_session.add(brand)
    await test_session.flush()

    branch = Branch(
        tenant_id=tenant.id,
        brand_id=brand.id,
        name={"en": "API Branch"},
        slug=f"api-branch-{uuid.uuid4().hex[:6]}",
        currency="EGP",
        timezone="Africa/Cairo",
        latitude=Decimal("30.0"),
        longitude=Decimal("31.0"),
    )
    test_session.add(branch)
    await test_session.flush()

    cat = Category(branch_id=branch.id, name={"en": "Desserts"}, display_order=1)
    test_session.add(cat)
    await test_session.flush()

    item = Item(
        category_id=cat.id,
        brand_id=brand.id,
        name={"en": "Cheesecake"},
        base_price=Decimal("75.00"),
        scope=MenuItemScope.ALL_BRANCHES,
        is_active=True,
    )
    test_session.add(item)
    await test_session.commit()

    # 1. GET /api/v1/menu/branch/{branch_id}
    res = await test_client.get(f"/api/v1/menu/branch/{branch.id}")
    assert res.status_code == 200
    data = res.json()
    assert data["branch_id"] == str(branch.id)
    assert data["currency"] == "EGP"
    assert len(data["categories"]) == 1
    assert data["categories"][0]["items"][0]["final_price"] == "75.00"

    # 2. PATCH /api/v1/menu/branches/{branch_id}/items/{item_id}/override
    patch_res = await test_client.patch(
        f"/api/v1/menu/branches/{branch.id}/items/{item.id}/override",
        json={"price_override": "85.50", "is_available": False},
    )
    assert patch_res.status_code == 200
    override_data = patch_res.json()
    assert override_data["price_override"] == "85.50"
    assert override_data["is_available"] is False

    # 3. GET /api/v1/menu/branch/{branch_id} again to verify reflected override
    res_updated = await test_client.get(f"/api/v1/menu/branch/{branch.id}")
    assert res_updated.status_code == 200
    updated_data = res_updated.json()
    updated_item = updated_data["categories"][0]["items"][0]
    assert updated_item["final_price"] == "85.50"
    assert updated_item["is_available"] is False
    assert updated_item["has_override"] is True


@pytest.mark.asyncio
async def test_brand_admin_user_role_semantics(test_session: AsyncSession):
    """Test Brand Admin user role semantics (brand_id populated, branch_id = None)."""
    tenant = Tenant(name="Org Tenant", slug=f"org-{uuid.uuid4().hex[:6]}")
    test_session.add(tenant)
    await test_session.flush()

    brand = Brand(name="Super Brand", slug=f"sb-{uuid.uuid4().hex[:6]}")
    test_session.add(brand)
    await test_session.flush()

    branch = Branch(
        tenant_id=tenant.id,
        brand_id=brand.id,
        name={"en": "Branch 1"},
        slug=f"sb-1-{uuid.uuid4().hex[:6]}",
        latitude=Decimal("30.0"),
        longitude=Decimal("31.0"),
    )
    test_session.add(branch)
    await test_session.flush()

    # Brand Admin: brand_id populated, branch_id = None
    brand_admin = User(
        tenant_id=tenant.id,
        brand_id=brand.id,
        email="brandadmin@superbrand.com",
        hashed_password=get_password_hash("Secret123"),
        full_name="Brand Director",
        role=UserRole.BRAND_ADMIN,
        is_active=True,
    )
    # Branch Staff: both brand_id and branch_id assigned
    cashier = User(
        tenant_id=tenant.id,
        brand_id=brand.id,
        email="cashier@superbrand.com",
        hashed_password=get_password_hash("Secret123"),
        full_name="Front Cashier",
        role=UserRole.CASHIER,
        is_active=True,
    )
    test_session.add_all([brand_admin, cashier])
    await test_session.flush()

    # Assign branch to cashier via UserBranchAccess
    access = UserBranchAccess(user_id=cashier.id, branch_id=branch.id)
    test_session.add(access)
    await test_session.commit()

    await test_session.refresh(brand_admin)
    await test_session.refresh(cashier, attribute_names=["branch_access"])

    # Role semantics validation
    assert brand_admin.role == UserRole.BRAND_ADMIN
    assert brand_admin.brand_id == brand.id
    assert brand_admin.branch_id is None  # Cross-branch management

    assert cashier.role == UserRole.CASHIER
    assert cashier.brand_id == brand.id
    assert cashier.branch_id == branch.id  # Scoped to single branch
