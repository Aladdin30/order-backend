"""Comprehensive Test Suite for Task BE-2.1: Table Session & Presence Verification Endpoint.

Validates the Three-Tier Presence Policy, Haversine geodesic calculation,
Table lifecycle transitions, Guest Session JWT issuance, dynamic i18n,
and downstream security dependencies.
"""

from __future__ import annotations

import time
import uuid
from datetime import timedelta
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
from app.core.qr_security import QRSignatureEngine
from app.core.session_security import (
    ExpiredGuestSessionError,
    InvalidGuestSessionError,
    create_guest_session_jwt,
    decode_guest_session_jwt,
)
from app.main import create_app
from app.models.auth import Base, Branch, Table, Tenant
from app.models.enums import TableStatus
from app.schemas.session import TablePresenceVerifyRequest


# ---------------------------------------------------------------------------
# Database & Client Fixtures
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
    """Seed tenant, branches, and tables for presence verification testing."""
    tenant = Tenant(name="Gourmet Dining Co", slug="gourmet-dining", is_active=True)
    tenant_inactive = Tenant(name="Inactive Tenant Co", slug="inactive-tenant", is_active=False)
    test_session.add_all([tenant, tenant_inactive])
    await test_session.flush()

    # Active branch located in Riyadh with 150m geofence radius
    branch = Branch(
        tenant_id=tenant.id,
        name={"en": "Downtown Branch", "ar": "فرع وسط المدينة"},
        slug="downtown",
        latitude=24.7136,
        longitude=46.6753,
        geofence_radius_meters=150,
        is_active=True,
    )
    branch_inactive = Branch(
        tenant_id=tenant.id,
        name={"en": "Closed Branch", "ar": "فرع مغلق"},
        slug="closed",
        latitude=24.7136,
        longitude=46.6753,
        geofence_radius_meters=150,
        is_active=False,
    )
    test_session.add_all([branch, branch_inactive])
    await test_session.flush()

    table_available = Table(
        branch_id=branch.id,
        table_number="T-01",
        capacity=4,
        status=TableStatus.AVAILABLE,
        is_active=True,
    )
    table_browsing = Table(
        branch_id=branch.id,
        table_number="T-02",
        capacity=2,
        status=TableStatus.BROWSING,
        is_active=True,
    )
    table_inactive = Table(
        branch_id=branch.id,
        table_number="T-INACTIVE",
        capacity=4,
        status=TableStatus.AVAILABLE,
        is_active=False,
    )
    table_in_inactive_branch = Table(
        branch_id=branch_inactive.id,
        table_number="T-INACTIVE-BRANCH",
        capacity=4,
        status=TableStatus.AVAILABLE,
        is_active=True,
    )
    test_session.add_all([table_available, table_browsing, table_inactive, table_in_inactive_branch])

    # Table under inactive tenant
    branch_of_inactive_tenant = Branch(
        tenant_id=tenant_inactive.id,
        name={"en": "Inactive Tenant Branch", "ar": "فرع مستأجر معطل"},
        slug="inact-t-branch",
        latitude=24.7136,
        longitude=46.6753,
        geofence_radius_meters=150,
        is_active=True,
    )
    test_session.add(branch_of_inactive_tenant)
    await test_session.flush()

    table_of_inactive_tenant = Table(
        branch_id=branch_of_inactive_tenant.id,
        table_number="T-INACT-TENANT",
        capacity=4,
        status=TableStatus.AVAILABLE,
        is_active=True,
    )
    test_session.add(table_of_inactive_tenant)
    await test_session.commit()

    return {
        "tenant": tenant,
        "tenant_inactive": tenant_inactive,
        "branch": branch,
        "branch_inactive": branch_inactive,
        "table_available": table_available,
        "table_browsing": table_browsing,
        "table_inactive": table_inactive,
        "table_in_inactive_branch": table_in_inactive_branch,
        "table_of_inactive_tenant": table_of_inactive_tenant,
    }


@pytest_asyncio.fixture(scope="function")
async def client(async_test_engine: AsyncEngine) -> AsyncGenerator[AsyncClient, None]:
    session_factory = async_sessionmaker(
        bind=async_test_engine,
        class_=AsyncSession,
        autoflush=False,
        expire_on_commit=False,
    )

    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_async_db] = override_get_db

    with patch("app.core.database.async_session_factory", session_factory), \
         patch("app.services.audit_service.async_session_factory", session_factory):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
            yield ac


# ---------------------------------------------------------------------------
# 1. Haversine Calculation Unit Tests
# ---------------------------------------------------------------------------

class TestHaversineDistance:
    """Unit tests for the server-side Haversine distance calculator."""

    def test_zero_distance_identical_points(self) -> None:
        """Identical coordinates return 0.0 meters."""
        dist = haversine_distance_meters(24.7136, 46.6753, 24.7136, 46.6753)
        assert dist == 0.0

    def test_short_distance_within_geofence(self) -> None:
        """Small offset (~30-50m) calculates accurately."""
        # 0.0003 deg latitude is ~33.3 meters
        dist = haversine_distance_meters(24.7136, 46.6753, 24.7139, 46.6753)
        assert 30.0 <= dist <= 40.0

    def test_large_distance_exceeding_geofence(self) -> None:
        """Distance across cities is calculated accurately in meters."""
        # Riyadh (24.7136, 46.6753) to Jeddah (21.5433, 39.1728) is ~850 km
        dist = haversine_distance_meters(24.7136, 46.6753, 21.5433, 39.1728)
        assert 840_000 <= dist <= 860_000

    def test_invalid_latitude_raises_error(self) -> None:
        """Latitudes exceeding [-90, 90] raise ValueError."""
        with pytest.raises(ValueError, match="Latitude"):
            haversine_distance_meters(95.0, 46.0, 24.0, 46.0)

    def test_invalid_longitude_raises_error(self) -> None:
        """Longitudes exceeding [-180, 180] raise ValueError."""
        with pytest.raises(ValueError, match="Longitude"):
            haversine_distance_meters(24.0, -185.0, 24.0, 46.0)


# ---------------------------------------------------------------------------
# 2. Guest Session JWT Unit Tests
# ---------------------------------------------------------------------------

class TestSessionSecurity:
    """Unit tests for Guest Session JWT generation and claim verification."""

    def test_create_and_decode_valid_guest_session_jwt(self) -> None:
        session_id = uuid.uuid4()
        tenant_id = uuid.uuid4()
        branch_id = uuid.uuid4()
        table_id = uuid.uuid4()

        token = create_guest_session_jwt(
            session_id=session_id,
            tenant_id=tenant_id,
            branch_id=branch_id,
            table_id=table_id,
            table_number="T-01",
            is_presence_verified=True,
        )
        assert isinstance(token, str)

        payload = decode_guest_session_jwt(token)
        assert payload["sub"] == str(session_id)
        assert payload["type"] == "guest_session"
        assert payload["tenant_id"] == str(tenant_id)
        assert payload["branch_id"] == str(branch_id)
        assert payload["table_id"] == str(table_id)
        assert payload["table_number"] == "T-01"
        assert payload["is_presence_verified"] is True
        assert "iat" in payload
        assert "exp" in payload

    def test_decode_expired_token_raises_error(self) -> None:
        session_id = uuid.uuid4()
        tenant_id = uuid.uuid4()
        branch_id = uuid.uuid4()
        table_id = uuid.uuid4()

        token = create_guest_session_jwt(
            session_id=session_id,
            tenant_id=tenant_id,
            branch_id=branch_id,
            table_id=table_id,
            table_number="T-01",
            is_presence_verified=True,
            expires_delta=timedelta(seconds=-10),
        )
        with pytest.raises(ExpiredGuestSessionError, match="expired"):
            decode_guest_session_jwt(token)

    def test_tampered_token_raises_error(self) -> None:
        session_id = uuid.uuid4()
        token = create_guest_session_jwt(
            session_id=session_id,
            tenant_id=uuid.uuid4(),
            branch_id=uuid.uuid4(),
            table_id=uuid.uuid4(),
            table_number="T-01",
            is_presence_verified=True,
        )
        header, payload, sig = token.split(".")
        tampered = f"{header}.{payload}.{sig[:-4]}XXXX"
        with pytest.raises(InvalidGuestSessionError, match="Invalid guest session"):
            decode_guest_session_jwt(tampered)

    def test_wrong_token_type_rejected(self) -> None:
        import jwt as pyjwt
        payload = {
            "sub": str(uuid.uuid4()),
            "type": "access_token",  # not guest_session
            "tenant_id": str(uuid.uuid4()),
            "branch_id": str(uuid.uuid4()),
            "table_id": str(uuid.uuid4()),
            "table_number": "T-01",
            "is_presence_verified": True,
            "iat": int(time.time()),
            "exp": int(time.time() + 3600),
        }
        token = pyjwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
        with pytest.raises(InvalidGuestSessionError, match="expected 'guest_session'"):
            decode_guest_session_jwt(token)


# ---------------------------------------------------------------------------
# 3. Three-Tier Presence & Endpoint Integration Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestTableSessionPresenceEndpoints:
    """End-to-end integration tests for POST /api/v1/sessions/verify-presence."""

    async def test_tier_1_verified_presence_success(
        self, client: AsyncClient, test_session: AsyncSession, seed_data: dict,
    ) -> None:
        """Tier 1: Client within geofence receives verified guest session & table transitions to BROWSING."""
        tenant = seed_data["tenant"]
        branch = seed_data["branch"]
        table = seed_data["table_available"]

        # Generate physical QR token
        qr_token = QRSignatureEngine.sign_table_token(tenant.id, branch.id, table.id, key_version=1)

        # Coordinate within ~30m of branch (24.7136, 46.6753)
        client_lat = 24.7138
        client_lon = 46.6754

        resp = await client.post(
            f"{settings.API_V1_STR}/sessions/verify-presence",
            headers={"Accept-Language": "ar"},
            json={
                "qr_token": qr_token,
                "client_latitude": client_lat,
                "client_longitude": client_lon,
            },
        )
        assert resp.status_code == 200
        data = resp.json()

        # Check response structure
        assert data["table_id"] == str(table.id)
        assert data["table_number"] == "T-01"
        assert data["is_presence_verified"] is True
        assert data["computed_distance_meters"] is not None
        assert data["computed_distance_meters"] <= branch.geofence_radius_meters
        assert data["table_status"] == TableStatus.BROWSING
        assert data["branch_name"] == "فرع وسط المدينة"  # localized to Arabic
        assert "session_token" in data
        assert data["token_type"] == "bearer"
        assert data["expires_in"] == settings.GUEST_SESSION_EXPIRE_MINUTES * 60

        # Verify Table DB state transition
        table_id = table.id
        test_session.expire_all()
        stmt = select(Table).where(Table.id == table_id)
        refreshed = (await test_session.execute(stmt)).scalar_one()
        assert refreshed.status == TableStatus.BROWSING
        assert refreshed.current_session_token == data["session_id"]

    async def test_tier_2_out_of_geofence_rejected(
        self, client: AsyncClient, test_session: AsyncSession, seed_data: dict,
    ) -> None:
        """Tier 2: Client outside geofence is rejected with 403 OUT_OF_GEOFENCE; no session is created."""
        tenant = seed_data["tenant"]
        branch = seed_data["branch"]
        table = seed_data["table_available"]

        qr_token = QRSignatureEngine.sign_table_token(tenant.id, branch.id, table.id, key_version=1)

        # Far coordinates: Jeddah (~850km away from Riyadh)
        resp = await client.post(
            f"{settings.API_V1_STR}/sessions/verify-presence",
            headers={"Accept-Language": "en"},
            json={
                "qr_token": qr_token,
                "client_latitude": 21.5433,
                "client_longitude": 39.1728,
            },
        )
        assert resp.status_code == 403
        assert "outside the permitted branch geofence radius" in resp.json()["detail"]

        # Table state MUST NOT have transitioned
        table_id = table.id
        test_session.expire_all()
        stmt = select(Table).where(Table.id == table_id)
        refreshed = (await test_session.execute(stmt)).scalar_one()
        assert refreshed.status == TableStatus.AVAILABLE
        assert refreshed.current_session_token is None

    async def test_tier_3_soft_fallback_permission_denied(
        self, client: AsyncClient, test_session: AsyncSession, seed_data: dict,
    ) -> None:
        """Tier 3: Client omits GPS coordinates; onboarded with is_presence_verified = False."""
        tenant = seed_data["tenant"]
        branch = seed_data["branch"]
        table = seed_data["table_available"]

        qr_token = QRSignatureEngine.sign_table_token(tenant.id, branch.id, table.id, key_version=1)

        resp = await client.post(
            f"{settings.API_V1_STR}/sessions/verify-presence",
            headers={"Accept-Language": "en"},
            json={
                "qr_token": qr_token,
                # client_latitude and client_longitude are omitted
            },
        )
        assert resp.status_code == 200
        data = resp.json()

        assert data["table_id"] == str(table.id)
        assert data["is_presence_verified"] is False
        assert data["computed_distance_meters"] is None
        assert data["branch_name"] == "Downtown Branch"  # localized to English
        assert data["table_status"] == TableStatus.BROWSING
        assert "session_token" in data

        # Verify DB state: table is set to BROWSING and session token stored
        table_id = table.id
        test_session.expire_all()
        stmt = select(Table).where(Table.id == table_id)
        refreshed = (await test_session.execute(stmt)).scalar_one()
        assert refreshed.status == TableStatus.BROWSING
        assert refreshed.current_session_token == data["session_id"]

    async def test_partial_coordinates_returns_422_validation_error(
        self, client: AsyncClient, seed_data: dict,
    ) -> None:
        """Passing only latitude without longitude raises 422 Unprocessable Entity."""
        tenant = seed_data["tenant"]
        branch = seed_data["branch"]
        table = seed_data["table_available"]

        qr_token = QRSignatureEngine.sign_table_token(tenant.id, branch.id, table.id, key_version=1)

        resp = await client.post(
            f"{settings.API_V1_STR}/sessions/verify-presence",
            json={
                "qr_token": qr_token,
                "client_latitude": 24.7136,
                # missing client_longitude
            },
        )
        assert resp.status_code == 422

    async def test_inactive_table_rejected_with_403_table_inactive(
        self, client: AsyncClient, seed_data: dict,
    ) -> None:
        """Inactive table raises 403 TABLE_INACTIVE (localized)."""
        tenant = seed_data["tenant"]
        branch = seed_data["branch"]
        table_inactive = seed_data["table_inactive"]

        qr_token = QRSignatureEngine.sign_table_token(
            tenant.id, branch.id, table_inactive.id, key_version=1
        )

        resp = await client.post(
            f"{settings.API_V1_STR}/sessions/verify-presence",
            headers={"Accept-Language": "en"},
            json={"qr_token": qr_token},
        )
        assert resp.status_code == 403
        assert "not currently active" in resp.json()["detail"]

    async def test_inactive_branch_rejected_with_403_table_inactive(
        self, client: AsyncClient, seed_data: dict,
    ) -> None:
        """Table in inactive branch raises 403 TABLE_INACTIVE (localized)."""
        tenant = seed_data["tenant"]
        branch_inactive = seed_data["branch_inactive"]
        table = seed_data["table_in_inactive_branch"]

        qr_token = QRSignatureEngine.sign_table_token(
            tenant.id, branch_inactive.id, table.id, key_version=1
        )

        resp = await client.post(
            f"{settings.API_V1_STR}/sessions/verify-presence",
            headers={"Accept-Language": "ar"},
            json={"qr_token": qr_token},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "الطاولة أو الفرع غير متاح حالياً"

    async def test_inactive_tenant_rejected_with_403_table_inactive(
        self, client: AsyncClient, seed_data: dict,
    ) -> None:
        """Table under inactive tenant raises 403 TABLE_INACTIVE (localized)."""
        tenant_inactive = seed_data["tenant_inactive"]
        table = seed_data["table_of_inactive_tenant"]

        qr_token = QRSignatureEngine.sign_table_token(
            tenant_inactive.id, table.branch_id, table.id, key_version=1
        )

        resp = await client.post(
            f"{settings.API_V1_STR}/sessions/verify-presence",
            headers={"Accept-Language": "en"},
            json={"qr_token": qr_token},
        )
        assert resp.status_code == 403
        assert "not currently active" in resp.json()["detail"]

    async def test_tampered_qr_token_rejected_with_400(
        self, client: AsyncClient, seed_data: dict,
    ) -> None:
        """Tampered QR token signature is rejected with 400 Bad Request."""
        tenant = seed_data["tenant"]
        branch = seed_data["branch"]
        table = seed_data["table_available"]

        valid_token = QRSignatureEngine.sign_table_token(
            tenant.id, branch.id, table.id, key_version=1
        )
        payload, sig = valid_token.split(".")
        tampered_sig = f"{'A' if sig[0] != 'A' else 'B'}{sig[1:]}"
        tampered_token = f"{payload}.{tampered_sig}"

        resp = await client.post(
            f"{settings.API_V1_STR}/sessions/verify-presence",
            json={"qr_token": tampered_token},
        )
        assert resp.status_code == 400
        assert "signature" in resp.json()["detail"].lower()

    async def test_table_already_browsing_preserves_status(
        self, client: AsyncClient, test_session: AsyncSession, seed_data: dict,
    ) -> None:
        """If table is already BROWSING, issuing another session maintains BROWSING status."""
        tenant = seed_data["tenant"]
        branch = seed_data["branch"]
        table = seed_data["table_browsing"]

        qr_token = QRSignatureEngine.sign_table_token(tenant.id, branch.id, table.id, key_version=1)

        resp = await client.post(
            f"{settings.API_V1_STR}/sessions/verify-presence",
            json={"qr_token": qr_token},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["table_status"] == TableStatus.BROWSING

        table_id = table.id
        test_session.expire_all()
        stmt = select(Table).where(Table.id == table_id)
        refreshed = (await test_session.execute(stmt)).scalar_one()
        assert refreshed.status == TableStatus.BROWSING
        assert refreshed.current_session_token == data["session_id"]


# ---------------------------------------------------------------------------
# 4. Downstream Security Dependency Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestDownstreamSessionDependencies:
    """Validates get_current_guest_session and RequirePresenceVerified."""

    async def test_guest_session_me_endpoint_success(
        self, client: AsyncClient, seed_data: dict,
    ) -> None:
        """Guest session profile endpoint returns verified claims."""
        tenant = seed_data["tenant"]
        branch = seed_data["branch"]
        table = seed_data["table_available"]

        session_id = uuid.uuid4()
        token = create_guest_session_jwt(
            session_id=session_id,
            tenant_id=tenant.id,
            branch_id=branch.id,
            table_id=table.id,
            table_number="T-01",
            is_presence_verified=True,
        )

        resp = await client.get(
            f"{settings.API_V1_STR}/sessions/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == str(session_id)
        assert data["tenant_id"] == str(tenant.id)
        assert data["branch_id"] == str(branch.id)
        assert data["table_id"] == str(table.id)
        assert data["table_number"] == "T-01"
        assert data["is_presence_verified"] is True

    async def test_guest_session_unauthorized_missing_bearer_token(
        self, client: AsyncClient,
    ) -> None:
        """Accessing guest-protected endpoint without token returns 401."""
        resp = await client.get(f"{settings.API_V1_STR}/sessions/me")
        assert resp.status_code == 401

    async def test_require_presence_verified_allows_tier_1_session(
        self, client: AsyncClient, seed_data: dict,
    ) -> None:
        """Action requiring presence verification succeeds when is_presence_verified=True."""
        tenant = seed_data["tenant"]
        branch = seed_data["branch"]
        table = seed_data["table_available"]

        session_id = uuid.uuid4()
        token = create_guest_session_jwt(
            session_id=session_id,
            tenant_id=tenant.id,
            branch_id=branch.id,
            table_id=table.id,
            table_number="T-01",
            is_presence_verified=True,
        )

        resp = await client.post(
            f"{settings.API_V1_STR}/sessions/verified-action",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "success"

    async def test_require_presence_verified_blocks_tier_3_session(
        self, client: AsyncClient, seed_data: dict,
    ) -> None:
        """Action requiring presence verification raises 403 when is_presence_verified=False."""
        tenant = seed_data["tenant"]
        branch = seed_data["branch"]
        table = seed_data["table_available"]

        session_id = uuid.uuid4()
        token = create_guest_session_jwt(
            session_id=session_id,
            tenant_id=tenant.id,
            branch_id=branch.id,
            table_id=table.id,
            table_number="T-01",
            is_presence_verified=False,  # Tier 3
        )

        resp = await client.post(
            f"{settings.API_V1_STR}/sessions/verified-action",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "PRESENCE_VERIFICATION_REQUIRED"
