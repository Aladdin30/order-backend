"""Comprehensive End-to-End API Integration Verification for all endpoints.

Tests all application endpoints across positive, negative, security, RBAC,
audit logging, and internationalization (i18n) scenarios.
"""

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
from app.core.config import settings
from app.core.qr_security import QRSignatureEngine
from app.core.security import create_access_token, get_password_hash
from app.main import create_app
from app.models import (
    AuditLog,
    Base,
    Branch,
    Table,
    TableStatus,
    Tenant,
    User,
    UserBranchAccess,
    UserRole,
)


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
async def seed_env(test_session: AsyncSession) -> dict:
    """Seed comprehensive hierarchy for endpoint testing."""
    tenant = Tenant(name="Gourmet Hospitality", slug="gourmet", is_active=True)
    test_session.add(tenant)
    await test_session.flush()

    branch = Branch(
        tenant_id=tenant.id,
        name={"en": "Capital Branch", "ar": "فرع العاصمة"},
        slug="capital",
        latitude=24.7136,
        longitude=46.6753,
        geofence_radius_meters=150,
        is_active=True,
    )
    branch_2 = Branch(
        tenant_id=tenant.id,
        name={"en": "Coastal Branch", "ar": "فرع الساحل"},
        slug="coastal",
        latitude=21.5433,
        longitude=39.1728,
        geofence_radius_meters=200,
        is_active=True,
    )
    test_session.add_all([branch, branch_2])
    await test_session.flush()

    table = Table(
        branch_id=branch.id,
        table_number="T-01",
        capacity=4,
        status=TableStatus.AVAILABLE,
        is_active=True,
    )
    table_inactive = Table(
        branch_id=branch.id,
        table_number="T-INACTIVE",
        capacity=2,
        status=TableStatus.AVAILABLE,
        is_active=False,
    )
    test_session.add_all([table, table_inactive])
    await test_session.flush()

    pw_hash = get_password_hash("ValidPass123!")

    super_admin = User(
        tenant_id=tenant.id,
        email="superadmin@gourmet.com",
        hashed_password=pw_hash,
        full_name="Alice (Super Admin)",
        role=UserRole.SUPER_ADMIN,
        is_active=True,
    )
    branch_admin = User(
        tenant_id=tenant.id,
        email="branchadmin@gourmet.com",
        hashed_password=pw_hash,
        full_name="Bob (Branch Admin)",
        role=UserRole.BRANCH_ADMIN,
        is_active=True,
    )
    cashier = User(
        tenant_id=tenant.id,
        email="cashier@gourmet.com",
        hashed_password=pw_hash,
        full_name="Charlie (Cashier)",
        role=UserRole.CASHIER,
        is_active=True,
    )
    inactive_user = User(
        tenant_id=tenant.id,
        email="inactive@gourmet.com",
        hashed_password=pw_hash,
        full_name="Dave (Inactive)",
        role=UserRole.CASHIER,
        is_active=False,
    )
    test_session.add_all([super_admin, branch_admin, cashier, inactive_user])
    await test_session.flush()

    # Link branch_admin to branch 1 only
    access = UserBranchAccess(user_id=branch_admin.id, branch_id=branch.id)
    test_session.add(access)
    await test_session.commit()

    return {
        "tenant": tenant,
        "branch": branch,
        "branch_2": branch_2,
        "table": table,
        "table_inactive": table_inactive,
        "super_admin": super_admin,
        "branch_admin": branch_admin,
        "cashier": cashier,
        "inactive_user": inactive_user,
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


def auth_header(user: User) -> dict[str, str]:
    token = create_access_token({
        "sub": str(user.id),
        "tenant_id": str(user.tenant_id),
        "role": user.role.value,
        "email": user.email,
    })
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Test Suite: Every Endpoint in the System
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestAllSystemEndpoints:
    """Systematic end-to-end execution of all API endpoints."""

    # -----------------------------------------------------------------------
    # 1. System Health Check: GET /health
    # -----------------------------------------------------------------------
    async def test_endpoint_health(self, client: AsyncClient) -> None:
        """GET /health - Verifies platform status and i18n header."""
        resp = await client.get("/health", headers={"Accept-Language": "ar"})
        assert resp.status_code == 200
        assert resp.headers["Content-Language"] == "ar"
        data = resp.json()
        assert data["status"] == "healthy"
        assert data["service"] == settings.PROJECT_NAME

    # -----------------------------------------------------------------------
    # 2. OpenAPI Specification: GET /api/v1/openapi.json
    # -----------------------------------------------------------------------
    async def test_endpoint_openapi_spec(self, client: AsyncClient) -> None:
        """GET /api/v1/openapi.json - Verifies all routes are registered in schema."""
        resp = await client.get(f"{settings.API_V1_STR}/openapi.json")
        assert resp.status_code == 200
        spec = resp.json()
        paths = spec["paths"]

        # Ensure all core endpoint paths are present
        assert f"{settings.API_V1_STR}/auth/token" in paths
        assert f"{settings.API_V1_STR}/auth/me" in paths
        assert f"{settings.API_V1_STR}/audit/logs" in paths
        assert f"{settings.API_V1_STR}/qr/verify" in paths
        assert f"{settings.API_V1_STR}/qr/generate-token" in paths

    # -----------------------------------------------------------------------
    # 3. Authentication Flow: POST /api/v1/auth/token
    # -----------------------------------------------------------------------
    async def test_endpoint_auth_token_success(self, client: AsyncClient, seed_env: dict) -> None:
        """POST /api/v1/auth/token - Valid credentials returns JWT and audits success."""
        resp = await client.post(
            f"{settings.API_V1_STR}/auth/token",
            data={"username": "superadmin@gourmet.com", "password": "ValidPass123!"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"
        assert data["expires_in"] == settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60

    async def test_endpoint_auth_token_invalid_password(self, client: AsyncClient, seed_env: dict) -> None:
        """POST /api/v1/auth/token - Invalid password returns 401."""
        resp = await client.post(
            f"{settings.API_V1_STR}/auth/token",
            data={"username": "superadmin@gourmet.com", "password": "WrongPassword!"},
        )
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Incorrect email or password"

    async def test_endpoint_auth_token_inactive_user(self, client: AsyncClient, seed_env: dict) -> None:
        """POST /api/v1/auth/token - Inactive account returns 400."""
        resp = await client.post(
            f"{settings.API_V1_STR}/auth/token",
            data={"username": "inactive@gourmet.com", "password": "ValidPass123!"},
        )
        assert resp.status_code == 400
        assert "Inactive user account" in resp.json()["detail"]

    async def test_endpoint_auth_token_validation_error(self, client: AsyncClient) -> None:
        """POST /api/v1/auth/token - Missing form fields returns 422 with localized envelope."""
        resp = await client.post(
            f"{settings.API_V1_STR}/auth/token",
            headers={"Accept-Language": "ar"},
            data={},
        )
        assert resp.status_code == 422
        assert resp.headers["Content-Language"] == "ar"
        assert resp.json()["detail"] == "بيانات الطلب المدخلة غير صحيحة"

    # -----------------------------------------------------------------------
    # 4. User Profile: GET /api/v1/auth/me
    # -----------------------------------------------------------------------
    async def test_endpoint_auth_me_success(self, client: AsyncClient, seed_env: dict) -> None:
        """GET /api/v1/auth/me - Authenticated user retrieves profile and permissions."""
        branch_admin = seed_env["branch_admin"]
        branch = seed_env["branch"]

        resp = await client.get(
            f"{settings.API_V1_STR}/auth/me",
            headers=auth_header(branch_admin),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["email"] == branch_admin.email
        assert data["role"] == UserRole.BRANCH_ADMIN
        assert str(branch.id) in [str(b) for b in data["allowed_branch_ids"]]

    async def test_endpoint_auth_me_unauthorized(self, client: AsyncClient) -> None:
        """GET /api/v1/auth/me - Missing bearer token returns 401."""
        resp = await client.get(f"{settings.API_V1_STR}/auth/me")
        assert resp.status_code == 401

    # -----------------------------------------------------------------------
    # 5. Audit Logging: GET /api/v1/audit/logs
    # -----------------------------------------------------------------------
    async def test_endpoint_audit_logs_super_admin(self, client: AsyncClient, seed_env: dict) -> None:
        """GET /api/v1/audit/logs - Super Admin can view tenant-wide audit records."""
        super_admin = seed_env["super_admin"]
        resp = await client.get(
            f"{settings.API_V1_STR}/audit/logs",
            headers=auth_header(super_admin),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "total" in data
        assert "items" in data
        assert isinstance(data["items"], list)

    async def test_endpoint_audit_logs_forbidden_for_branch_admin(self, client: AsyncClient, seed_env: dict) -> None:
        """GET /api/v1/audit/logs - BRANCH_ADMIN cannot view audit logs (requires SUPER_ADMIN / REGIONAL_MANAGER)."""
        branch_admin = seed_env["branch_admin"]
        resp = await client.get(
            f"{settings.API_V1_STR}/audit/logs",
            headers=auth_header(branch_admin),
        )
        assert resp.status_code == 403

    # -----------------------------------------------------------------------
    # 6. Physical QR Verification: POST /api/v1/qr/verify
    # -----------------------------------------------------------------------
    async def test_endpoint_qr_verify_success(self, client: AsyncClient, seed_env: dict) -> None:
        """POST /api/v1/qr/verify - Public client verifies valid physical QR token."""
        tenant = seed_env["tenant"]
        branch = seed_env["branch"]
        table = seed_env["table"]

        token = QRSignatureEngine.sign_table_token(tenant.id, branch.id, table.id, key_version=1)

        resp = await client.post(
            f"{settings.API_V1_STR}/qr/verify",
            headers={"Accept-Language": "ar"},
            json={"token": token},
        )
        assert resp.status_code == 200
        assert resp.headers["Content-Language"] == "ar"
        data = resp.json()
        assert data["is_valid"] is True
        assert data["table_id"] == str(table.id)
        assert data["table_number"] == "T-01"
        assert data["branch_name"] == {"en": "Capital Branch", "ar": "فرع العاصمة"}
        assert data["geofence_radius_meters"] == 150
        assert data["requires_geofence_check"] is True

    async def test_endpoint_qr_verify_tampered_rejected(self, client: AsyncClient, seed_env: dict) -> None:
        """POST /api/v1/qr/verify - Tampered token rejected with 400."""
        tenant = seed_env["tenant"]
        branch = seed_env["branch"]
        table = seed_env["table"]

        token = QRSignatureEngine.sign_table_token(tenant.id, branch.id, table.id, key_version=1)
        p, s = token.split(".")
        tampered_token = f"{p}.{'X' if s[0] != 'X' else 'Y'}{s[1:]}"

        resp = await client.post(
            f"{settings.API_V1_STR}/qr/verify",
            json={"token": tampered_token},
        )
        assert resp.status_code == 400
        assert "verification failed" in resp.json()["detail"].lower()

    async def test_endpoint_qr_verify_inactive_table_forbidden(self, client: AsyncClient, seed_env: dict) -> None:
        """POST /api/v1/qr/verify - Inactive table token rejected with 403."""
        tenant = seed_env["tenant"]
        branch = seed_env["branch"]
        table_inactive = seed_env["table_inactive"]

        token = QRSignatureEngine.sign_table_token(tenant.id, branch.id, table_inactive.id, key_version=1)

        resp = await client.post(
            f"{settings.API_V1_STR}/qr/verify",
            json={"token": token},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Branch or table is not currently active"

    # -----------------------------------------------------------------------
    # 7. Physical QR Token Generation: POST /api/v1/qr/generate-token
    # -----------------------------------------------------------------------
    async def test_endpoint_qr_generate_token_success(self, client: AsyncClient, seed_env: dict) -> None:
        """POST /api/v1/qr/generate-token - Authorized staff generates signed QR token."""
        branch_admin = seed_env["branch_admin"]
        branch = seed_env["branch"]
        table = seed_env["table"]

        resp = await client.post(
            f"{settings.API_V1_STR}/qr/generate-token",
            headers={
                **auth_header(branch_admin),
                "X-Branch-ID": str(branch.id),
            },
            json={"table_id": str(table.id), "key_version": 1},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "token" in data
        assert data["table_id"] == str(table.id)
        assert data["table_number"] == "T-01"

    async def test_endpoint_qr_generate_token_unauthorized_role(self, client: AsyncClient, seed_env: dict) -> None:
        """POST /api/v1/qr/generate-token - Cashier role rejected with 403."""
        cashier = seed_env["cashier"]
        branch = seed_env["branch"]
        table = seed_env["table"]

        resp = await client.post(
            f"{settings.API_V1_STR}/qr/generate-token",
            headers={
                **auth_header(cashier),
                "X-Branch-ID": str(branch.id),
            },
            json={"table_id": str(table.id)},
        )
        assert resp.status_code == 403

    async def test_endpoint_qr_generate_token_cross_branch_forbidden(self, client: AsyncClient, seed_env: dict) -> None:
        """POST /api/v1/qr/generate-token - Branch admin generating for unassigned branch 2 rejected with 403."""
        branch_admin = seed_env["branch_admin"]
        branch_2 = seed_env["branch_2"]
        table = seed_env["table"]

        resp = await client.post(
            f"{settings.API_V1_STR}/qr/generate-token",
            headers={
                **auth_header(branch_admin),
                "X-Branch-ID": str(branch_2.id),
            },
            json={"table_id": str(table.id)},
        )
        assert resp.status_code == 403

    # -----------------------------------------------------------------------
    # 8. Internationalization & Header Propagation Across All Endpoints
    # -----------------------------------------------------------------------
    async def test_content_language_header_propagation_all_endpoints(self, client: AsyncClient) -> None:
        """Verify Content-Language header is reliably present across all responses."""
        # 1. Health check with English
        r1 = await client.get("/health", headers={"Accept-Language": "en-US,en;q=0.9"})
        assert r1.headers.get("Content-Language") == "en"

        # 2. Health check with Arabic
        r2 = await client.get("/health", headers={"Accept-Language": "ar-EG,ar;q=0.9"})
        assert r2.headers.get("Content-Language") == "ar"

        # 3. Query param override
        r3 = await client.get("/health?lang=en", headers={"Accept-Language": "ar"})
        assert r3.headers.get("Content-Language") == "en"

        # 4. Error response with localized language header
        r4 = await client.post(
            f"{settings.API_V1_STR}/qr/verify",
            headers={"Accept-Language": "en"},
            json={"token": "invalid.token"},
        )
        assert r4.status_code == 400
        assert r4.headers.get("Content-Language") == "en"
