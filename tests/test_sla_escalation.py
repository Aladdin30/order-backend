"""Comprehensive Automated Test Suite for Task BE-3.4: Quick-Service Dispatcher Pipeline & Automated SLA Escalation.

Tests:
1. SERVICE_REQUEST_CREATED real-time broadcast to branch_{branch_id}_runners on guest request submission.
2. SERVICE_REQUEST_UPDATED real-time broadcast to both runners and table channels on acknowledge/completion.
3. Database & logic verification of check_and_escalate_overdue_requests (3-minute cutoff, row filtering, synonym compatibility).
4. SERVICE_REQUEST_ESCALATED real-time dual broadcast to branch_{branch_id}_runners and branch_{branch_id}_admin.
5. Multi-branch isolation ensuring other branches do not receive SLA escalation alerts.
6. RBAC & functionality of POST /api/v1/service-requests/sla/escalate-overdue admin endpoint.
7. SLAMonitorWorker lifecycle, background loop, and graceful asyncio.CancelledError shutdown handling.
"""

from __future__ import annotations

import asyncio
import datetime
import uuid
from decimal import Decimal
from typing import Any, AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import select
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
from app.models.enums import (
    ServiceRequestStatus,
    ServiceRequestType,
    TableStatus,
    UserRole,
)
from app.models.service import ServiceRequest
from app.services.sla_monitor_service import (
    SLAMonitorWorker,
    check_and_escalate_overdue_requests,
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
    """Seed tenant, branches, tables, staff users (runner/waiter and admin)."""
    async with test_session_factory() as session:
        tenant = Tenant(
            id=uuid.uuid4(),
            name="SLA Dining Group",
            slug=f"sla-group-{uuid.uuid4().hex[:6]}",
            is_active=True,
        )
        session.add(tenant)
        await session.flush()

        branch_a = Branch(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            name={"en": "Downtown Branch", "ar": "فرع وسط المدينة"},
            slug=f"downtown-{uuid.uuid4().hex[:6]}",
            latitude=Decimal("24.7136"),
            longitude=Decimal("46.6753"),
            is_active=True,
        )
        branch_b = Branch(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            name={"en": "Uptown Branch", "ar": "فرع شمال المدينة"},
            slug=f"uptown-{uuid.uuid4().hex[:6]}",
            latitude=Decimal("24.7500"),
            longitude=Decimal("46.7000"),
            is_active=True,
        )
        session.add_all([branch_a, branch_b])
        await session.flush()

        table_a = Table(
            id=uuid.uuid4(),
            branch_id=branch_a.id,
            table_number="T-07",
            capacity=4,
            status=TableStatus.AVAILABLE,
            is_active=True,
        )
        table_b = Table(
            id=uuid.uuid4(),
            branch_id=branch_b.id,
            table_number="TB-01",
            capacity=2,
            status=TableStatus.AVAILABLE,
            is_active=True,
        )
        session.add_all([table_a, table_b])
        await session.flush()

        pwd = get_password_hash("StaffPass123!")

        waiter = User(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            email="waiter@sla.com",
            hashed_password=pwd,
            full_name="Walter Waiter",
            role=UserRole.WAITER,
            is_active=True,
        )
        admin = User(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            email="admin@sla.com",
            hashed_password=pwd,
            full_name="Alice Admin",
            role=UserRole.BRANCH_ADMIN,
            is_active=True,
        )
        session.add_all([waiter, admin])
        await session.flush()

        session.add_all([
            UserBranchAccess(user_id=waiter.id, branch_id=branch_a.id),
            UserBranchAccess(user_id=admin.id, branch_id=branch_a.id),
        ])

        await session.commit()

        return {
            "tenant": tenant,
            "branch_a": branch_a,
            "branch_b": branch_b,
            "table_a": table_a,
            "table_b": table_b,
            "waiter": waiter,
            "admin": admin,
        }


# ---------------------------------------------------------------------------
# Test 1: SERVICE_REQUEST_CREATED Real-Time Broadcast to Runners
# ---------------------------------------------------------------------------

def test_service_request_created_broadcast_to_runners(
    seed_data: dict[str, Any],
    test_session_factory: async_sessionmaker[AsyncSession],
):
    """Submitting a service request broadcasts SERVICE_REQUEST_CREATED to branch_{branch_id}_runners."""
    app = create_app()

    async def override_get_db():
        async with test_session_factory() as session:
            yield session

    app.dependency_overrides[get_async_db] = override_get_db
    app.dependency_overrides[get_session_factory] = lambda: test_session_factory

    branch = seed_data["branch_a"]
    table = seed_data["table_a"]
    waiter = seed_data["waiter"]

    guest_jwt = create_guest_session_jwt(
        session_id=uuid.uuid4(),
        tenant_id=seed_data["tenant"].id,
        branch_id=branch.id,
        table_id=table.id,
        table_number=table.table_number,
        is_presence_verified=True,
    )
    waiter_jwt = create_access_token({
        "sub": str(waiter.id),
        "tenant_id": str(waiter.tenant_id),
        "role": waiter.role.value,
    })

    with TestClient(app) as client:
        # Waiter connects to WebSocket and joins branch_..._runners
        with client.websocket_connect(f"/api/v1/ws?token={waiter_jwt}&branch_id={branch.id}") as ws_waiter:
            init_frame = ws_waiter.receive_json()
            assert init_frame["event"] == "CONNECTED"
            assert f"branch_{branch.id}_runners" in init_frame["channels"]

            # Guest submits a service request
            create_resp = client.post(
                "/api/v1/service-requests",
                headers={"Authorization": f"Bearer {guest_jwt}"},
                json={"request_type": "WATER", "note": "Cold water bottle please"},
            )
            assert create_resp.status_code == 201
            req_data = create_resp.json()
            assert req_data["status"] == "PENDING"
            assert req_data["request_type"] == "WATER"

            # Waiter receives real-time notification
            event_frame = ws_waiter.receive_json()
            assert event_frame["event"] == "SERVICE_REQUEST_CREATED"
            assert event_frame["channel"] == f"branch_{branch.id}_runners"
            payload = event_frame["data"]
            assert payload["request_id"] == req_data["id"]
            assert payload["table_number"] == table.table_number
            assert payload["type"] == "WATER"
            assert payload["notes"] == "Cold water bottle please"
            assert "created_at" in payload


# ---------------------------------------------------------------------------
# Test 2: SERVICE_REQUEST_UPDATED Real-Time Broadcast to Runners & Table
# ---------------------------------------------------------------------------

def test_service_request_updated_broadcast_to_runners_and_table(
    seed_data: dict[str, Any],
    test_session_factory: async_sessionmaker[AsyncSession],
):
    """Status transitions broadcast SERVICE_REQUEST_UPDATED to both runners and table channels."""
    app = create_app()

    async def override_get_db():
        async with test_session_factory() as session:
            yield session

    app.dependency_overrides[get_async_db] = override_get_db
    app.dependency_overrides[get_session_factory] = lambda: test_session_factory

    branch = seed_data["branch_a"]
    table = seed_data["table_a"]
    waiter = seed_data["waiter"]

    guest_jwt = create_guest_session_jwt(
        session_id=uuid.uuid4(),
        tenant_id=seed_data["tenant"].id,
        branch_id=branch.id,
        table_id=table.id,
        table_number=table.table_number,
        is_presence_verified=True,
    )
    waiter_jwt = create_access_token({
        "sub": str(waiter.id),
        "tenant_id": str(waiter.tenant_id),
        "role": waiter.role.value,
    })

    with TestClient(app) as client:
        # Create request first
        create_resp = client.post(
            "/api/v1/service-requests",
            headers={"Authorization": f"Bearer {guest_jwt}"},
            json={"request_type": "CUTLERY", "note": None},
        )
        assert create_resp.status_code == 201
        request_id = create_resp.json()["id"]

        # Guest and Waiter connect to WebSocket
        with client.websocket_connect(f"/api/v1/ws?token={guest_jwt}") as ws_guest, \
             client.websocket_connect(f"/api/v1/ws?token={waiter_jwt}&branch_id={branch.id}") as ws_waiter:

            guest_init = ws_guest.receive_json()
            assert guest_init["event"] == "CONNECTED"
            assert f"branch_{branch.id}_table_{table.id}" in guest_init["channels"]

            waiter_init = ws_waiter.receive_json()
            assert waiter_init["event"] == "CONNECTED"

            # 1. Staff transitions status to ACKNOWLEDGED
            patch_resp = client.patch(
                f"/api/v1/service-requests/{request_id}/status",
                headers={
                    "Authorization": f"Bearer {waiter_jwt}",
                    "X-Branch-ID": str(branch.id),
                },
                json={"status": "ACKNOWLEDGED"},
            )
            assert patch_resp.status_code == 200

            # Both guest table and runner receive ACKNOWLEDGED update
            guest_frame = ws_guest.receive_json()
            assert guest_frame["event"] == "SERVICE_REQUEST_UPDATED"
            assert guest_frame["channel"] == f"branch_{branch.id}_table_{table.id}"
            assert guest_frame["data"]["request_id"] == request_id
            assert guest_frame["data"]["status"] == "ACKNOWLEDGED"
            assert guest_frame["data"]["resolved_by"] == str(waiter.id)

            waiter_frame = ws_waiter.receive_json()
            assert waiter_frame["event"] == "SERVICE_REQUEST_UPDATED"
            assert waiter_frame["channel"] == f"branch_{branch.id}_runners"
            assert waiter_frame["data"]["request_id"] == request_id
            assert waiter_frame["data"]["status"] == "ACKNOWLEDGED"

            # 2. Staff transitions status to COMPLETED
            comp_resp = client.patch(
                f"/api/v1/service-requests/{request_id}/status",
                headers={
                    "Authorization": f"Bearer {waiter_jwt}",
                    "X-Branch-ID": str(branch.id),
                },
                json={"status": "COMPLETED"},
            )
            assert comp_resp.status_code == 200

            guest_comp = ws_guest.receive_json()
            assert guest_comp["event"] == "SERVICE_REQUEST_UPDATED"
            assert guest_comp["data"]["status"] == "COMPLETED"

            waiter_comp = ws_waiter.receive_json()
            assert waiter_comp["event"] == "SERVICE_REQUEST_UPDATED"
            assert waiter_comp["data"]["status"] == "COMPLETED"


# ---------------------------------------------------------------------------
# Test 3: SLA Engine check_and_escalate_overdue_requests Core Logic
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_check_and_escalate_overdue_requests_logic(
    seed_data: dict[str, Any],
    test_session_factory: async_sessionmaker[AsyncSession],
):
    """Verify check_and_escalate_overdue_requests filters by 3-minute SLA cutoff and status."""
    now = datetime.datetime.now(datetime.timezone.utc)
    branch = seed_data["branch_a"]
    table = seed_data["table_a"]

    async with test_session_factory() as session:
        # Request 1: Overdue PENDING (created 4 minutes ago) -> SHOULD ESCALATE
        req_overdue = ServiceRequest(
            id=uuid.uuid4(),
            branch_id=branch.id,
            table_id=table.id,
            request_type=ServiceRequestType.WATER,
            status=ServiceRequestStatus.PENDING,
            note="Urgent water",
            is_escalated=False,
            created_at=now - datetime.timedelta(minutes=4),
            updated_at=now - datetime.timedelta(minutes=4),
        )

        # Request 2: Recent PENDING (created 1 minute ago) -> SHOULD NOT ESCALATE
        req_recent = ServiceRequest(
            id=uuid.uuid4(),
            branch_id=branch.id,
            table_id=table.id,
            request_type=ServiceRequestType.CUTLERY,
            status=ServiceRequestStatus.PENDING,
            note="Recent cutlery",
            is_escalated=False,
            created_at=now - datetime.timedelta(minutes=1),
            updated_at=now - datetime.timedelta(minutes=1),
        )

        # Request 3: Old ACKNOWLEDGED (created 5 minutes ago) -> SHOULD NOT ESCALATE
        req_ack = ServiceRequest(
            id=uuid.uuid4(),
            branch_id=branch.id,
            table_id=table.id,
            request_type=ServiceRequestType.WAITER_CALL,
            status=ServiceRequestStatus.ACKNOWLEDGED,
            note="Old but acknowledged",
            is_escalated=False,
            created_at=now - datetime.timedelta(minutes=5),
            acknowledged_at=now - datetime.timedelta(minutes=4),
            updated_at=now - datetime.timedelta(minutes=4),
        )

        # Request 4: Old PENDING ALREADY ESCALATED -> SHOULD NOT RE-ESCALATE
        req_already_esc = ServiceRequest(
            id=uuid.uuid4(),
            branch_id=branch.id,
            table_id=table.id,
            request_type=ServiceRequestType.OTHER,
            status=ServiceRequestStatus.PENDING,
            note="Already escalated",
            is_escalated=True,
            escalated_at=now - datetime.timedelta(minutes=2),
            created_at=now - datetime.timedelta(minutes=6),
            updated_at=now - datetime.timedelta(minutes=2),
        )

        session.add_all([req_overdue, req_recent, req_ack, req_already_esc])
        await session.commit()

        # Run SLA check with default 180s threshold
        escalated_count = await check_and_escalate_overdue_requests(
            db=session,
            threshold_seconds=180,
        )
        assert escalated_count == 1

        # Verify DB updates on Request 1
        refreshed_req1 = (
            await session.execute(select(ServiceRequest).where(ServiceRequest.id == req_overdue.id))
        ).scalar_one()
        assert refreshed_req1.is_escalated is True
        assert refreshed_req1.escalated is True  # synonym check
        assert refreshed_req1.escalated_at is not None

        # Verify Request 2 was untouched
        refreshed_req2 = (
            await session.execute(select(ServiceRequest).where(ServiceRequest.id == req_recent.id))
        ).scalar_one()
        assert refreshed_req2.is_escalated is False
        assert refreshed_req2.escalated_at is None

        # Verify Request 3 was untouched
        refreshed_req3 = (
            await session.execute(select(ServiceRequest).where(ServiceRequest.id == req_ack.id))
        ).scalar_one()
        assert refreshed_req3.is_escalated is False

        # Idempotency check: run again immediately
        second_run = await check_and_escalate_overdue_requests(db=session, threshold_seconds=180)
        assert second_run == 0


# ---------------------------------------------------------------------------
# Test 4: SERVICE_REQUEST_ESCALATED Real-Time Dual Broadcast (Runners + Admin)
# ---------------------------------------------------------------------------

def test_sla_escalation_websocket_broadcast(
    seed_data: dict[str, Any],
    test_session_factory: async_sessionmaker[AsyncSession],
):
    """Escalating overdue requests broadcasts SERVICE_REQUEST_ESCALATED to both runners and admin channels."""
    app = create_app()

    async def override_get_db():
        async with test_session_factory() as session:
            yield session

    app.dependency_overrides[get_async_db] = override_get_db
    app.dependency_overrides[get_session_factory] = lambda: test_session_factory

    branch_a = seed_data["branch_a"]
    table_a = seed_data["table_a"]
    waiter = seed_data["waiter"]
    admin = seed_data["admin"]

    now = datetime.datetime.now(datetime.timezone.utc)

    # Insert an overdue request directly in DB (created 4 minutes ago)
    overdue_id = uuid.uuid4()
    async def create_overdue():
        async with test_session_factory() as session:
            session.add(
                ServiceRequest(
                    id=overdue_id,
                    branch_id=branch_a.id,
                    table_id=table_a.id,
                    request_type=ServiceRequestType.WATER,
                    status=ServiceRequestStatus.PENDING,
                    note="Cold water please",
                    is_escalated=False,
                    created_at=now - datetime.timedelta(minutes=4),
                    updated_at=now - datetime.timedelta(minutes=4),
                )
            )
            await session.commit()

    asyncio.run(create_overdue())

    waiter_jwt = create_access_token({
        "sub": str(waiter.id),
        "tenant_id": str(waiter.tenant_id),
        "role": waiter.role.value,
    })
    admin_jwt = create_access_token({
        "sub": str(admin.id),
        "tenant_id": str(admin.tenant_id),
        "role": admin.role.value,
    })

    with TestClient(app) as client:
        # Waiter joins runners channel, Admin joins admin channel
        with client.websocket_connect(f"/api/v1/ws?token={waiter_jwt}&branch_id={branch_a.id}") as ws_waiter, \
             client.websocket_connect(f"/api/v1/ws?token={admin_jwt}&branch_id={branch_a.id}") as ws_admin:

            waiter_init = ws_waiter.receive_json()
            assert waiter_init["event"] == "CONNECTED"
            assert f"branch_{branch_a.id}_runners" in waiter_init["channels"]

            admin_init = ws_admin.receive_json()
            assert admin_init["event"] == "CONNECTED"
            assert f"branch_{branch_a.id}_admin" in admin_init["channels"]

            # Admin calls the SLA escalation endpoint
            trigger_resp = client.post(
                "/api/v1/service-requests/sla/escalate-overdue?threshold_seconds=180",
                headers={
                    "Authorization": f"Bearer {admin_jwt}",
                    "X-Branch-ID": str(branch_a.id),
                },
            )
            assert trigger_resp.status_code == 200
            assert trigger_resp.json()["escalated_count"] == 1

            # Waiter receives escalation alert on runners channel
            waiter_alert = ws_waiter.receive_json()
            assert waiter_alert["event"] == "SERVICE_REQUEST_ESCALATED"
            assert waiter_alert["channel"] == f"branch_{branch_a.id}_runners"
            assert waiter_alert["data"]["request_id"] == str(overdue_id)
            assert waiter_alert["data"]["table_number"] == table_a.table_number
            assert waiter_alert["data"]["type"] == "WATER"
            assert waiter_alert["data"]["elapsed_seconds"] >= 240

            # Admin is subscribed to both runners and admin channels, so receives on both
            admin_frames = [ws_admin.receive_json(), ws_admin.receive_json()]
            admin_channels = {f["channel"] for f in admin_frames}
            assert admin_channels == {
                f"branch_{branch_a.id}_runners",
                f"branch_{branch_a.id}_admin",
            }
            for f in admin_frames:
                assert f["event"] == "SERVICE_REQUEST_ESCALATED"
                assert f["data"]["request_id"] == str(overdue_id)
                assert f["data"]["table_number"] == table_a.table_number
                assert f["data"]["type"] == "WATER"


# ---------------------------------------------------------------------------
# Test 5: Multi-Branch Isolation for SLA Escalation Alerts
# ---------------------------------------------------------------------------

def test_sla_escalation_branch_isolation(
    seed_data: dict[str, Any],
    test_session_factory: async_sessionmaker[AsyncSession],
):
    """SLA escalation in branch Alpha does not send events to branch Beta sockets."""
    app = create_app()

    async def override_get_db():
        async with test_session_factory() as session:
            yield session

    app.dependency_overrides[get_async_db] = override_get_db
    app.dependency_overrides[get_session_factory] = lambda: test_session_factory

    branch_a = seed_data["branch_a"]
    branch_b = seed_data["branch_b"]
    table_b = seed_data["table_b"]
    admin = seed_data["admin"]

    # Assign admin to branch_b as well for testing
    async def assign_b():
        async with test_session_factory() as session:
            session.add(UserBranchAccess(user_id=admin.id, branch_id=branch_b.id))
            await session.commit()

    asyncio.run(assign_b())

    now = datetime.datetime.now(datetime.timezone.utc)
    overdue_id = uuid.uuid4()

    async def create_b_overdue():
        async with test_session_factory() as session:
            session.add(
                ServiceRequest(
                    id=overdue_id,
                    branch_id=branch_b.id,
                    table_id=table_b.id,
                    request_type=ServiceRequestType.CUTLERY,
                    status=ServiceRequestStatus.PENDING,
                    note="Fork for branch B",
                    is_escalated=False,
                    created_at=now - datetime.timedelta(minutes=5),
                    updated_at=now - datetime.timedelta(minutes=5),
                )
            )
            await session.commit()

    asyncio.run(create_b_overdue())

    admin_jwt = create_access_token({
        "sub": str(admin.id),
        "tenant_id": str(admin.tenant_id),
        "role": admin.role.value,
    })

    with TestClient(app) as client:
        # Connect to branch_a runner channel
        with client.websocket_connect(f"/api/v1/ws?token={admin_jwt}&branch_id={branch_a.id}") as ws_branch_a, \
             client.websocket_connect(f"/api/v1/ws?token={admin_jwt}&branch_id={branch_b.id}") as ws_branch_b:

            ws_branch_a.receive_json()  # init frame
            ws_branch_b.receive_json()  # init frame

            # Trigger escalation
            resp = client.post(
                "/api/v1/service-requests/sla/escalate-overdue?threshold_seconds=180",
                headers={
                    "Authorization": f"Bearer {admin_jwt}",
                    "X-Branch-ID": str(branch_b.id),
                },
            )
            assert resp.status_code == 200
            assert resp.json()["escalated_count"] == 1

            # Branch B receives the frame
            frame_b = ws_branch_b.receive_json()
            assert frame_b["event"] == "SERVICE_REQUEST_ESCALATED"
            assert frame_b["data"]["request_id"] == str(overdue_id)

            # Branch A socket should NOT have received this event (can verify via quick ping-pong)
            ws_branch_a.send_text("ping")
            assert ws_branch_a.receive_text() == "pong"


# ---------------------------------------------------------------------------
# Test 6: SLA Escalate Overdue API Endpoint RBAC
# ---------------------------------------------------------------------------

def test_sla_escalate_overdue_endpoint_rbac(
    seed_data: dict[str, Any],
    test_session_factory: async_sessionmaker[AsyncSession],
):
    """Only BRANCH_ADMIN or SUPER_ADMIN may trigger the SLA escalate overdue endpoint."""
    app = create_app()

    async def override_get_db():
        async with test_session_factory() as session:
            yield session

    app.dependency_overrides[get_async_db] = override_get_db

    branch = seed_data["branch_a"]
    waiter = seed_data["waiter"]
    admin = seed_data["admin"]

    waiter_jwt = create_access_token({
        "sub": str(waiter.id),
        "tenant_id": str(waiter.tenant_id),
        "role": waiter.role.value,
    })
    admin_jwt = create_access_token({
        "sub": str(admin.id),
        "tenant_id": str(admin.tenant_id),
        "role": admin.role.value,
    })

    with TestClient(app) as client:
        # Waiter is blocked with 403 Forbidden
        resp_waiter = client.post(
            "/api/v1/service-requests/sla/escalate-overdue",
            headers={
                "Authorization": f"Bearer {waiter_jwt}",
                "X-Branch-ID": str(branch.id),
            },
        )
        assert resp_waiter.status_code == 403

        # Branch Admin succeeds with 200 OK
        resp_admin = client.post(
            "/api/v1/service-requests/sla/escalate-overdue",
            headers={
                "Authorization": f"Bearer {admin_jwt}",
                "X-Branch-ID": str(branch.id),
            },
        )
        assert resp_admin.status_code == 200
        assert resp_admin.json()["status"] == "success"
        assert "escalated_count" in resp_admin.json()


# ---------------------------------------------------------------------------
# Test 7: SLAMonitorWorker Lifecycle and CancelledError Handling
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sla_monitor_worker_lifecycle():
    """Verify SLAMonitorWorker starts, stops, and cleanly catches asyncio.CancelledError."""
    worker = SLAMonitorWorker(interval_seconds=1, threshold_seconds=180)
    assert not worker.is_running

    # Start worker
    await worker.start()
    assert worker.is_running
    assert worker._task is not None

    # Redundant start is a no-op
    await worker.start()
    assert worker.is_running

    # Stop worker gracefully
    await worker.stop()
    assert not worker.is_running
    assert worker._task is None

    # Redundant stop is a safe no-op
    await worker.stop()
    assert not worker.is_running
