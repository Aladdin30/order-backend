"""Comprehensive test suite for Cryptographic QR Signature Engine, Service, and Endpoints."""

import base64
import datetime
import struct
import uuid
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

from app.api.deps import get_async_db
from app.core.config import settings
from app.core.qr_security import (
    InvalidTokenError,
    QRSignatureEngine,
    TokenTamperedError,
    UnsupportedKeyVersionError,
)
from app.core.security import create_access_token, get_password_hash
from app.main import create_app
from app.models import (
    Base,
    Branch,
    Table,
    TableStatus,
    Tenant,
    User,
    UserBranchAccess,
    UserRole,
)
from app.schemas.qr import QRTokenPayload, QRVerificationResponse
from app.services.qr_service import QRService


# ---------------------------------------------------------------------------
# Database & Test Engine Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture(scope="function")
async def async_test_engine() -> AsyncGenerator[AsyncEngine, None]:
    """Create an isolated in-memory SQLite async engine per test."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        future=True,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture(scope="function")
async def test_session(async_test_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """Provide an AsyncSession linked to the test engine."""
    session_factory = async_sessionmaker(
        bind=async_test_engine,
        class_=AsyncSession,
        autoflush=False,
        expire_on_commit=False,
    )
    async with session_factory() as session:
        yield session


@pytest_asyncio.fixture(scope="function")
async def qr_seed_data(test_session: AsyncSession) -> dict:
    """Seed comprehensive hierarchy: Active/Inactive Tenants, Branches, Tables, and Staff Users."""
    # Active Tenant
    tenant = Tenant(
        name="Artisan Bistro Group",
        slug="artisan-bistro",
        is_active=True,
    )
    # Inactive Tenant
    inactive_tenant = Tenant(
        name="Defunct Eatery Group",
        slug="defunct-eatery",
        is_active=False,
    )
    test_session.add_all([tenant, inactive_tenant])
    await test_session.flush()

    # Active Branch 1
    branch_1 = Branch(
        tenant_id=tenant.id,
        name={"en": "Downtown Branch", "ar": "فرع وسط المدينة"},
        slug="downtown",
        latitude=24.7135517,
        longitude=46.6752957,
        geofence_radius_meters=150,
        is_active=True,
    )
    # Active Branch 2
    branch_2 = Branch(
        tenant_id=tenant.id,
        name={"en": "Seaside Branch", "ar": "فرع الشاطئ"},
        slug="seaside",
        latitude=21.543333,
        longitude=39.172778,
        geofence_radius_meters=200,
        is_active=True,
    )
    # Inactive Branch
    inactive_branch = Branch(
        tenant_id=tenant.id,
        name={"en": "Closed Branch", "ar": "فرع مغلق"},
        slug="closed",
        latitude=24.800000,
        longitude=46.600000,
        geofence_radius_meters=100,
        is_active=False,
    )
    # Branch belonging to inactive tenant
    branch_inactive_tenant = Branch(
        tenant_id=inactive_tenant.id,
        name={"en": "Ghost Branch", "ar": "فرع معطل"},
        slug="ghost",
        latitude=25.000000,
        longitude=47.000000,
        geofence_radius_meters=100,
        is_active=True,
    )
    test_session.add_all([branch_1, branch_2, inactive_branch, branch_inactive_tenant])
    await test_session.flush()

    # Active Table in Branch 1
    table_1 = Table(
        branch_id=branch_1.id,
        table_number="T-01",
        capacity=4,
        status=TableStatus.AVAILABLE,
        is_active=True,
    )
    # Inactive Table in Branch 1
    table_inactive = Table(
        branch_id=branch_1.id,
        table_number="T-INACTIVE",
        capacity=2,
        status=TableStatus.AVAILABLE,
        is_active=False,
    )
    # Table in Inactive Branch
    table_in_inactive_branch = Table(
        branch_id=inactive_branch.id,
        table_number="T-CLOSED-01",
        capacity=6,
        status=TableStatus.AVAILABLE,
        is_active=True,
    )
    # Table in Branch of Inactive Tenant
    table_in_inactive_tenant = Table(
        branch_id=branch_inactive_tenant.id,
        table_number="T-GHOST-01",
        capacity=4,
        status=TableStatus.AVAILABLE,
        is_active=True,
    )
    # Active Table in Branch 2
    table_branch_2 = Table(
        branch_id=branch_2.id,
        table_number="T-B2-01",
        capacity=4,
        status=TableStatus.AVAILABLE,
        is_active=True,
    )
    test_session.add_all([
        table_1,
        table_inactive,
        table_in_inactive_branch,
        table_in_inactive_tenant,
        table_branch_2,
    ])
    await test_session.flush()

    # Staff Users
    pw_hash = get_password_hash("Secret123!")
    super_admin = User(
        tenant_id=tenant.id,
        email="superadmin@artisan.com",
        hashed_password=pw_hash,
        full_name="Super Admin",
        role=UserRole.SUPER_ADMIN,
        is_active=True,
    )
    branch_admin = User(
        tenant_id=tenant.id,
        email="branchadmin@artisan.com",
        hashed_password=pw_hash,
        full_name="Branch 1 Admin",
        role=UserRole.BRANCH_ADMIN,
        is_active=True,
    )
    cashier = User(
        tenant_id=tenant.id,
        email="cashier@artisan.com",
        hashed_password=pw_hash,
        full_name="Branch 1 Cashier",
        role=UserRole.CASHIER,
        is_active=True,
    )
    test_session.add_all([super_admin, branch_admin, cashier])
    await test_session.flush()

    # Assign branch_admin and cashier to branch_1 only
    admin_access = UserBranchAccess(user_id=branch_admin.id, branch_id=branch_1.id)
    cashier_access = UserBranchAccess(user_id=cashier.id, branch_id=branch_1.id)
    test_session.add_all([admin_access, cashier_access])
    await test_session.commit()

    return {
        "tenant": tenant,
        "inactive_tenant": inactive_tenant,
        "branch_1": branch_1,
        "branch_2": branch_2,
        "inactive_branch": inactive_branch,
        "table_1": table_1,
        "table_inactive": table_inactive,
        "table_in_inactive_branch": table_in_inactive_branch,
        "table_in_inactive_tenant": table_in_inactive_tenant,
        "table_branch_2": table_branch_2,
        "super_admin": super_admin,
        "branch_admin": branch_admin,
        "cashier": cashier,
    }


@pytest_asyncio.fixture(scope="function")
async def client(async_test_engine: AsyncEngine) -> AsyncGenerator[AsyncClient, None]:
    """Provide AsyncClient wired to the test SQLite engine and patched session factory."""
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


# Helper: Generate Bearer Header
def make_auth_header(user: User) -> dict[str, str]:
    token = create_access_token({
        "sub": str(user.id),
        "tenant_id": str(user.tenant_id),
        "role": user.role.value,
        "email": user.email,
    })
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 1. Cryptographic Engine Unit Tests
# ---------------------------------------------------------------------------

class TestQRSignatureEngine:
    """Rigorous tests for QRSignatureEngine cryptographic integrity and edge cases."""

    def test_deterministic_signing_and_verification(self) -> None:
        """Ensure signing is deterministic, verifiable, and returns exact typed payload."""
        tenant_id = uuid.uuid4()
        branch_id = uuid.uuid4()
        table_id = uuid.uuid4()
        key_version = 1

        token_1 = QRSignatureEngine.sign_table_token(tenant_id, branch_id, table_id, key_version)
        token_2 = QRSignatureEngine.sign_table_token(tenant_id, branch_id, table_id, key_version)

        assert token_1 == token_2, "Signature must be stateless and deterministic"
        assert "." in token_1, "Token must be dot-separated"

        payload = QRSignatureEngine.decode_and_verify_signature(token_1)
        assert isinstance(payload, QRTokenPayload)
        assert payload.tenant_id == tenant_id
        assert payload.branch_id == branch_id
        assert payload.table_id == table_id
        assert payload.key_version == key_version

    def test_token_compactness_and_url_safety(self) -> None:
        """Validate that token is ultra-compact and adheres to URL-safe characters."""
        tenant_id = uuid.uuid4()
        branch_id = uuid.uuid4()
        table_id = uuid.uuid4()

        token = QRSignatureEngine.sign_table_token(tenant_id, branch_id, table_id, key_version=1)
        payload_b64, sig_b64 = token.split(".")

        # 52 bytes URL-safe base64 is 70 chars, SHA256 is 43 chars -> total ~114
        assert len(token) <= 120, f"Token length {len(token)} exceeds compactness ceiling of 120 chars"
        assert "=" not in token, "Token must not include base64 padding characters"

        # Check URL-safe characters only
        allowed_chars = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.")
        assert set(token).issubset(allowed_chars), "Token contains non-URL-safe characters"

    def test_tamper_table_id_rejected(self) -> None:
        """Modifying physical table_id must immediately fail HMAC verification."""
        tenant_id = uuid.uuid4()
        branch_id = uuid.uuid4()
        table_id_1 = uuid.uuid4()
        table_id_2 = uuid.uuid4()

        # Token generated for table 1
        valid_token = QRSignatureEngine.sign_table_token(tenant_id, branch_id, table_id_1, key_version=1)
        _, sig_b64 = valid_token.split(".")

        # Construct tampered payload replacing table 1 with table 2
        tampered_bytes = tenant_id.bytes + branch_id.bytes + table_id_2.bytes + struct.pack(">I", 1)
        tampered_payload_b64 = base64.urlsafe_b64encode(tampered_bytes).decode("ascii").rstrip("=")
        tampered_token = f"{tampered_payload_b64}.{sig_b64}"

        with pytest.raises(TokenTamperedError):
            QRSignatureEngine.decode_and_verify_signature(tampered_token)

    def test_tamper_branch_id_rejected(self) -> None:
        """Modifying branch_id must fail signature check."""
        tenant_id = uuid.uuid4()
        branch_id_1 = uuid.uuid4()
        branch_id_2 = uuid.uuid4()
        table_id = uuid.uuid4()

        valid_token = QRSignatureEngine.sign_table_token(tenant_id, branch_id_1, table_id, key_version=1)
        _, sig_b64 = valid_token.split(".")

        tampered_bytes = tenant_id.bytes + branch_id_2.bytes + table_id.bytes + struct.pack(">I", 1)
        tampered_payload_b64 = base64.urlsafe_b64encode(tampered_bytes).decode("ascii").rstrip("=")
        tampered_token = f"{tampered_payload_b64}.{sig_b64}"

        with pytest.raises(TokenTamperedError):
            QRSignatureEngine.decode_and_verify_signature(tampered_token)

    def test_tamper_tenant_id_rejected(self) -> None:
        """Modifying tenant_id must fail signature check."""
        tenant_id_1 = uuid.uuid4()
        tenant_id_2 = uuid.uuid4()
        branch_id = uuid.uuid4()
        table_id = uuid.uuid4()

        valid_token = QRSignatureEngine.sign_table_token(tenant_id_1, branch_id, table_id, key_version=1)
        _, sig_b64 = valid_token.split(".")

        tampered_bytes = tenant_id_2.bytes + branch_id.bytes + table_id.bytes + struct.pack(">I", 1)
        tampered_payload_b64 = base64.urlsafe_b64encode(tampered_bytes).decode("ascii").rstrip("=")
        tampered_token = f"{tampered_payload_b64}.{sig_b64}"

        with pytest.raises(TokenTamperedError):
            QRSignatureEngine.decode_and_verify_signature(tampered_token)

    def test_tamper_signature_rejected(self) -> None:
        """Modifying the signature component must fail verification."""
        tenant_id = uuid.uuid4()
        branch_id = uuid.uuid4()
        table_id = uuid.uuid4()

        valid_token = QRSignatureEngine.sign_table_token(tenant_id, branch_id, table_id, key_version=1)
        payload_b64, sig_b64 = valid_token.split(".")

        # Corrupt last character of signature
        flipped_char = "B" if sig_b64[-1] == "A" else "A"
        tampered_sig = sig_b64[:-1] + flipped_char
        tampered_token = f"{payload_b64}.{tampered_sig}"

        with pytest.raises(TokenTamperedError):
            QRSignatureEngine.decode_and_verify_signature(tampered_token)

    def test_malformed_tokens(self) -> None:
        """Malformed structures, bad base64, and truncated bytes must raise InvalidTokenError."""
        with pytest.raises(InvalidTokenError):
            QRSignatureEngine.decode_and_verify_signature("")

        with pytest.raises(InvalidTokenError):
            QRSignatureEngine.decode_and_verify_signature("no_separator_token")

        with pytest.raises(InvalidTokenError):
            QRSignatureEngine.decode_and_verify_signature("part1.part2.part3")

        with pytest.raises(InvalidTokenError):
            QRSignatureEngine.decode_and_verify_signature("!!!bad_b64!!!.invalidsig")

        # Truncated payload: only 20 bytes instead of 52
        short_bytes = uuid.uuid4().bytes + b"1234"
        short_b64 = base64.urlsafe_b64encode(short_bytes).decode("ascii").rstrip("=")
        with pytest.raises(InvalidTokenError):
            QRSignatureEngine.decode_and_verify_signature(f"{short_b64}.somesig")

    def test_key_rotation_and_revocation(self) -> None:
        """Validate multi-key rotation and revocation life cycle."""
        tenant_id = uuid.uuid4()
        branch_id = uuid.uuid4()
        table_id = uuid.uuid4()

        # Register key version 2
        key_v2 = "new_secret_key_rotation_version_2_test_value"
        QRSignatureEngine.register_key(version=2, secret_key=key_v2)

        # Issue token with version 2
        token_v2 = QRSignatureEngine.sign_table_token(tenant_id, branch_id, table_id, key_version=2)
        payload_v2 = QRSignatureEngine.decode_and_verify_signature(token_v2)
        assert payload_v2.key_version == 2

        # Issue token with version 1
        token_v1 = QRSignatureEngine.sign_table_token(tenant_id, branch_id, table_id, key_version=1)
        assert QRSignatureEngine.decode_and_verify_signature(token_v1).key_version == 1

        # Token referencing unsupported key version 99
        unsupported_bytes = tenant_id.bytes + branch_id.bytes + table_id.bytes + struct.pack(">I", 99)
        unsupported_b64 = base64.urlsafe_b64encode(unsupported_bytes).decode("ascii").rstrip("=")
        with pytest.raises(UnsupportedKeyVersionError):
            QRSignatureEngine.decode_and_verify_signature(f"{unsupported_b64}.dummysig")

        # Revoke version 2
        QRSignatureEngine.revoke_key(version=2)
        with pytest.raises(UnsupportedKeyVersionError):
            QRSignatureEngine.decode_and_verify_signature(token_v2)

        # Version 1 still works
        assert QRSignatureEngine.decode_and_verify_signature(token_v1).key_version == 1

    def test_constant_time_comparison_preventing_timing_attacks(self) -> None:
        """Verify that constant-time compare_digest is strictly invoked."""
        tenant_id = uuid.uuid4()
        branch_id = uuid.uuid4()
        table_id = uuid.uuid4()
        token = QRSignatureEngine.sign_table_token(tenant_id, branch_id, table_id, key_version=1)

        with patch("hmac.compare_digest", wraps=base64.hmac.compare_digest if hasattr(base64, "hmac") else None) as mock_cmp:
            import hmac
            with patch.object(hmac, "compare_digest", return_value=True) as mock_compare:
                QRSignatureEngine.decode_and_verify_signature(token)
                assert mock_compare.called, "hmac.compare_digest must be called for constant-time comparison"


# ---------------------------------------------------------------------------
# 2. Database Verification Pipeline Tests (QRService)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestQRService:
    """Test suite for QRService active DB state and isolation checks."""

    async def test_verify_active_table_success(
        self,
        test_session: AsyncSession,
        qr_seed_data: dict,
    ) -> None:
        """Valid active table, branch, and tenant returns complete QRVerificationResponse."""
        table = qr_seed_data["table_1"]
        branch = qr_seed_data["branch_1"]
        tenant = qr_seed_data["tenant"]

        token = QRSignatureEngine.sign_table_token(
            tenant_id=tenant.id,
            branch_id=branch.id,
            table_id=table.id,
            key_version=1,
        )

        response = await QRService.verify_table_qr(test_session, token)
        assert isinstance(response, QRVerificationResponse)
        assert response.is_valid is True
        assert response.tenant_id == tenant.id
        assert response.branch_id == branch.id
        assert response.table_id == table.id
        assert response.table_number == "T-01"
        assert response.branch_name == {"en": "Downtown Branch", "ar": "فرع وسط المدينة"}
        assert float(response.branch_latitude) == pytest.approx(24.7135517)
        assert float(response.branch_longitude) == pytest.approx(46.6752957)
        assert response.geofence_radius_meters == 150
        assert response.requires_geofence_check is True

    async def test_verify_inactive_table_raises_403(
        self,
        test_session: AsyncSession,
        qr_seed_data: dict,
    ) -> None:
        """Inactive table must raise HTTP 403."""
        from fastapi import HTTPException

        table = qr_seed_data["table_inactive"]
        branch = qr_seed_data["branch_1"]
        tenant = qr_seed_data["tenant"]

        token = QRSignatureEngine.sign_table_token(
            tenant_id=tenant.id,
            branch_id=branch.id,
            table_id=table.id,
            key_version=1,
        )

        with pytest.raises(HTTPException) as exc_info:
            await QRService.verify_table_qr(test_session, token)
        assert exc_info.value.status_code == 403
        assert "Branch or table is not currently active" in exc_info.value.detail

    async def test_verify_inactive_branch_raises_403(
        self,
        test_session: AsyncSession,
        qr_seed_data: dict,
    ) -> None:
        """Table in inactive branch must raise HTTP 403."""
        from fastapi import HTTPException

        table = qr_seed_data["table_in_inactive_branch"]
        branch = qr_seed_data["inactive_branch"]
        tenant = qr_seed_data["tenant"]

        token = QRSignatureEngine.sign_table_token(
            tenant_id=tenant.id,
            branch_id=branch.id,
            table_id=table.id,
            key_version=1,
        )

        with pytest.raises(HTTPException) as exc_info:
            await QRService.verify_table_qr(test_session, token)
        assert exc_info.value.status_code == 403
        assert "Branch or table is not currently active" in exc_info.value.detail

    async def test_verify_inactive_tenant_raises_403(
        self,
        test_session: AsyncSession,
        qr_seed_data: dict,
    ) -> None:
        """Table belonging to inactive tenant must raise HTTP 403."""
        from fastapi import HTTPException

        table = qr_seed_data["table_in_inactive_tenant"]
        tenant = qr_seed_data["inactive_tenant"]
        branch = table.branch_id

        token = QRSignatureEngine.sign_table_token(
            tenant_id=tenant.id,
            branch_id=branch,
            table_id=table.id,
            key_version=1,
        )

        with pytest.raises(HTTPException) as exc_info:
            await QRService.verify_table_qr(test_session, token)
        assert exc_info.value.status_code == 403
        assert "Branch or table is not currently active" in exc_info.value.detail

    async def test_verify_nonexistent_table_raises_403(
        self,
        test_session: AsyncSession,
        qr_seed_data: dict,
    ) -> None:
        """Validly signed token referencing non-existent table must raise HTTP 403."""
        from fastapi import HTTPException

        tenant = qr_seed_data["tenant"]
        branch = qr_seed_data["branch_1"]
        ghost_table_id = uuid.uuid4()

        token = QRSignatureEngine.sign_table_token(
            tenant_id=tenant.id,
            branch_id=branch.id,
            table_id=ghost_table_id,
            key_version=1,
        )

        with pytest.raises(HTTPException) as exc_info:
            await QRService.verify_table_qr(test_session, token)
        assert exc_info.value.status_code == 403
        assert "Branch or table is not currently active" in exc_info.value.detail

    async def test_generate_table_token_service(
        self,
        test_session: AsyncSession,
        qr_seed_data: dict,
    ) -> None:
        """Test staff token generation in QRService."""
        tenant = qr_seed_data["tenant"]
        branch = qr_seed_data["branch_1"]
        table = qr_seed_data["table_1"]

        res = await QRService.generate_table_token(
            db=test_session,
            tenant_id=tenant.id,
            branch_id=branch.id,
            table_id=table.id,
            key_version=1,
        )
        assert res.table_id == table.id
        assert res.table_number == "T-01"
        assert res.branch_id == branch.id
        assert res.tenant_id == tenant.id
        assert "." in res.token


# ---------------------------------------------------------------------------
# 3. API Endpoints Integration Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestQREndpoints:
    """API integration tests for /api/v1/qr routes."""

    async def test_public_verify_endpoint_success(
        self,
        client: AsyncClient,
        test_session: AsyncSession,
        qr_seed_data: dict,
    ) -> None:
        """Public client without authentication can verify physical QR token."""
        tenant = qr_seed_data["tenant"]
        branch = qr_seed_data["branch_1"]
        table = qr_seed_data["table_1"]

        token = QRSignatureEngine.sign_table_token(tenant.id, branch.id, table.id, key_version=1)

        resp = await client.post(
            "/api/v1/qr/verify",
            json={"token": token},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_valid"] is True
        assert data["table_id"] == str(table.id)
        assert data["table_number"] == "T-01"
        assert data["branch_id"] == str(branch.id)
        assert data["branch_name"] == {"en": "Downtown Branch", "ar": "فرع وسط المدينة"}
        assert data["geofence_radius_meters"] == 150
        assert data["requires_geofence_check"] is True

    async def test_public_verify_tampered_token_returns_400(
        self,
        client: AsyncClient,
        qr_seed_data: dict,
    ) -> None:
        """Tampered cryptographic token returns HTTP 400 Bad Request."""
        tenant = qr_seed_data["tenant"]
        branch = qr_seed_data["branch_1"]
        table = qr_seed_data["table_1"]

        valid_token = QRSignatureEngine.sign_table_token(tenant.id, branch.id, table.id, key_version=1)
        payload_b64, sig_b64 = valid_token.split(".")
        tampered_sig = ("B" if sig_b64[0] == "A" else "A") + sig_b64[1:]
        tampered_token = f"{payload_b64}.{tampered_sig}"

        resp = await client.post(
            "/api/v1/qr/verify",
            json={"token": tampered_token},
        )
        assert resp.status_code == 400
        assert "verification failed" in resp.json()["detail"].lower()

    async def test_public_verify_inactive_table_returns_403(
        self,
        client: AsyncClient,
        qr_seed_data: dict,
    ) -> None:
        """Token for inactive table returns HTTP 403 Forbidden."""
        tenant = qr_seed_data["tenant"]
        branch = qr_seed_data["branch_1"]
        table = qr_seed_data["table_inactive"]

        token = QRSignatureEngine.sign_table_token(tenant.id, branch.id, table.id, key_version=1)

        resp = await client.post(
            "/api/v1/qr/verify",
            json={"token": token},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Branch or table is not currently active"

    async def test_generate_token_unauthenticated_returns_401(
        self,
        client: AsyncClient,
        qr_seed_data: dict,
    ) -> None:
        """Attempting to generate QR token without credentials returns 401."""
        table = qr_seed_data["table_1"]
        branch = qr_seed_data["branch_1"]

        resp = await client.post(
            "/api/v1/qr/generate-token",
            headers={"X-Branch-ID": str(branch.id)},
            json={"table_id": str(table.id)},
        )
        assert resp.status_code == 401

    async def test_generate_token_unauthorized_role_returns_403(
        self,
        client: AsyncClient,
        qr_seed_data: dict,
    ) -> None:
        """Cashier role cannot generate table QR tokens (restricted to SUPER_ADMIN, BRANCH_ADMIN)."""
        cashier = qr_seed_data["cashier"]
        table = qr_seed_data["table_1"]
        branch = qr_seed_data["branch_1"]

        resp = await client.post(
            "/api/v1/qr/generate-token",
            headers={
                **make_auth_header(cashier),
                "X-Branch-ID": str(branch.id),
            },
            json={"table_id": str(table.id)},
        )
        assert resp.status_code == 403
        assert "not authorized" in resp.json()["detail"].lower()

    async def test_generate_token_unauthorized_branch_returns_403(
        self,
        client: AsyncClient,
        qr_seed_data: dict,
    ) -> None:
        """Branch Admin for Branch 1 cannot generate tokens for Branch 2."""
        branch_admin = qr_seed_data["branch_admin"]
        branch_2 = qr_seed_data["branch_2"]
        table_b2 = qr_seed_data["table_branch_2"]

        resp = await client.post(
            "/api/v1/qr/generate-token",
            headers={
                **make_auth_header(branch_admin),
                "X-Branch-ID": str(branch_2.id),
            },
            json={"table_id": str(table_b2.id)},
        )
        assert resp.status_code == 403
        assert "forbidden" in resp.json()["detail"].lower()

    async def test_generate_token_success_and_verify_roundtrip(
        self,
        client: AsyncClient,
        qr_seed_data: dict,
    ) -> None:
        """Authorized Branch Admin generates token; guest verifies it successfully."""
        branch_admin = qr_seed_data["branch_admin"]
        branch = qr_seed_data["branch_1"]
        table = qr_seed_data["table_1"]

        # Step 1: Staff generates token
        resp = await client.post(
            "/api/v1/qr/generate-token",
            headers={
                **make_auth_header(branch_admin),
                "X-Branch-ID": str(branch.id),
            },
            json={"table_id": str(table.id), "key_version": 1},
        )
        assert resp.status_code == 200
        gen_data = resp.json()
        generated_token = gen_data["token"]
        assert gen_data["table_number"] == "T-01"

        # Step 2: Guest scans and verifies token on public endpoint
        verify_resp = await client.post(
            "/api/v1/qr/verify",
            json={"token": generated_token},
        )
        assert verify_resp.status_code == 200
        verify_data = verify_resp.json()
        assert verify_data["is_valid"] is True
        assert verify_data["table_id"] == str(table.id)
        assert verify_data["branch_id"] == str(branch.id)
        assert verify_data["table_number"] == "T-01"
        assert verify_data["geofence_radius_meters"] == 150

    async def test_generate_token_via_query_params_and_super_admin(
        self,
        client: AsyncClient,
        qr_seed_data: dict,
    ) -> None:
        """Super Admin can generate token for any branch using query parameters."""
        super_admin = qr_seed_data["super_admin"]
        branch_2 = qr_seed_data["branch_2"]
        table_b2 = qr_seed_data["table_branch_2"]

        resp = await client.post(
            f"/api/v1/qr/generate-token?table_id={table_b2.id}&branch_id={branch_2.id}",
            headers=make_auth_header(super_admin),
        )
        assert resp.status_code == 200
        gen_data = resp.json()
        assert gen_data["table_number"] == "T-B2-01"
        assert gen_data["branch_id"] == str(branch_2.id)
