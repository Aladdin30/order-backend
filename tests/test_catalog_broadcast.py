"""Comprehensive Automated Test Suite for Task BE-3.3: Real-Time Item & Modifier 86 Broadcast Service.

Verifies:
1. Broadcast helper methods publish to `branch_{branch_id}_catalog` with event `ITEM_AVAILABILITY_CHANGED`.
2. Both Guest and Staff WebSockets subscribe to `branch_{branch_id}_catalog` upon connection.
3. Direct Item 86 toggle endpoint triggers real-time broadcast to connected sockets.
4. General Item PATCH update triggers broadcast when `is_available` is updated.
5. Direct Modifier Option 86 toggle endpoint triggers real-time broadcast.
6. General Modifier Option PATCH update triggers real-time broadcast when `is_available` is updated.
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from typing import Any, AsyncGenerator
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from starlette.testclient import TestClient

from app.api.deps import get_async_db
from app.api.v1.websocket import get_session_factory
from app.core.security import create_access_token, get_password_hash
from app.core.session_security import create_guest_session_jwt
from app.main import create_app
from app.models.auth import Base, Branch, Table, Tenant, User, UserBranchAccess
from app.models.catalog import Category, Item, ModifierGroup, ModifierOption
from app.models.enums import KitchenStation, TableStatus, UserRole
from app.services.catalog_broadcast_service import (
    broadcast_item_availability_change,
    broadcast_modifier_availability_change,
)


# ---------------------------------------------------------------------------
# Database & Seed Fixtures
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
async def seed_data(test_session_factory: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
    """Seed tenant, branch, categories, items, modifiers, and staff users."""
    async with test_session_factory() as session:
        tenant = Tenant(
            id=uuid.uuid4(),
            name="86 Broadcast Dining Co",
            slug=f"broadcast-co-{uuid.uuid4().hex[:6]}",
            is_active=True,
        )
        session.add(tenant)
        await session.flush()

        branch = Branch(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            name={"en": "Broadcast Branch", "ar": "فرع البث"},
            slug=f"b-branch-{uuid.uuid4().hex[:6]}",
            latitude=Decimal("24.7136"),
            longitude=Decimal("46.6753"),
            is_active=True,
        )
        session.add(branch)
        await session.flush()

        table = Table(
            id=uuid.uuid4(),
            branch_id=branch.id,
            table_number="T-86",
            capacity=4,
            status=TableStatus.AVAILABLE,
            is_active=True,
        )
        session.add(table)
        await session.flush()

        category = Category(
            id=uuid.uuid4(),
            branch_id=branch.id,
            name={"en": "Burgers", "ar": "برجر"},
            station=KitchenStation.HOT_KITCHEN,
            is_active=True,
        )
        session.add(category)
        await session.flush()

        item = Item(
            id=uuid.uuid4(),
            category_id=category.id,
            name={"en": "Smash Burger", "ar": "سماش برجر"},
            base_price=Decimal("40.00"),
            station=KitchenStation.HOT_KITCHEN,
            is_available=True,
        )
        session.add(item)
        await session.flush()

        group = ModifierGroup(
            id=uuid.uuid4(),
            item_id=item.id,
            name={"en": "Cheese Selection", "ar": "اختيار الجبن"},
            min_choices=0,
            max_choices=1,
            is_required=False,
        )
        session.add(group)
        await session.flush()

        option = ModifierOption(
            id=uuid.uuid4(),
            modifier_group_id=group.id,
            name={"en": "Cheddar", "ar": "شيدر"},
            price_delta=Decimal("5.00"),
            is_available=True,
        )
        session.add(option)
        await session.flush()

        pwd = get_password_hash("Secret123!")
        admin = User(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            email="admin86@test.com",
            hashed_password=pwd,
            full_name="Admin 86",
            role=UserRole.BRANCH_ADMIN,
            is_active=True,
        )
        session.add(admin)
        await session.flush()
        session.add(UserBranchAccess(user_id=admin.id, branch_id=branch.id))

        await session.commit()

        return {
            "tenant": tenant,
            "branch": branch,
            "table": table,
            "category": category,
            "item": item,
            "group": group,
            "option": option,
            "admin": admin,
        }


# ---------------------------------------------------------------------------
# Part 1: Helper Unit Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_broadcast_item_availability_change_helper():
    """Verify broadcast_item_availability_change publishes to correct catalog channel."""
    branch_id = uuid.uuid4()
    item_id = uuid.uuid4()

    with patch("app.services.catalog_broadcast_service.redis_pubsub.publish", new_callable=AsyncMock) as mock_pub:
        await broadcast_item_availability_change(branch_id=branch_id, item_id=item_id, is_available=False)

        mock_pub.assert_awaited_once_with(
            channel=f"branch_{branch_id}_catalog",
            event_type="ITEM_AVAILABILITY_CHANGED",
            data={
                "entity_type": "ITEM",
                "entity_id": str(item_id),
                "item_id": str(item_id),
                "is_available": False,
                "branch_id": str(branch_id),
            },
        )


@pytest.mark.asyncio
async def test_broadcast_modifier_availability_change_helper():
    """Verify broadcast_modifier_availability_change publishes to correct catalog channel."""
    branch_id = uuid.uuid4()
    option_id = uuid.uuid4()
    group_id = uuid.uuid4()

    with patch("app.services.catalog_broadcast_service.redis_pubsub.publish", new_callable=AsyncMock) as mock_pub:
        await broadcast_modifier_availability_change(
            branch_id=branch_id,
            modifier_option_id=option_id,
            group_id=group_id,
            is_available=True,
        )

        mock_pub.assert_awaited_once_with(
            channel=f"branch_{branch_id}_catalog",
            event_type="ITEM_AVAILABILITY_CHANGED",
            data={
                "entity_type": "MODIFIER_OPTION",
                "entity_id": str(option_id),
                "group_id": str(group_id),
                "is_available": True,
                "branch_id": str(branch_id),
            },
        )


# ---------------------------------------------------------------------------
# Part 2: Channel Topology Subscription Tests
# ---------------------------------------------------------------------------

def test_guest_and_staff_subscribe_to_catalog_channel(
    seed_data: dict[str, Any],
    test_session_factory: async_sessionmaker[AsyncSession],
):
    """Verify both guest and staff WebSockets automatically subscribe to branch_{id}_catalog."""
    app = create_app()
    app.dependency_overrides[get_session_factory] = lambda: test_session_factory

    branch = seed_data["branch"]
    table = seed_data["table"]
    admin = seed_data["admin"]

    with TestClient(app) as client:
        # 1. Guest connection
        guest_token = create_guest_session_jwt(
            session_id=uuid.uuid4(),
            tenant_id=seed_data["tenant"].id,
            branch_id=branch.id,
            table_id=table.id,
            table_number=table.table_number,
            is_presence_verified=True,
        )
        with client.websocket_connect(f"/api/v1/ws?token={guest_token}") as ws:
            frame = ws.receive_json()
            assert frame["event"] == "CONNECTED"
            assert f"branch_{branch.id}_catalog" in frame["channels"]

        # 2. Staff connection
        staff_token = create_access_token({
            "sub": str(admin.id),
            "tenant_id": str(admin.tenant_id),
            "role": admin.role.value,
        })
        with client.websocket_connect(f"/api/v1/ws?token={staff_token}&branch_id={branch.id}") as ws:
            frame = ws.receive_json()
            assert frame["event"] == "CONNECTED"
            assert f"branch_{branch.id}_catalog" in frame["channels"]


# ---------------------------------------------------------------------------
# Part 3: End-to-End Live 86 Broadcast Tests
# ---------------------------------------------------------------------------

def test_end_to_end_item_86_toggle_broadcast(
    seed_data: dict[str, Any],
    test_session_factory: async_sessionmaker[AsyncSession],
):
    """Verify direct toggle /items/{id}/availability triggers live broadcast received by guest WS."""
    app = create_app()

    async def override_get_db():
        async with test_session_factory() as session:
            yield session

    app.dependency_overrides[get_async_db] = override_get_db
    app.dependency_overrides[get_session_factory] = lambda: test_session_factory

    branch = seed_data["branch"]
    table = seed_data["table"]
    item = seed_data["item"]
    admin = seed_data["admin"]

    guest_token = create_guest_session_jwt(
        session_id=uuid.uuid4(),
        tenant_id=seed_data["tenant"].id,
        branch_id=branch.id,
        table_id=table.id,
        table_number=table.table_number,
        is_presence_verified=True,
    )
    admin_token = create_access_token({
        "sub": str(admin.id),
        "tenant_id": str(admin.tenant_id),
        "role": admin.role.value,
    })

    headers = {
        "Authorization": f"Bearer {admin_token}",
        "X-Branch-ID": str(branch.id),
    }

    with TestClient(app) as client:
        with client.websocket_connect(f"/api/v1/ws?token={guest_token}") as ws:
            init_frame = ws.receive_json()
            assert init_frame["event"] == "CONNECTED"

            # Toggle item to unavailable (86)
            patch_res = client.patch(
                f"/api/v1/staff/menu/items/{item.id}/availability",
                headers=headers,
                json={"is_available": False},
            )
            assert patch_res.status_code == 200

            # Guest WS receives real-time broadcast frame
            event_frame = ws.receive_json()
            assert event_frame["event"] == "ITEM_AVAILABILITY_CHANGED"
            assert event_frame["data"]["entity_type"] == "ITEM"
            assert event_frame["data"]["item_id"] == str(item.id)
            assert event_frame["data"]["is_available"] is False

            # Toggle item to available (restocked)
            patch_res2 = client.patch(
                f"/api/v1/staff/menu/items/{item.id}/availability",
                headers=headers,
                json={"is_available": True},
            )
            assert patch_res2.status_code == 200

            event_frame2 = ws.receive_json()
            assert event_frame2["event"] == "ITEM_AVAILABILITY_CHANGED"
            assert event_frame2["data"]["is_available"] is True


def test_end_to_end_item_patch_update_broadcast(
    seed_data: dict[str, Any],
    test_session_factory: async_sessionmaker[AsyncSession],
):
    """Verify general PATCH /items/{id} triggers broadcast when is_available is toggled."""
    app = create_app()

    async def override_get_db():
        async with test_session_factory() as session:
            yield session

    app.dependency_overrides[get_async_db] = override_get_db
    app.dependency_overrides[get_session_factory] = lambda: test_session_factory

    branch = seed_data["branch"]
    table = seed_data["table"]
    item = seed_data["item"]
    admin = seed_data["admin"]

    guest_token = create_guest_session_jwt(
        session_id=uuid.uuid4(),
        tenant_id=seed_data["tenant"].id,
        branch_id=branch.id,
        table_id=table.id,
        table_number=table.table_number,
        is_presence_verified=True,
    )
    admin_token = create_access_token({
        "sub": str(admin.id),
        "tenant_id": str(admin.tenant_id),
        "role": admin.role.value,
    })

    headers = {
        "Authorization": f"Bearer {admin_token}",
        "X-Branch-ID": str(branch.id),
    }

    with TestClient(app) as client:
        with client.websocket_connect(f"/api/v1/ws?token={guest_token}") as ws:
            ws.receive_json()

            # General patch updating is_available
            patch_res = client.patch(
                f"/api/v1/staff/menu/items/{item.id}",
                headers=headers,
                json={"is_available": False},
            )
            assert patch_res.status_code == 200

            event_frame = ws.receive_json()
            assert event_frame["event"] == "ITEM_AVAILABILITY_CHANGED"
            assert event_frame["data"]["entity_type"] == "ITEM"
            assert event_frame["data"]["is_available"] is False


def test_end_to_end_modifier_86_toggle_broadcast(
    seed_data: dict[str, Any],
    test_session_factory: async_sessionmaker[AsyncSession],
):
    """Verify direct toggle /modifier-options/{id}/availability triggers live broadcast."""
    app = create_app()

    async def override_get_db():
        async with test_session_factory() as session:
            yield session

    app.dependency_overrides[get_async_db] = override_get_db
    app.dependency_overrides[get_session_factory] = lambda: test_session_factory

    branch = seed_data["branch"]
    table = seed_data["table"]
    option = seed_data["option"]
    group = seed_data["group"]
    admin = seed_data["admin"]

    guest_token = create_guest_session_jwt(
        session_id=uuid.uuid4(),
        tenant_id=seed_data["tenant"].id,
        branch_id=branch.id,
        table_id=table.id,
        table_number=table.table_number,
        is_presence_verified=True,
    )
    admin_token = create_access_token({
        "sub": str(admin.id),
        "tenant_id": str(admin.tenant_id),
        "role": admin.role.value,
    })

    headers = {
        "Authorization": f"Bearer {admin_token}",
        "X-Branch-ID": str(branch.id),
    }

    with TestClient(app) as client:
        with client.websocket_connect(f"/api/v1/ws?token={guest_token}") as ws:
            ws.receive_json()

            # Toggle modifier option to unavailable
            patch_res = client.patch(
                f"/api/v1/staff/menu/modifier-options/{option.id}/availability",
                headers=headers,
                json={"is_available": False},
            )
            assert patch_res.status_code == 200

            event_frame = ws.receive_json()
            assert event_frame["event"] == "ITEM_AVAILABILITY_CHANGED"
            assert event_frame["data"]["entity_type"] == "MODIFIER_OPTION"
            assert event_frame["data"]["entity_id"] == str(option.id)
            assert event_frame["data"]["group_id"] == str(group.id)
            assert event_frame["data"]["is_available"] is False


def test_end_to_end_modifier_patch_update_broadcast(
    seed_data: dict[str, Any],
    test_session_factory: async_sessionmaker[AsyncSession],
):
    """Verify general PATCH /modifier-options/{id} triggers broadcast when is_available is toggled."""
    app = create_app()

    async def override_get_db():
        async with test_session_factory() as session:
            yield session

    app.dependency_overrides[get_async_db] = override_get_db
    app.dependency_overrides[get_session_factory] = lambda: test_session_factory

    branch = seed_data["branch"]
    table = seed_data["table"]
    option = seed_data["option"]
    group = seed_data["group"]
    admin = seed_data["admin"]

    guest_token = create_guest_session_jwt(
        session_id=uuid.uuid4(),
        tenant_id=seed_data["tenant"].id,
        branch_id=branch.id,
        table_id=table.id,
        table_number=table.table_number,
        is_presence_verified=True,
    )
    admin_token = create_access_token({
        "sub": str(admin.id),
        "tenant_id": str(admin.tenant_id),
        "role": admin.role.value,
    })

    headers = {
        "Authorization": f"Bearer {admin_token}",
        "X-Branch-ID": str(branch.id),
    }

    with TestClient(app) as client:
        with client.websocket_connect(f"/api/v1/ws?token={guest_token}") as ws:
            ws.receive_json()

            # General patch updating is_available on modifier option
            patch_res = client.patch(
                f"/api/v1/staff/menu/modifier-options/{option.id}",
                headers=headers,
                json={"is_available": False},
            )
            assert patch_res.status_code == 200

            event_frame = ws.receive_json()
            assert event_frame["event"] == "ITEM_AVAILABILITY_CHANGED"
            assert event_frame["data"]["entity_type"] == "MODIFIER_OPTION"
            assert event_frame["data"]["entity_id"] == str(option.id)
            assert event_frame["data"]["group_id"] == str(group.id)
            assert event_frame["data"]["is_available"] is False
