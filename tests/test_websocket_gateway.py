"""Comprehensive Automated Test Suite for Task BE-3.1: ASGI WebSocket Gateway & Redis Pub/Sub.

Verifies:
1. WebSocketManager unit behavior:
   - Safe subscription registration and reverse lookup indexing.
   - Clean eviction upon disconnect and automatic pruning of empty channels.
   - Local frame broadcasting and broken-pipe auto-eviction.
2. RedisPubSubBridge behavior:
   - Asynchronous listener, deserialization, and routing.
   - Real-time publish and delivery via Redis Pub/Sub.
   - Resilient local fallback when Redis is offline.
3. WebSocket Handshake & Auth Endpoints:
   - Immediate close with code 4001 on missing, malformed, or expired tokens.
   - Immediate close with code 4003 when guest presence is unverified.
   - Immediate close with code 4003 on unauthorized branch scoping.
   - Verified guest connection subscribed to scoped table & session channels.
   - Staff role channel scoping (WAITER/RUNNER -> runners, KITCHEN -> kitchen, ADMIN -> all).
   - Real-time event delivery and ping/pong keep-alive.
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect, WebSocketState

from app.api.v1.websocket import get_session_factory
from app.core.config import settings
from app.core.redis_pubsub import RedisPubSubBridge, publish
from app.core.security import create_access_token, get_password_hash
from app.core.session_security import create_guest_session_jwt
from app.core.websocket_manager import WebSocketManager, ws_manager
from app.main import create_app
from app.models.auth import Base, Branch, Table, Tenant, User, UserBranchAccess
from app.models.enums import TableStatus, UserRole


# ---------------------------------------------------------------------------
# Test Database Engine & Session Fixtures
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
    """Session factory bound to the in-memory SQLite engine."""
    return async_sessionmaker(
        bind=async_test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autocommit=False,
        autoflush=False,
    )


@pytest_asyncio.fixture(scope="function")
async def seed_data(test_session_factory: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
    """Seed tenant, branch, tables, and staff users across roles."""
    async with test_session_factory() as session:
        # Tenant
        tenant = Tenant(
            id=uuid.uuid4(),
            name="WS Test Dining Co",
            slug=f"ws-dining-{uuid.uuid4().hex[:6]}",
            is_active=True,
        )
        session.add(tenant)
        await session.flush()

        # Branches
        branch_a = Branch(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            name={"en": "Branch Alpha", "ar": "فرع ألفا"},
            slug=f"branch-a-{uuid.uuid4().hex[:6]}",
            latitude=Decimal("24.7136"),
            longitude=Decimal("46.6753"),
            is_active=True,
        )
        branch_b = Branch(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            name={"en": "Branch Beta", "ar": "فرع بيتا"},
            slug=f"branch-b-{uuid.uuid4().hex[:6]}",
            latitude=Decimal("24.7136"),
            longitude=Decimal("46.6753"),
            is_active=True,
        )
        session.add_all([branch_a, branch_b])
        await session.flush()

        # Tables
        table_1 = Table(
            id=uuid.uuid4(),
            branch_id=branch_a.id,
            table_number="T-01",
            capacity=4,
            status=TableStatus.AVAILABLE,
            is_active=True,
        )
        session.add(table_1)
        await session.flush()

        # Password
        hashed_pwd = get_password_hash("Secret123!")

        # Waiter (Branch A only)
        waiter = User(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            email="waiter@test.com",
            hashed_password=hashed_pwd,
            full_name="Waiter Alice",
            role=UserRole.WAITER,
            is_active=True,
        )
        session.add(waiter)
        await session.flush()
        session.add(UserBranchAccess(user_id=waiter.id, branch_id=branch_a.id))

        # Kitchen Staff (Branch A only)
        kitchen = User(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            email="chef@test.com",
            hashed_password=hashed_pwd,
            full_name="Chef Bob",
            role=UserRole.KITCHEN_STAFF,
            is_active=True,
        )
        session.add(kitchen)
        await session.flush()
        session.add(UserBranchAccess(user_id=kitchen.id, branch_id=branch_a.id))

        # Branch Admin (Branch A only)
        admin = User(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            email="admin@test.com",
            hashed_password=hashed_pwd,
            full_name="Admin Dave",
            role=UserRole.BRANCH_ADMIN,
            is_active=True,
        )
        session.add(admin)
        await session.flush()
        session.add(UserBranchAccess(user_id=admin.id, branch_id=branch_a.id))

        # Inactive User
        inactive = User(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            email="inactive@test.com",
            hashed_password=hashed_pwd,
            full_name="Inactive Staff",
            role=UserRole.WAITER,
            is_active=False,
        )
        session.add(inactive)
        await session.flush()
        session.add(UserBranchAccess(user_id=inactive.id, branch_id=branch_a.id))

        # Staff with No Branches
        unassigned = User(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            email="nobranches@test.com",
            hashed_password=hashed_pwd,
            full_name="Unassigned Staff",
            role=UserRole.WAITER,
            is_active=True,
        )
        session.add(unassigned)

        await session.commit()

        return {
            "tenant": tenant,
            "branch_a": branch_a,
            "branch_b": branch_b,
            "table_1": table_1,
            "waiter": waiter,
            "kitchen": kitchen,
            "admin": admin,
            "inactive": inactive,
            "unassigned": unassigned,
        }


# ---------------------------------------------------------------------------
# Part 1: WebSocketManager Unit Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ws_manager_connect_and_subscriptions():
    """Verify subscription registration and reverse lookup mappings."""
    manager = WebSocketManager()
    ws1 = AsyncMock()
    ws1.client_state = WebSocketState.CONNECTED

    channels = ["branch_1_kitchen", "branch_1_runners"]
    await manager.connect(ws1, channels)

    assert manager.is_connected(ws1) is True
    assert manager.get_socket_channels(ws1) == set(channels)
    assert manager.get_channel_subscribers_count("branch_1_kitchen") == 1
    assert manager.get_channel_subscribers_count("branch_1_runners") == 1
    assert manager.get_active_channels() == {"branch_1_kitchen", "branch_1_runners"}

    # Connect second socket to overlapping channel
    ws2 = AsyncMock()
    ws2.client_state = WebSocketState.CONNECTED
    await manager.connect(ws2, ["branch_1_kitchen", "branch_1_admin"])

    assert manager.get_channel_subscribers_count("branch_1_kitchen") == 2
    assert manager.get_channel_subscribers_count("branch_1_admin") == 1


@pytest.mark.asyncio
async def test_ws_manager_disconnect_and_pruning():
    """Verify clean socket eviction and automatic pruning of empty channels."""
    manager = WebSocketManager()
    ws1 = AsyncMock()
    ws1.client_state = WebSocketState.CONNECTED
    await manager.connect(ws1, ["ch_a", "ch_b"])

    evicted = await manager.disconnect(ws1)
    assert evicted == {"ch_a", "ch_b"}
    assert manager.is_connected(ws1) is False
    assert manager.get_active_channels() == set()
    assert manager.get_channel_subscribers_count("ch_a") == 0

    # Idempotent disconnect
    evicted_again = await manager.disconnect(ws1)
    assert evicted_again == set()


@pytest.mark.asyncio
async def test_ws_manager_broadcast_local_success():
    """Verify delivery of parsed frames to local subscribed sockets."""
    manager = WebSocketManager()
    ws_kitchen = AsyncMock()
    ws_kitchen.client_state = WebSocketState.CONNECTED
    ws_waiter = AsyncMock()
    ws_waiter.client_state = WebSocketState.CONNECTED

    await manager.connect(ws_kitchen, ["branch_10_kitchen"])
    await manager.connect(ws_waiter, ["branch_10_runners"])

    payload = {"event": "ORDER_PLACED", "order_id": "ord_100"}
    await manager.broadcast_local("branch_10_kitchen", payload)

    ws_kitchen.send_json.assert_awaited_once_with(payload)
    ws_waiter.send_json.assert_not_awaited()


@pytest.mark.asyncio
async def test_ws_manager_broken_pipe_auto_eviction():
    """Verify broken socket eviction during local broadcast while preserving healthy sockets."""
    manager = WebSocketManager()
    ws_broken = AsyncMock()
    ws_broken.client_state = WebSocketState.CONNECTED
    ws_broken.send_json.side_effect = RuntimeError("Broken pipe / Connection reset")

    ws_healthy = AsyncMock()
    ws_healthy.client_state = WebSocketState.CONNECTED

    channel = "branch_1_broadcast"
    await manager.connect(ws_broken, [channel])
    await manager.connect(ws_healthy, [channel])
    assert manager.get_channel_subscribers_count(channel) == 2

    # Broadcast should detect broken pipe on ws_broken, evict it, and deliver to ws_healthy
    payload = {"event": "ALERT", "msg": "test"}
    await manager.broadcast_local(channel, payload)

    ws_healthy.send_json.assert_awaited_once_with(payload)
    assert manager.is_connected(ws_broken) is False
    assert manager.is_connected(ws_healthy) is True
    assert manager.get_channel_subscribers_count(channel) == 1


# ---------------------------------------------------------------------------
# Part 2: RedisPubSubBridge Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_redis_pubsub_start_publish_and_broadcast():
    """Verify end-to-end publish and background reader loop dispatch via local Redis."""
    custom_manager = WebSocketManager()
    bridge = RedisPubSubBridge()

    # Start bridge against local Redis
    await bridge.start(custom_manager)

    try:
        mock_ws = AsyncMock()
        mock_ws.client_state = WebSocketState.CONNECTED
        channel = f"branch_{uuid.uuid4().hex[:8]}_runners"
        await custom_manager.connect(mock_ws, [channel])

        # Publish via bridge
        await bridge.publish(channel, "TEST_EVENT", {"data_key": "data_val"})

        # Allow background listener task to multiplex frame
        for _ in range(20):
            if mock_ws.send_json.await_count > 0:
                break
            await asyncio.sleep(0.05)

        assert mock_ws.send_json.await_count >= 1
        call_arg = mock_ws.send_json.await_args[0][0]
        assert call_arg["event"] == "TEST_EVENT"
        assert call_arg["data"]["data_key"] == "data_val"
        assert call_arg["channel"] == channel
    finally:
        await bridge.stop()


@pytest.mark.asyncio
async def test_redis_pubsub_fallback_when_offline():
    """Verify resilient local fallback when Redis server is offline."""
    custom_manager = WebSocketManager()
    # Unreachable port
    bridge = RedisPubSubBridge(redis_url="redis://127.0.0.1:59999/0")
    await bridge.start(custom_manager)

    mock_ws = AsyncMock()
    mock_ws.client_state = WebSocketState.CONNECTED
    channel = "branch_offline_fallback"
    await custom_manager.connect(mock_ws, [channel])

    # Publish should fallback directly to local broadcast without throwing
    await bridge.publish(channel, "FALLBACK_EVENT", {"status": "local"})

    mock_ws.send_json.assert_awaited_once()
    payload = mock_ws.send_json.await_args[0][0]
    assert payload["event"] == "FALLBACK_EVENT"
    assert payload["data"]["status"] == "local"

    await bridge.stop()


# ---------------------------------------------------------------------------
# Part 3: WebSocket Handshake & Security Tests
# ---------------------------------------------------------------------------

def test_ws_reject_missing_token():
    """Assert WebSocket handshake closes with code 4001 if token is missing."""
    app = create_app()
    client = TestClient(app)

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/api/v1/ws"):
            pass
    assert exc_info.value.code == 4001


def test_ws_reject_malformed_token():
    """Assert WebSocket handshake closes with code 4001 on malformed JWT."""
    app = create_app()
    client = TestClient(app)

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/api/v1/ws?token=invalid.jwt.token"):
            pass
    assert exc_info.value.code == 4001


def test_ws_reject_expired_guest_token(seed_data: dict[str, Any]):
    """Assert WebSocket handshake closes with code 4001 on expired guest token."""
    app = create_app()
    client = TestClient(app)

    tenant = seed_data["tenant"]
    branch = seed_data["branch_a"]
    table = seed_data["table_1"]

    expired_token = create_guest_session_jwt(
        session_id=uuid.uuid4(),
        tenant_id=tenant.id,
        branch_id=branch.id,
        table_id=table.id,
        table_number=table.table_number,
        is_presence_verified=True,
        expires_delta=timedelta(seconds=-10),
    )

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(f"/api/v1/ws?token={expired_token}"):
            pass
    assert exc_info.value.code == 4001


def test_ws_reject_unverified_guest_token(seed_data: dict[str, Any]):
    """Assert WebSocket handshake closes with code 4003 if guest presence is unverified."""
    app = create_app()
    client = TestClient(app)

    tenant = seed_data["tenant"]
    branch = seed_data["branch_a"]
    table = seed_data["table_1"]

    unverified_token = create_guest_session_jwt(
        session_id=uuid.uuid4(),
        tenant_id=tenant.id,
        branch_id=branch.id,
        table_id=table.id,
        table_number=table.table_number,
        is_presence_verified=False,  # Unverified
    )

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(f"/api/v1/ws?token={unverified_token}"):
            pass
    assert exc_info.value.code == 4003


def test_ws_reject_staff_inactive_user(
    seed_data: dict[str, Any],
    test_session_factory: async_sessionmaker[AsyncSession],
):
    """Assert WebSocket handshake closes with code 4001 for inactive staff user."""
    app = create_app()
    app.dependency_overrides[get_session_factory] = lambda: test_session_factory
    client = TestClient(app)

    inactive_user = seed_data["inactive"]
    token = create_access_token({
        "sub": str(inactive_user.id),
        "tenant_id": str(inactive_user.tenant_id),
        "role": inactive_user.role.value,
    })

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(f"/api/v1/ws?token={token}"):
            pass
    assert exc_info.value.code == 4001


def test_ws_reject_staff_unauthorized_branch(
    seed_data: dict[str, Any],
    test_session_factory: async_sessionmaker[AsyncSession],
):
    """Assert WebSocket handshake closes with code 4003 when requesting an unauthorized branch."""
    app = create_app()
    app.dependency_overrides[get_session_factory] = lambda: test_session_factory
    client = TestClient(app)

    waiter = seed_data["waiter"]
    branch_b = seed_data["branch_b"]  # Waiter only has access to branch_a

    token = create_access_token({
        "sub": str(waiter.id),
        "tenant_id": str(waiter.tenant_id),
        "role": waiter.role.value,
    })

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(f"/api/v1/ws?token={token}&branch_id={branch_b.id}"):
            pass
    assert exc_info.value.code == 4003


def test_ws_reject_staff_no_branch_access(
    seed_data: dict[str, Any],
    test_session_factory: async_sessionmaker[AsyncSession],
):
    """Assert WebSocket handshake closes with code 4003 when staff has no branch access assigned."""
    app = create_app()
    app.dependency_overrides[get_session_factory] = lambda: test_session_factory
    client = TestClient(app)

    unassigned = seed_data["unassigned"]
    token = create_access_token({
        "sub": str(unassigned.id),
        "tenant_id": str(unassigned.tenant_id),
        "role": unassigned.role.value,
    })

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(f"/api/v1/ws?token={token}"):
            pass
    assert exc_info.value.code == 4003


# ---------------------------------------------------------------------------
# Part 4: Successful Connection & Scoped Channel Routing Tests
# ---------------------------------------------------------------------------

def test_ws_guest_verified_connection_and_ping_pong(seed_data: dict[str, Any]):
    """Verify presence-verified guest connects, receives CONNECTED frame, and handles ping/pong."""
    app = create_app()
    client = TestClient(app)

    tenant = seed_data["tenant"]
    branch = seed_data["branch_a"]
    table = seed_data["table_1"]
    session_id = uuid.uuid4()

    token = create_guest_session_jwt(
        session_id=session_id,
        tenant_id=tenant.id,
        branch_id=branch.id,
        table_id=table.id,
        table_number=table.table_number,
        is_presence_verified=True,
    )

    with client.websocket_connect(f"/api/v1/ws?token={token}") as ws:
        frame = ws.receive_json()
        assert frame["event"] == "CONNECTED"
        expected_table_ch = f"branch_{branch.id}_table_{table.id}"
        expected_session_ch = f"branch_{branch.id}_session_{session_id}"
        assert expected_table_ch in frame["channels"]
        assert expected_session_ch in frame["channels"]
        assert frame["identity"]["type"] == "guest"

        # Ping string test
        ws.send_text("ping")
        assert ws.receive_text() == "pong"

        # Ping JSON test
        ws.send_json({"action": "ping"})
        pong_frame = ws.receive_json()
        assert pong_frame["event"] == "PONG"


def test_ws_staff_waiter_channel_scoping(
    seed_data: dict[str, Any],
    test_session_factory: async_sessionmaker[AsyncSession],
):
    """Verify WAITER role subscribes to 'staff' and standardized 'runners' channel."""
    app = create_app()
    app.dependency_overrides[get_session_factory] = lambda: test_session_factory
    client = TestClient(app)

    waiter = seed_data["waiter"]
    branch_a = seed_data["branch_a"]

    token = create_access_token({
        "sub": str(waiter.id),
        "tenant_id": str(waiter.tenant_id),
        "role": waiter.role.value,
    })

    with client.websocket_connect(f"/api/v1/ws?token={token}&branch_id={branch_a.id}") as ws:
        frame = ws.receive_json()
        assert frame["event"] == "CONNECTED"
        channels = set(frame["channels"])
        assert f"branch_{branch_a.id}_staff" in channels
        assert f"branch_{branch_a.id}_runners" in channels
        # Kitchen and admin channels should NOT be present for waiter
        assert f"branch_{branch_a.id}_kitchen" not in channels
        assert f"branch_{branch_a.id}_admin" not in channels


def test_ws_staff_kitchen_channel_scoping(
    seed_data: dict[str, Any],
    test_session_factory: async_sessionmaker[AsyncSession],
):
    """Verify KITCHEN_STAFF role subscribes to 'staff' and 'kitchen' channels."""
    app = create_app()
    app.dependency_overrides[get_session_factory] = lambda: test_session_factory
    client = TestClient(app)

    kitchen = seed_data["kitchen"]
    branch_a = seed_data["branch_a"]

    token = create_access_token({
        "sub": str(kitchen.id),
        "tenant_id": str(kitchen.tenant_id),
        "role": kitchen.role.value,
    })

    with client.websocket_connect(f"/api/v1/ws?token={token}&branch_id={branch_a.id}") as ws:
        frame = ws.receive_json()
        assert frame["event"] == "CONNECTED"
        channels = set(frame["channels"])
        assert f"branch_{branch_a.id}_staff" in channels
        assert f"branch_{branch_a.id}_kitchen" in channels
        # Runners and admin channels should NOT be present for kitchen staff
        assert f"branch_{branch_a.id}_runners" not in channels
        assert f"branch_{branch_a.id}_admin" not in channels


def test_ws_staff_admin_receives_all_role_channels(
    seed_data: dict[str, Any],
    test_session_factory: async_sessionmaker[AsyncSession],
):
    """Verify BRANCH_ADMIN role subscribes to all operational role channels."""
    app = create_app()
    app.dependency_overrides[get_session_factory] = lambda: test_session_factory
    client = TestClient(app)

    admin = seed_data["admin"]
    branch_a = seed_data["branch_a"]

    token = create_access_token({
        "sub": str(admin.id),
        "tenant_id": str(admin.tenant_id),
        "role": admin.role.value,
    })

    with client.websocket_connect(f"/api/v1/ws?token={token}") as ws:
        frame = ws.receive_json()
        assert frame["event"] == "CONNECTED"
        channels = set(frame["channels"])
        assert f"branch_{branch_a.id}_admin" in channels
        assert f"branch_{branch_a.id}_staff" in channels
        assert f"branch_{branch_a.id}_runners" in channels
        assert f"branch_{branch_a.id}_kitchen" in channels
        assert f"branch_{branch_a.id}_cashier" in channels


def test_ws_realtime_broadcast_delivery(seed_data: dict[str, Any]):
    """Verify real-time event published across channel is delivered to connected client."""
    app = create_app()
    client = TestClient(app)

    tenant = seed_data["tenant"]
    branch = seed_data["branch_a"]
    table = seed_data["table_1"]
    session_id = uuid.uuid4()

    token = create_guest_session_jwt(
        session_id=session_id,
        tenant_id=tenant.id,
        branch_id=branch.id,
        table_id=table.id,
        table_number=table.table_number,
        is_presence_verified=True,
    )

    table_channel = f"branch_{branch.id}_table_{table.id}"

    with client.websocket_connect(f"/api/v1/ws?token={token}") as ws:
        init_frame = ws.receive_json()
        assert init_frame["event"] == "CONNECTED"

        # Broadcast event directly via ws_manager
        order_event = {
            "event": "ORDER_STATUS_UPDATED",
            "data": {"order_id": "ord_999", "status": "PREPARING"},
        }
        # Run broadcast synchronously using asyncio
        loop = asyncio.get_event_loop()
        loop.run_until_complete(ws_manager.broadcast_local(table_channel, order_event))

        received = ws.receive_json()
        assert received["event"] == "ORDER_STATUS_UPDATED"
        assert received["data"]["order_id"] == "ord_999"
        assert received["data"]["status"] == "PREPARING"


def test_ws_disconnect_cleans_up_registry(seed_data: dict[str, Any]):
    """Verify disconnecting a WebSocket immediately cleans up the manager's channel registry."""
    app = create_app()
    client = TestClient(app)

    tenant = seed_data["tenant"]
    branch = seed_data["branch_a"]
    table = seed_data["table_1"]
    session_id = uuid.uuid4()

    token = create_guest_session_jwt(
        session_id=session_id,
        tenant_id=tenant.id,
        branch_id=branch.id,
        table_id=table.id,
        table_number=table.table_number,
        is_presence_verified=True,
    )

    table_channel = f"branch_{branch.id}_table_{table.id}"
    initial_count = ws_manager.get_channel_subscribers_count(table_channel)

    with client.websocket_connect(f"/api/v1/ws?token={token}") as ws:
        ws.receive_json()
        # Should be +1 subscriber during active connection
        assert ws_manager.get_channel_subscribers_count(table_channel) == initial_count + 1

    # After exiting context manager, client is disconnected
    assert ws_manager.get_channel_subscribers_count(table_channel) == initial_count
