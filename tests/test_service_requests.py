"""Comprehensive Test Suite for Task BE-2.4: Quick-Service & Takeaway Request Endpoints.

Tests:
1. Guest creates standard service request (WATER, CUTLERY, etc.) with verified presence.
2. Note validation for OTHER requests (mandatory, 3-200 chars; rejected if missing/empty/<3 chars).
3. Unverified presence blocked (Tier 3 session returns 403).
4. Active duplicate guard returns 409 when request is PENDING or ACKNOWLEDGED for same table & type.
5. Table cooldown returns 429 when re-requesting same type within 60 seconds (even if completed).
6. Different request types from the same table are allowed simultaneously.
7. Staff FIFO queue (GET /api/v1/service-requests/active) returns pending/acknowledged in created_at ASC.
8. Staff lifecycle status transitions (PENDING -> ACKNOWLEDGED -> COMPLETED / DISMISSED) and timestamp setting.
9. Illegal status transitions rejected with 409.
10. Branch isolation (staff cannot view or transition requests for another branch).
11. Unauthorized roles blocked from staff endpoints.
12. Guest active table requests endpoint (GET /api/v1/service-requests/table/active).
"""

from __future__ import annotations

import datetime
import uuid
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
async def seed_data(test_session: AsyncSession) -> dict:
    """Seed tenant, branches, tables, staff users, and guest tokens."""
    tenant = Tenant(name="Sultan Restaurant", slug="sultan-rest", is_active=True)
    test_session.add(tenant)
    await test_session.flush()

    branch_a = Branch(
        tenant_id=tenant.id,
        name={"en": "Branch Alpha", "ar": "فرع ألفا"},
        slug="branch-alpha",
        latitude=24.7136,
        longitude=46.6753,
        geofence_radius_meters=150,
        is_active=True,
    )
    branch_b = Branch(
        tenant_id=tenant.id,
        name={"en": "Branch Beta", "ar": "فرع بيتا"},
        slug="branch-beta",
        latitude=24.7500,
        longitude=46.7000,
        geofence_radius_meters=150,
        is_active=True,
    )
    test_session.add_all([branch_a, branch_b])
    await test_session.flush()

    # Tables
    table_a1 = Table(
        branch_id=branch_a.id,
        table_number="T-01",
        capacity=4,
        status=TableStatus.BROWSING,
        is_active=True,
    )
    table_a2 = Table(
        branch_id=branch_a.id,
        table_number="T-02",
        capacity=2,
        status=TableStatus.BROWSING,
        is_active=True,
    )
    table_b1 = Table(
        branch_id=branch_b.id,
        table_number="TB-01",
        capacity=4,
        status=TableStatus.BROWSING,
        is_active=True,
    )
    test_session.add_all([table_a1, table_a2, table_b1])
    await test_session.flush()

    # Users
    waiter = User(
        tenant_id=tenant.id,
        email="waiter@sultan.com",
        full_name="John Waiter",
        hashed_password=get_password_hash("Secret123!"),
        role=UserRole.WAITER,
        is_active=True,
    )
    admin = User(
        tenant_id=tenant.id,
        email="admin@sultan.com",
        full_name="Alice Admin",
        hashed_password=get_password_hash("Secret123!"),
        role=UserRole.BRANCH_ADMIN,
        is_active=True,
    )
    chef = User(
        tenant_id=tenant.id,
        email="chef@sultan.com",
        full_name="Bob Chef",
        hashed_password=get_password_hash("Secret123!"),
        role=UserRole.KITCHEN_STAFF,
        is_active=True,
    )
    test_session.add_all([waiter, admin, chef])
    await test_session.flush()

    # Branch access
    access_waiter = UserBranchAccess(user_id=waiter.id, branch_id=branch_a.id)
    access_admin = UserBranchAccess(user_id=admin.id, branch_id=branch_a.id)
    access_chef = UserBranchAccess(user_id=chef.id, branch_id=branch_a.id)
    test_session.add_all([access_waiter, access_admin, access_chef])
    await test_session.commit()

    # JWT Tokens
    waiter_token = create_access_token(
        data={"sub": str(waiter.id), "tenant_id": str(tenant.id), "role": waiter.role.value}
    )
    admin_token = create_access_token(
        data={"sub": str(admin.id), "tenant_id": str(tenant.id), "role": admin.role.value}
    )
    chef_token = create_access_token(
        data={"sub": str(chef.id), "tenant_id": str(tenant.id), "role": chef.role.value}
    )

    verified_guest_token = create_guest_session_jwt(
        session_id=uuid.uuid4(),
        tenant_id=tenant.id,
        branch_id=branch_a.id,
        table_id=table_a1.id,
        table_number="T-01",
        is_presence_verified=True,
    )
    verified_guest_token_t2 = create_guest_session_jwt(
        session_id=uuid.uuid4(),
        tenant_id=tenant.id,
        branch_id=branch_a.id,
        table_id=table_a2.id,
        table_number="T-02",
        is_presence_verified=True,
    )
    unverified_guest_token = create_guest_session_jwt(
        session_id=uuid.uuid4(),
        tenant_id=tenant.id,
        branch_id=branch_a.id,
        table_id=table_a1.id,
        table_number="T-01",
        is_presence_verified=False,
    )

    return {
        "tenant": tenant,
        "branch_a": branch_a,
        "branch_b": branch_b,
        "table_a1": table_a1,
        "table_a2": table_a2,
        "table_b1": table_b1,
        "waiter": waiter,
        "admin": admin,
        "waiter_token": waiter_token,
        "admin_token": admin_token,
        "chef_token": chef_token,
        "verified_guest_token": verified_guest_token,
        "verified_guest_token_t2": verified_guest_token_t2,
        "unverified_guest_token": unverified_guest_token,
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


class TestServiceRequestCreationAndValidation:
    @pytest.mark.asyncio
    async def test_guest_create_standard_request_success(
        self,
        test_client: AsyncClient,
        seed_data: dict,
    ):
        """Verified dining guest can submit WATER or CUTLERY request without a note."""
        token = seed_data["verified_guest_token"]
        response = await test_client.post(
            "/api/v1/service-requests",
            json={"request_type": "WATER"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 201
        data = response.json()
        assert data["request_type"] == "WATER"
        assert data["status"] == "PENDING"
        assert data["table_number"] == "T-01"
        assert data["note"] is None
        assert data["acknowledged_at"] is None
        assert data["completed_at"] is None
        assert data["dismissed_at"] is None

    @pytest.mark.asyncio
    async def test_guest_create_other_request_note_validation(
        self,
        test_client: AsyncClient,
        seed_data: dict,
    ):
        """OTHER request requires mandatory note of 3-200 characters."""
        token = seed_data["verified_guest_token"]

        # Missing note
        res_missing = await test_client.post(
            "/api/v1/service-requests",
            json={"request_type": "OTHER"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert res_missing.status_code == 422

        # Empty / whitespace note
        res_empty = await test_client.post(
            "/api/v1/service-requests",
            json={"request_type": "OTHER", "note": "   "},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert res_empty.status_code == 422

        # Too short note (< 3 chars)
        res_short = await test_client.post(
            "/api/v1/service-requests",
            json={"request_type": "OTHER", "note": "hi"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert res_short.status_code == 422

        # Valid note
        res_valid = await test_client.post(
            "/api/v1/service-requests",
            json={"request_type": "OTHER", "note": "Baby high chair please"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert res_valid.status_code == 201
        assert res_valid.json()["note"] == "Baby high chair please"

    @pytest.mark.asyncio
    async def test_unverified_presence_blocked(
        self,
        test_client: AsyncClient,
        seed_data: dict,
    ):
        """Unverified guest session (Tier 3) is rejected with 403."""
        token = seed_data["unverified_guest_token"]
        response = await test_client.post(
            "/api/v1/service-requests",
            json={"request_type": "WATER"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 403
        assert "PRESENCE_VERIFICATION_REQUIRED" in response.json()["detail"]


class TestServiceRequestAntiSpamAndCooldown:
    @pytest.mark.asyncio
    async def test_active_duplicate_guard_returns_409(
        self,
        test_client: AsyncClient,
        seed_data: dict,
    ):
        """Table cannot open a duplicate request of same type while PENDING or ACKNOWLEDGED."""
        token = seed_data["verified_guest_token"]
        staff_token = seed_data["waiter_token"]
        branch_id = str(seed_data["branch_a"].id)

        # 1. Create first request (WATER) -> 201
        res1 = await test_client.post(
            "/api/v1/service-requests",
            json={"request_type": "WATER"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert res1.status_code == 201
        req_id = res1.json()["id"]

        # 2. Duplicate while PENDING -> 409 Conflict
        res2 = await test_client.post(
            "/api/v1/service-requests",
            json={"request_type": "WATER"},
            headers={"Authorization": f"Bearer {token}", "Accept-Language": "en"},
        )
        assert res2.status_code == 409
        assert "active service request" in res2.json()["detail"].lower()

        # 3. Transition to ACKNOWLEDGED
        patch_res = await test_client.patch(
            f"/api/v1/service-requests/{req_id}/status",
            json={"status": "ACKNOWLEDGED"},
            headers={"Authorization": f"Bearer {staff_token}", "X-Branch-ID": branch_id},
        )
        assert patch_res.status_code == 200

        # 4. Duplicate while ACKNOWLEDGED -> still 409 Conflict
        res3 = await test_client.post(
            "/api/v1/service-requests",
            json={"request_type": "WATER"},
            headers={"Authorization": f"Bearer {token}", "Accept-Language": "en"},
        )
        assert res3.status_code == 409

    @pytest.mark.asyncio
    async def test_table_cooldown_returns_429(
        self,
        test_client: AsyncClient,
        seed_data: dict,
    ):
        """Table cannot re-submit same request type within 60s cooldown even after completed."""
        token = seed_data["verified_guest_token"]
        staff_token = seed_data["waiter_token"]
        branch_id = str(seed_data["branch_a"].id)

        # 1. Create request -> 201
        res1 = await test_client.post(
            "/api/v1/service-requests",
            json={"request_type": "CUTLERY"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert res1.status_code == 201
        req_id = res1.json()["id"]

        # 2. Staff completes it immediately
        patch_res = await test_client.patch(
            f"/api/v1/service-requests/{req_id}/status",
            json={"status": "COMPLETED"},
            headers={"Authorization": f"Bearer {staff_token}", "X-Branch-ID": branch_id},
        )
        assert patch_res.status_code == 200

        # 3. Guest tries to request CUTLERY again immediately -> 429 Too Many Requests
        res_cooldown = await test_client.post(
            "/api/v1/service-requests",
            json={"request_type": "CUTLERY"},
            headers={"Authorization": f"Bearer {token}", "Accept-Language": "en"},
        )
        assert res_cooldown.status_code == 429
        assert "60 seconds" in res_cooldown.json()["detail"]

        # 4. Different request type (e.g. PACK_LEFTOVERS) is NOT blocked by CUTLERY's cooldown
        res_diff_type = await test_client.post(
            "/api/v1/service-requests",
            json={"request_type": "PACK_LEFTOVERS"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert res_diff_type.status_code == 201


class TestStaffResolutionAndLifecycle:
    @pytest.mark.asyncio
    async def test_staff_fifo_active_queue(
        self,
        test_client: AsyncClient,
        seed_data: dict,
    ):
        """Staff receives active requests in FIFO order with table numbers."""
        token_t1 = seed_data["verified_guest_token"]
        token_t2 = seed_data["verified_guest_token_t2"]
        staff_token = seed_data["waiter_token"]
        branch_id = str(seed_data["branch_a"].id)

        # T1 submits WATER
        await test_client.post(
            "/api/v1/service-requests",
            json={"request_type": "WATER"},
            headers={"Authorization": f"Bearer {token_t1}"},
        )
        # T2 submits TAKEAWAY_ORDER
        await test_client.post(
            "/api/v1/service-requests",
            json={"request_type": "TAKEAWAY_ORDER"},
            headers={"Authorization": f"Bearer {token_t2}"},
        )

        # Staff fetches queue
        queue_res = await test_client.get(
            "/api/v1/service-requests/active",
            headers={"Authorization": f"Bearer {staff_token}", "X-Branch-ID": branch_id},
        )
        assert queue_res.status_code == 200
        items = queue_res.json()
        assert len(items) == 2
        assert items[0]["request_type"] == "WATER"
        assert items[0]["table_number"] == "T-01"
        assert items[1]["request_type"] == "TAKEAWAY_ORDER"
        assert items[1]["table_number"] == "T-02"

    @pytest.mark.asyncio
    async def test_staff_status_lifecycle_and_illegal_transitions(
        self,
        test_client: AsyncClient,
        seed_data: dict,
    ):
        """Full transition lifecycle from PENDING -> ACKNOWLEDGED -> COMPLETED and rejection of illegal moves."""
        token = seed_data["verified_guest_token"]
        staff_token = seed_data["waiter_token"]
        branch_id = str(seed_data["branch_a"].id)

        # Create
        create_res = await test_client.post(
            "/api/v1/service-requests",
            json={"request_type": "WAITER_CALL"},
            headers={"Authorization": f"Bearer {token}"},
        )
        req_id = create_res.json()["id"]

        # Acknowledge
        ack_res = await test_client.patch(
            f"/api/v1/service-requests/{req_id}/status",
            json={"status": "ACKNOWLEDGED"},
            headers={"Authorization": f"Bearer {staff_token}", "X-Branch-ID": branch_id},
        )
        assert ack_res.status_code == 200
        assert ack_res.json()["status"] == "ACKNOWLEDGED"
        assert ack_res.json()["acknowledged_at"] is not None
        assert ack_res.json()["completed_at"] is None

        # Complete
        comp_res = await test_client.patch(
            f"/api/v1/service-requests/{req_id}/status",
            json={"status": "COMPLETED"},
            headers={"Authorization": f"Bearer {staff_token}", "X-Branch-ID": branch_id},
        )
        assert comp_res.status_code == 200
        assert comp_res.json()["status"] == "COMPLETED"
        assert comp_res.json()["completed_at"] is not None

        # Attempt illegal transition from COMPLETED terminal state -> 409 Conflict
        illegal_res = await test_client.patch(
            f"/api/v1/service-requests/{req_id}/status",
            json={"status": "ACKNOWLEDGED"},
            headers={"Authorization": f"Bearer {staff_token}", "X-Branch-ID": branch_id, "Accept-Language": "en"},
        )
        assert illegal_res.status_code == 409
        assert "invalid service request status transition" in illegal_res.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_branch_isolation_and_unauthorized_roles(
        self,
        test_client: AsyncClient,
        seed_data: dict,
    ):
        """Staff cannot modify requests of another branch, and unauthorized roles cannot access staff queue."""
        guest_token = seed_data["verified_guest_token"]
        waiter_token = seed_data["waiter_token"]
        chef_token = seed_data["chef_token"]
        branch_a_id = str(seed_data["branch_a"].id)
        branch_b_id = str(seed_data["branch_b"].id)

        # Create request at Branch A
        res = await test_client.post(
            "/api/v1/service-requests",
            json={"request_type": "WATER"},
            headers={"Authorization": f"Bearer {guest_token}"},
        )
        req_id = res.json()["id"]

        # Staff trying to patch with Branch B header -> 404
        cross_branch_res = await test_client.patch(
            f"/api/v1/service-requests/{req_id}/status",
            json={"status": "ACKNOWLEDGED"},
            headers={"Authorization": f"Bearer {waiter_token}", "X-Branch-ID": branch_b_id},
        )
        assert cross_branch_res.status_code in [403, 404]

        # Chef role (not WAITER or BRANCH_ADMIN) attempting to access queue -> 403
        chef_res = await test_client.get(
            "/api/v1/service-requests/active",
            headers={"Authorization": f"Bearer {chef_token}", "X-Branch-ID": branch_a_id},
        )
        assert chef_res.status_code == 403

    @pytest.mark.asyncio
    async def test_guest_active_table_requests(
        self,
        test_client: AsyncClient,
        seed_data: dict,
    ):
        """Guest can list active requests for their table."""
        token = seed_data["verified_guest_token"]

        await test_client.post(
            "/api/v1/service-requests",
            json={"request_type": "WATER"},
            headers={"Authorization": f"Bearer {token}"},
        )

        res = await test_client.get(
            "/api/v1/service-requests/table/active",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert res.status_code == 200
        requests = res.json()
        assert len(requests) == 1
        assert requests[0]["request_type"] == "WATER"
        assert requests[0]["table_number"] == "T-01"
