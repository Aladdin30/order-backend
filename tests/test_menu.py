"""Comprehensive Test Suite for Task BE-2.2: Localized Menu Retrieval & Modifier Engine.

Tests:
1. Hierarchical catalog tree retrieval (Category -> Item -> Modifier Group -> Option).
2. Category display ordering and deterministic item sorting.
3. Item 86 kill-switch preservation (is_available=false included in tree).
4. Zero-overhead dynamic i18n localization (ar vs en, Content-Language header).
5. Complex modifier validation:
   - Item existence and availability checks (400 ITEM_UNAVAILABLE).
   - Mandatory groups and radio groups enforcement.
   - Min / Max choices selection limits.
   - Foreign / mismatched modifier option rejection.
   - Unavailable modifier option rejection (400 MODIFIER_OPTION_UNAVAILABLE).
   - Duplicate option & group prevention.
6. Authoritative pricing calculation (Unit Price = base + deltas, Subtotal = Unit Price * qty).
7. Shared table cart attribution and canonical table binding.
"""

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

from app.api.deps import get_async_db
from app.core.session_security import create_guest_session_jwt
from app.main import create_app
from app.models.auth import Base, Branch, Table, Tenant
from app.models.catalog import Category, Item, ModifierGroup, ModifierOption
from app.models.enums import KitchenStation, TableStatus


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
async def seed_menu_data(test_session: AsyncSession) -> dict:
    """Seed comprehensive catalog hierarchy with active and 86'd items and modifiers."""
    tenant = Tenant(name="Sultan Dining", slug="sultan-dining", is_active=True)
    test_session.add(tenant)
    await test_session.flush()

    branch = Branch(
        tenant_id=tenant.id,
        name={"en": "Downtown Branch", "ar": "فرع وسط المدينة"},
        slug="downtown",
        latitude=24.7136,
        longitude=46.6753,
        geofence_radius_meters=150,
        is_active=True,
    )
    other_branch = Branch(
        tenant_id=tenant.id,
        name={"en": "North Branch", "ar": "فرع الشمال"},
        slug="north",
        latitude=24.8000,
        longitude=46.6000,
        geofence_radius_meters=150,
        is_active=True,
    )
    test_session.add_all([branch, other_branch])
    await test_session.flush()

    table = Table(
        branch_id=branch.id,
        table_number="T-01",
        capacity=4,
        status=TableStatus.BROWSING,
        is_active=True,
    )
    test_session.add(table)
    await test_session.flush()

    # Category 1: Main Dishes (display_order = 1)
    cat_mains = Category(
        branch_id=branch.id,
        name={"en": "Mains", "ar": "الأطباق الرئيسية"},
        display_order=1,
        is_active=True,
    )
    # Category 2: Appetizers (display_order = 0 -> should appear first)
    cat_apps = Category(
        branch_id=branch.id,
        name={"en": "Appetizers", "ar": "المقبلات"},
        display_order=0,
        is_active=True,
    )
    # Category 3: Inactive Category (should not appear in tree)
    cat_inactive = Category(
        branch_id=branch.id,
        name={"en": "Seasonal", "ar": "موسمي"},
        display_order=2,
        is_active=False,
    )
    # Category for other branch
    cat_other = Category(
        branch_id=other_branch.id,
        name={"en": "Other Branch Mains", "ar": "أطباق الفرع الآخر"},
        display_order=0,
        is_active=True,
    )
    test_session.add_all([cat_mains, cat_apps, cat_inactive, cat_other])
    await test_session.flush()

    # Item 1: Burger in Mains (Available, with description and modifiers)
    item_burger = Item(
        category_id=cat_mains.id,
        name={"en": "Truffle Burger", "ar": "برجر الكمأة"},
        description={"en": "Prime Angus beef with black truffle aioli", "ar": "لحم أنجوس ممتاز مع صلصة الكمأة السوداء"},
        base_price=Decimal("45.00"),
        station=KitchenStation.HOT_KITCHEN,
        image_url="https://cdn.example.com/burger.jpg",
        is_available=True,
        allergens=["gluten", "dairy"],
        dietary_badges=["halal"],
    )
    # Item 2: Sold out item (is_available = False, Item 86 switch)
    item_sold_out = Item(
        category_id=cat_mains.id,
        name={"en": "Ribeye Steak", "ar": "ستيك ريب آي"},
        description=None,
        base_price=Decimal("95.00"),
        station=KitchenStation.HOT_KITCHEN,
        image_url=None,
        is_available=False,
        allergens=["dairy"],
        dietary_badges=["halal"],
    )
    # Item 3: Appetizer (no description)
    item_fries = Item(
        category_id=cat_apps.id,
        name={"en": "Crispy Fries", "ar": "بطاطس مقرمشة"},
        description=None,
        base_price=Decimal("15.00"),
        station=KitchenStation.HOT_KITCHEN,
        image_url=None,
        is_available=True,
        allergens=[],
        dietary_badges=["vegan"],
    )
    # Item 4: Belongs to other branch
    item_other = Item(
        category_id=cat_other.id,
        name={"en": "Foreign Item", "ar": "عنصر أجنبي"},
        description=None,
        base_price=Decimal("30.00"),
        station=KitchenStation.COLD_KITCHEN,
        image_url=None,
        is_available=True,
        allergens=[],
        dietary_badges=[],
    )
    test_session.add_all([item_burger, item_sold_out, item_fries, item_other])
    await test_session.flush()

    # Modifier Group 1 for Burger: Patty Doneness (Required Radio Group: min 1, max 1)
    group_doneness = ModifierGroup(
        item_id=item_burger.id,
        name={"en": "Doneness", "ar": "درجة الاستواء"},
        min_choices=1,
        max_choices=1,
        is_required=True,
    )
    # Modifier Group 2 for Burger: Extra Toppings (Optional Multi-choice: min 0, max 3)
    group_toppings = ModifierGroup(
        item_id=item_burger.id,
        name={"en": "Extra Toppings", "ar": "إضافات اختيارية"},
        min_choices=0,
        max_choices=3,
        is_required=False,
    )
    test_session.add_all([group_doneness, group_toppings])
    await test_session.flush()

    # Options for Doneness
    opt_medium = ModifierOption(
        modifier_group_id=group_doneness.id,
        name={"en": "Medium", "ar": "متوسط"},
        price_delta=Decimal("0.00"),
        is_available=True,
    )
    opt_well_done = ModifierOption(
        modifier_group_id=group_doneness.id,
        name={"en": "Well Done", "ar": "مطبوخ جيداً"},
        price_delta=Decimal("0.00"),
        is_available=True,
    )
    # Options for Toppings
    opt_cheese = ModifierOption(
        modifier_group_id=group_toppings.id,
        name={"en": "Aged Cheddar", "ar": "جبنة شيدر معتقة"},
        price_delta=Decimal("5.00"),
        is_available=True,
    )
    opt_bacon = ModifierOption(
        modifier_group_id=group_toppings.id,
        name={"en": "Beef Bacon", "ar": "بيكون بقري"},
        price_delta=Decimal("7.00"),
        is_available=True,
    )
    opt_truffle_sold_out = ModifierOption(
        modifier_group_id=group_toppings.id,
        name={"en": "Extra Truffle", "ar": "كمأة إضافية"},
        price_delta=Decimal("12.00"),
        is_available=False,  # 86 switch flag
    )
    test_session.add_all([opt_medium, opt_well_done, opt_cheese, opt_bacon, opt_truffle_sold_out])
    await test_session.commit()

    # Create guest JWT token
    guest_token = create_guest_session_jwt(
        session_id=uuid.uuid4(),
        tenant_id=tenant.id,
        branch_id=branch.id,
        table_id=table.id,
        table_number=table.table_number,
        is_presence_verified=True,
    )

    return {
        "tenant": tenant,
        "branch": branch,
        "other_branch": other_branch,
        "table": table,
        "guest_token": guest_token,
        "cat_mains": cat_mains,
        "cat_apps": cat_apps,
        "cat_inactive": cat_inactive,
        "item_burger": item_burger,
        "item_sold_out": item_sold_out,
        "item_fries": item_fries,
        "item_other": item_other,
        "group_doneness": group_doneness,
        "group_toppings": group_toppings,
        "opt_medium": opt_medium,
        "opt_well_done": opt_well_done,
        "opt_cheese": opt_cheese,
        "opt_bacon": opt_bacon,
        "opt_truffle_sold_out": opt_truffle_sold_out,
    }


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

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Tests: Catalog Tree Retrieval (GET /api/v1/menu/tree)
# ---------------------------------------------------------------------------

class TestMenuCatalogTree:
    """Verification of GET /api/v1/menu/tree endpoint."""

    @pytest.mark.asyncio
    async def test_get_menu_tree_hierarchical_structure(
        self,
        test_client: AsyncClient,
        seed_menu_data: dict,
    ):
        """Verify full hierarchical tree retrieval matching Category -> Item -> Group -> Option."""
        token = seed_menu_data["guest_token"]
        headers = {"Authorization": f"Bearer {token}", "Accept-Language": "en"}

        response = await test_client.get("/api/v1/menu/tree", headers=headers)
        assert response.status_code == 200
        data = response.json()

        assert data["branch_id"] == str(seed_menu_data["branch"].id)
        categories = data["categories"]
        # Inactive category must be excluded; only cat_apps and cat_mains
        assert len(categories) == 2

        # Verify ordering: display_order 0 (Appetizers) first, then display_order 1 (Mains)
        assert categories[0]["name"] == "Appetizers"
        assert categories[0]["display_order"] == 0
        assert categories[1]["name"] == "Mains"
        assert categories[1]["display_order"] == 1

        # Inspect Mains items
        mains_items = categories[1]["items"]
        assert len(mains_items) == 2

        burger = next(i for i in mains_items if i["id"] == str(seed_menu_data["item_burger"].id))
        assert burger["name"] == "Truffle Burger"
        assert burger["description"] == "Prime Angus beef with black truffle aioli"
        assert float(burger["base_price"]) == 45.00
        assert burger["is_available"] is True
        assert len(burger["modifier_groups"]) == 2

        doneness_group = next(g for g in burger["modifier_groups"] if g["id"] == str(seed_menu_data["group_doneness"].id))
        assert doneness_group["name"] == "Doneness"
        assert doneness_group["min_choices"] == 1
        assert doneness_group["max_choices"] == 1
        assert doneness_group["is_required"] is True
        assert len(doneness_group["options"]) == 2

    @pytest.mark.asyncio
    async def test_get_menu_tree_86_kill_switch_preservation(
        self,
        test_client: AsyncClient,
        seed_menu_data: dict,
    ):
        """Ensure items and options marked is_available=False are preserved with is_available: false."""
        token = seed_menu_data["guest_token"]
        headers = {"Authorization": f"Bearer {token}"}

        response = await test_client.get("/api/v1/menu/tree", headers=headers)
        assert response.status_code == 200
        data = response.json()

        mains = next(c for c in data["categories"] if c["id"] == str(seed_menu_data["cat_mains"].id))

        # Check sold out item is present and marked unavailable
        sold_out_item = next(i for i in mains["items"] if i["id"] == str(seed_menu_data["item_sold_out"].id))
        assert sold_out_item["is_available"] is False

        # Check sold out modifier option is present and marked unavailable
        burger = next(i for i in mains["items"] if i["id"] == str(seed_menu_data["item_burger"].id))
        toppings = next(g for g in burger["modifier_groups"] if g["id"] == str(seed_menu_data["group_toppings"].id))
        truffle_opt = next(o for o in toppings["options"] if o["id"] == str(seed_menu_data["opt_truffle_sold_out"].id))
        assert truffle_opt["is_available"] is False

    @pytest.mark.asyncio
    async def test_get_menu_tree_zero_overhead_i18n(
        self,
        test_client: AsyncClient,
        seed_menu_data: dict,
    ):
        """Verify dynamic localization via Accept-Language header (ar vs en)."""
        token = seed_menu_data["guest_token"]

        # 1. Test Arabic localization
        ar_response = await test_client.get(
            "/api/v1/menu/tree",
            headers={"Authorization": f"Bearer {token}", "Accept-Language": "ar"},
        )
        assert ar_response.status_code == 200
        assert ar_response.headers["content-language"] == "ar"
        ar_data = ar_response.json()
        ar_mains = next(c for c in ar_data["categories"] if c["id"] == str(seed_menu_data["cat_mains"].id))
        assert ar_mains["name"] == "الأطباق الرئيسية"
        ar_burger = next(i for i in ar_mains["items"] if i["id"] == str(seed_menu_data["item_burger"].id))
        assert ar_burger["name"] == "برجر الكمأة"
        assert ar_burger["description"] == "لحم أنجوس ممتاز مع صلصة الكمأة السوداء"

        # 2. Test English localization
        en_response = await test_client.get(
            "/api/v1/menu/tree",
            headers={"Authorization": f"Bearer {token}", "Accept-Language": "en"},
        )
        assert en_response.status_code == 200
        assert en_response.headers["content-language"] == "en"
        en_data = en_response.json()
        en_mains = next(c for c in en_data["categories"] if c["id"] == str(seed_menu_data["cat_mains"].id))
        assert en_mains["name"] == "Mains"
        en_burger = next(i for i in en_mains["items"] if i["id"] == str(seed_menu_data["item_burger"].id))
        assert en_burger["name"] == "Truffle Burger"
        assert en_burger["description"] == "Prime Angus beef with black truffle aioli"

    @pytest.mark.asyncio
    async def test_get_menu_tree_fallback_query_param(
        self,
        test_client: AsyncClient,
        seed_menu_data: dict,
    ):
        """Verify fallback branch_id query parameter when bearer token is omitted."""
        branch_id = seed_menu_data["branch"].id
        response = await test_client.get(f"/api/v1/menu/tree?branch_id={branch_id}")
        assert response.status_code == 200
        data = response.json()
        assert data["branch_id"] == str(branch_id)

    @pytest.mark.asyncio
    async def test_get_menu_tree_unauthorized_missing_credentials(
        self,
        test_client: AsyncClient,
    ):
        """Verify 401 when no token and no branch_id query parameter is provided."""
        response = await test_client.get("/api/v1/menu/tree")
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# Tests: Modifier Engine & Server-Side Price Calculation
# ---------------------------------------------------------------------------

class TestModifierValidationAndPricing:
    """Verification of POST /api/v1/menu/validate-item-selection endpoint."""

    @pytest.mark.asyncio
    async def test_validate_selection_authoritative_pricing_success(
        self,
        test_client: AsyncClient,
        seed_menu_data: dict,
    ):
        """Verify valid selection computes authoritative unit_price and subtotal."""
        token = seed_menu_data["guest_token"]
        headers = {"Authorization": f"Bearer {token}", "Accept-Language": "en"}

        payload = {
            "item_id": str(seed_menu_data["item_burger"].id),
            "quantity": 2,
            "selected_groups": [
                {
                    "group_id": str(seed_menu_data["group_doneness"].id),
                    "option_ids": [str(seed_menu_data["opt_medium"].id)],
                },
                {
                    "group_id": str(seed_menu_data["group_toppings"].id),
                    "option_ids": [
                        str(seed_menu_data["opt_cheese"].id),
                        str(seed_menu_data["opt_bacon"].id),
                    ],
                },
            ],
            "client_session_id": "client-device-1234",
            "guest_label": "Guest 1",
            "special_instructions": "No onions please",
        }

        response = await test_client.post(
            "/api/v1/menu/validate-item-selection",
            json=payload,
            headers=headers,
        )
        assert response.status_code == 200
        data = response.json()

        # Calculation check:
        # Base: 45.00
        # Doneness Medium: +0.00
        # Toppings Cheese: +5.00, Bacon: +7.00
        # Unit price = 45.00 + 0 + 5 + 7 = 57.00
        # Subtotal = 57.00 * 2 = 114.00
        assert float(data["base_price"]) == 45.00
        assert float(data["unit_price"]) == 57.00
        assert float(data["subtotal"]) == 114.00
        assert data["quantity"] == 2
        assert data["item_name"] == "Truffle Burger"
        assert data["table_id"] == str(seed_menu_data["table"].id)
        assert data["client_session_id"] == "client-device-1234"
        assert data["guest_label"] == "Guest 1"
        assert data["special_instructions"] == "No onions please"

        # Check snapshots format
        modifiers = data["selected_modifiers"]
        assert len(modifiers) == 3
        cheese_snap = next(m for m in modifiers if m["option_id"] == str(seed_menu_data["opt_cheese"].id))
        assert cheese_snap["group_name"] == "Extra Toppings"
        assert cheese_snap["name"] == "Aged Cheddar"
        assert float(cheese_snap["price_delta"]) == 5.00

    @pytest.mark.asyncio
    async def test_validate_selection_item_unavailable_400(
        self,
        test_client: AsyncClient,
        seed_menu_data: dict,
    ):
        """Verify item with is_available=False is rejected with 400 ITEM_UNAVAILABLE."""
        token = seed_menu_data["guest_token"]
        headers = {"Authorization": f"Bearer {token}", "Accept-Language": "en"}

        payload = {
            "item_id": str(seed_menu_data["item_sold_out"].id),
            "quantity": 1,
            "selected_groups": [],
        }

        response = await test_client.post(
            "/api/v1/menu/validate-item-selection",
            json=payload,
            headers=headers,
        )
        assert response.status_code == 400
        assert response.json()["detail"] == "The requested item is currently unavailable"

    @pytest.mark.asyncio
    async def test_validate_selection_item_not_found_404(
        self,
        test_client: AsyncClient,
        seed_menu_data: dict,
    ):
        """Verify non-existent item or item from another branch returns 404 ITEM_NOT_FOUND."""
        token = seed_menu_data["guest_token"]
        headers = {"Authorization": f"Bearer {token}", "Accept-Language": "en"}

        # 1. Random non-existent UUID
        response = await test_client.post(
            "/api/v1/menu/validate-item-selection",
            json={"item_id": str(uuid.uuid4()), "quantity": 1},
            headers=headers,
        )
        assert response.status_code == 404
        assert response.json()["detail"] == "The requested menu item was not found"

        # 2. Item belonging to other branch
        response_other = await test_client.post(
            "/api/v1/menu/validate-item-selection",
            json={"item_id": str(seed_menu_data["item_other"].id), "quantity": 1},
            headers=headers,
        )
        assert response_other.status_code == 404

    @pytest.mark.asyncio
    async def test_validate_selection_missing_mandatory_group_400(
        self,
        test_client: AsyncClient,
        seed_menu_data: dict,
    ):
        """Verify omitting a required modifier group returns 400 MODIFIER_GROUP_REQUIRED."""
        token = seed_menu_data["guest_token"]
        headers = {"Authorization": f"Bearer {token}", "Accept-Language": "en"}

        # Burger requires doneness group, but only toppings group is provided
        payload = {
            "item_id": str(seed_menu_data["item_burger"].id),
            "quantity": 1,
            "selected_groups": [
                {
                    "group_id": str(seed_menu_data["group_toppings"].id),
                    "option_ids": [str(seed_menu_data["opt_cheese"].id)],
                }
            ],
        }

        response = await test_client.post(
            "/api/v1/menu/validate-item-selection",
            json=payload,
            headers=headers,
        )
        assert response.status_code == 400
        assert "modifier group is required" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_validate_selection_radio_group_boundary_enforced(
        self,
        test_client: AsyncClient,
        seed_menu_data: dict,
    ):
        """Verify radio group with min_choices=1, max_choices=1 rejects selecting multiple options."""
        token = seed_menu_data["guest_token"]
        headers = {"Authorization": f"Bearer {token}", "Accept-Language": "en"}

        # Selecting 2 options for doneness (min=1, max=1)
        payload = {
            "item_id": str(seed_menu_data["item_burger"].id),
            "quantity": 1,
            "selected_groups": [
                {
                    "group_id": str(seed_menu_data["group_doneness"].id),
                    "option_ids": [
                        str(seed_menu_data["opt_medium"].id),
                        str(seed_menu_data["opt_well_done"].id),
                    ],
                }
            ],
        }

        response = await test_client.post(
            "/api/v1/menu/validate-item-selection",
            json=payload,
            headers=headers,
        )
        assert response.status_code == 400
        assert "out of the allowed bounds" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_validate_selection_max_choices_exceeded_400(
        self,
        test_client: AsyncClient,
        seed_menu_data: dict,
    ):
        """Verify exceeding max_choices boundary returns 400 MODIFIER_SELECTION_OUT_OF_BOUNDS."""
        token = seed_menu_data["guest_token"]
        headers = {"Authorization": f"Bearer {token}", "Accept-Language": "en"}

        # Toppings max_choices is 3, submitting 4 (even if one is invalid/fake option)
        payload = {
            "item_id": str(seed_menu_data["item_burger"].id),
            "quantity": 1,
            "selected_groups": [
                {
                    "group_id": str(seed_menu_data["group_doneness"].id),
                    "option_ids": [str(seed_menu_data["opt_medium"].id)],
                },
                {
                    "group_id": str(seed_menu_data["group_toppings"].id),
                    "option_ids": [
                        str(seed_menu_data["opt_cheese"].id),
                        str(seed_menu_data["opt_bacon"].id),
                        str(uuid.uuid4()),
                        str(uuid.uuid4()),
                    ],
                },
            ],
        }

        response = await test_client.post(
            "/api/v1/menu/validate-item-selection",
            json=payload,
            headers=headers,
        )
        assert response.status_code == 400
        assert "out of the allowed bounds" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_validate_selection_foreign_option_rejected_400(
        self,
        test_client: AsyncClient,
        seed_menu_data: dict,
    ):
        """Verify submitting an option that doesn't belong to the group returns 400 MODIFIER_OPTION_INVALID."""
        token = seed_menu_data["guest_token"]
        headers = {"Authorization": f"Bearer {token}", "Accept-Language": "en"}

        # Doneness option submitted under toppings group
        payload = {
            "item_id": str(seed_menu_data["item_burger"].id),
            "quantity": 1,
            "selected_groups": [
                {
                    "group_id": str(seed_menu_data["group_doneness"].id),
                    "option_ids": [str(seed_menu_data["opt_medium"].id)],
                },
                {
                    "group_id": str(seed_menu_data["group_toppings"].id),
                    "option_ids": [str(seed_menu_data["opt_well_done"].id)],
                },
            ],
        }

        response = await test_client.post(
            "/api/v1/menu/validate-item-selection",
            json=payload,
            headers=headers,
        )
        assert response.status_code == 400
        assert "does not belong to this item" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_validate_selection_unavailable_option_rejected_400(
        self,
        test_client: AsyncClient,
        seed_menu_data: dict,
    ):
        """Verify submitting an option with is_available=False returns 400 MODIFIER_OPTION_UNAVAILABLE."""
        token = seed_menu_data["guest_token"]
        headers = {"Authorization": f"Bearer {token}", "Accept-Language": "en"}

        # opt_truffle_sold_out has is_available=False
        payload = {
            "item_id": str(seed_menu_data["item_burger"].id),
            "quantity": 1,
            "selected_groups": [
                {
                    "group_id": str(seed_menu_data["group_doneness"].id),
                    "option_ids": [str(seed_menu_data["opt_medium"].id)],
                },
                {
                    "group_id": str(seed_menu_data["group_toppings"].id),
                    "option_ids": [str(seed_menu_data["opt_truffle_sold_out"].id)],
                },
            ],
        }

        response = await test_client.post(
            "/api/v1/menu/validate-item-selection",
            json=payload,
            headers=headers,
        )
        assert response.status_code == 400
        assert "currently unavailable" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_validate_selection_duplicate_option_rejected_400(
        self,
        test_client: AsyncClient,
        seed_menu_data: dict,
    ):
        """Verify duplicate option_id in the same group returns 400 DUPLICATE_MODIFIER_OPTION."""
        token = seed_menu_data["guest_token"]
        headers = {"Authorization": f"Bearer {token}", "Accept-Language": "en"}

        payload = {
            "item_id": str(seed_menu_data["item_burger"].id),
            "quantity": 1,
            "selected_groups": [
                {
                    "group_id": str(seed_menu_data["group_doneness"].id),
                    "option_ids": [str(seed_menu_data["opt_medium"].id)],
                },
                {
                    "group_id": str(seed_menu_data["group_toppings"].id),
                    "option_ids": [
                        str(seed_menu_data["opt_cheese"].id),
                        str(seed_menu_data["opt_cheese"].id),
                    ],
                },
            ],
        }

        response = await test_client.post(
            "/api/v1/menu/validate-item-selection",
            json=payload,
            headers=headers,
        )
        assert response.status_code == 400
        assert "Duplicate modifier option selections" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_validate_selection_duplicate_group_rejected_400(
        self,
        test_client: AsyncClient,
        seed_menu_data: dict,
    ):
        """Verify duplicate group_id submitted returns 400 DUPLICATE_MODIFIER_GROUP."""
        token = seed_menu_data["guest_token"]
        headers = {"Authorization": f"Bearer {token}", "Accept-Language": "en"}

        payload = {
            "item_id": str(seed_menu_data["item_burger"].id),
            "quantity": 1,
            "selected_groups": [
                {
                    "group_id": str(seed_menu_data["group_doneness"].id),
                    "option_ids": [str(seed_menu_data["opt_medium"].id)],
                },
                {
                    "group_id": str(seed_menu_data["group_doneness"].id),
                    "option_ids": [str(seed_menu_data["opt_well_done"].id)],
                },
            ],
        }

        response = await test_client.post(
            "/api/v1/menu/validate-item-selection",
            json=payload,
            headers=headers,
        )
        assert response.status_code == 400
        assert "Duplicate modifier group selections" in response.json()["detail"]
