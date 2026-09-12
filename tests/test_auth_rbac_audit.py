"""Comprehensive test suite for Authentication, RBAC, RLS, and Audit Logging Engine."""

import datetime
import uuid
from typing import AsyncGenerator
from unittest.mock import patch

import jwt
import pytest
import pytest_asyncio
from fastapi import APIRouter, Depends, Header, HTTPException, status
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.api.deps import (
    EnforceBranchAccess,
    RequireRoles,
    get_async_db,
    get_current_user_context,
)
from app.core.config import settings
from app.core.context import SecurityContext
from app.core.security import (
    create_access_token,
    decode_access_token,
    get_password_hash,
    verify_password,
)
from app.crud.base_scoped import apply_branch_scope, apply_tenancy_filter
from app.main import create_app
from app.models import (
    AuditLog,
    Base,
    Branch,
    Category,
    Item,
    KitchenStation,
    Order,
    OrderStatus,
    OrderType,
    Table,
    TableStatus,
    Tenant,
    User,
    UserBranchAccess,
    UserRole,
)
from app.services.audit_service import AuditLogger


# ---------------------------------------------------------------------------
# Test Database & Application Fixtures
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
async def seed_data(test_session: AsyncSession) -> dict:
    """Seed comprehensive multi-tenant test data: Tenant, Branches, Users with varying roles."""
    tenant = Tenant(
        name="Gourmet Dining Group",
        slug="gourmet-dining",
        is_active=True,
    )
    test_session.add(tenant)
    await test_session.flush()

    branch_1 = Branch(
        tenant_id=tenant.id,
        name={"en": "Downtown Branch", "ar": "فرع وسط المدينة"},
        slug="downtown",
        latitude=24.7135517,
        longitude=46.6752957,
        geofence_radius_meters=150,
        is_active=True,
    )
    branch_2 = Branch(
        tenant_id=tenant.id,
        name={"en": "Uptown Branch", "ar": "فرع أعلى المدينة"},
        slug="uptown",
        latitude=24.7500000,
        longitude=46.7000000,
        geofence_radius_meters=200,
        is_active=True,
    )
    test_session.add_all([branch_1, branch_2])
    await test_session.flush()

    # Password for all test users: 'Secret123!'
    pw_hash = get_password_hash("Secret123!")

    super_admin = User(
        tenant_id=tenant.id,
        email="superadmin@gourmet.com",
        hashed_password=pw_hash,
        full_name="Alice Vance (Super Admin)",
        role=UserRole.SUPER_ADMIN,
        is_active=True,
    )
    regional_manager = User(
        tenant_id=tenant.id,
        email="regional@gourmet.com",
        hashed_password=pw_hash,
        full_name="Bob Miller (Regional Manager)",
        role=UserRole.REGIONAL_MANAGER,
        is_active=True,
    )
    cashier = User(
        tenant_id=tenant.id,
        email="cashier@gourmet.com",
        hashed_password=pw_hash,
        full_name="Charlie Davis (Cashier)",
        role=UserRole.CASHIER,
        is_active=True,
    )
    inactive_user = User(
        tenant_id=tenant.id,
        email="inactive@gourmet.com",
        hashed_password=pw_hash,
        full_name="Inactive Employee",
        role=UserRole.CASHIER,
        is_active=False,
    )
    test_session.add_all([super_admin, regional_manager, cashier, inactive_user])
    await test_session.flush()

    # Assign Regional Manager to Branch 1 only; Cashier to Branch 1 only
    rm_access = UserBranchAccess(user_id=regional_manager.id, branch_id=branch_1.id)
    cashier_access = UserBranchAccess(user_id=cashier.id, branch_id=branch_1.id)
    test_session.add_all([rm_access, cashier_access])
    await test_session.commit()

    return {
        "tenant": tenant,
        "branch_1": branch_1,
        "branch_2": branch_2,
        "super_admin": super_admin,
        "regional_manager": regional_manager,
        "cashier": cashier,
        "inactive_user": inactive_user,
    }


@pytest_asyncio.fixture(scope="function")
async def client(async_test_engine: AsyncEngine) -> AsyncGenerator[AsyncClient, None]:
    """Provide AsyncClient wired to the test engine."""
    session_factory = async_sessionmaker(
        bind=async_test_engine,
        class_=AsyncSession,
        autoflush=False,
        expire_on_commit=False,
    )

    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    # Add test route to verify EnforceBranchAccess and RequireRoles in API context
    app = create_app()
    app.dependency_overrides[get_async_db] = override_get_db

    # Patch database async_session_factory for decoupled AuditLogger calls during tests
    with patch("app.core.database.async_session_factory", session_factory), \
         patch("app.services.audit_service.async_session_factory", session_factory):

        test_router = APIRouter(prefix="/api/v1/test", tags=["test"])

        @test_router.get("/kitchen-only")
        async def kitchen_endpoint(
            context: SecurityContext = Depends(RequireRoles([UserRole.KITCHEN_STAFF, UserRole.SUPER_ADMIN])),
        ):
            return {"message": "kitchen_authorized", "actor": context.user.email}

        @test_router.post("/branch-scoped-mutation")
        async def branch_mutation_endpoint(
            branch_id: uuid.UUID = Depends(EnforceBranchAccess()),
            context: SecurityContext = Depends(get_current_user_context),
        ):
            return {"message": "mutation_success", "branch_id": str(branch_id)}

        app.include_router(test_router)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


# ---------------------------------------------------------------------------
# 1. Cryptographic Primitives: Argon2id & JWT
# ---------------------------------------------------------------------------

def test_argon2id_password_hashing():
    """Verify password hashing adheres to RFC-standard Argon2id format and validates correctly."""
    plain = "P@ssw0rdSecure!"
    hashed = get_password_hash(plain)

    assert hashed.startswith("$argon2id$")
    assert verify_password(plain, hashed) is True
    assert verify_password("WrongPassword!", hashed) is False


def test_jwt_token_creation_and_decoding():
    """Verify JWT encodes claims and decodes faithfully."""
    uid = uuid.uuid4()
    tid = uuid.uuid4()
    claims = {
        "sub": str(uid),
        "tenant_id": str(tid),
        "role": UserRole.REGIONAL_MANAGER.value,
        "email": "manager@restaurant.com",
    }

    token = create_access_token(claims)
    decoded = decode_access_token(token)

    assert decoded["sub"] == str(uid)
    assert decoded["tenant_id"] == str(tid)
    assert decoded["role"] == UserRole.REGIONAL_MANAGER.value
    assert decoded["email"] == "manager@restaurant.com"
    assert "exp" in decoded
    assert "iat" in decoded


def test_jwt_token_expiration():
    """Verify expired JWT tokens raise PyJWTError."""
    claims = {
        "sub": str(uuid.uuid4()),
        "tenant_id": str(uuid.uuid4()),
        "role": UserRole.CASHIER.value,
    }
    expired_token = create_access_token(claims, expires_delta=datetime.timedelta(seconds=-10))

    with pytest.raises(jwt.PyJWTError):
        decode_access_token(expired_token)


def test_jwt_token_tampering_rejected():
    """Verify modified JWT payload signature check fails."""
    token = create_access_token({"sub": "test", "tenant_id": "test", "role": "CASHIER"})
    tampered_token = token[:-5] + "ABCDE"

    with pytest.raises(jwt.PyJWTError):
        decode_access_token(tampered_token)


# ---------------------------------------------------------------------------
# 2. SecurityContext & Hierarchical RBAC Evaluation
# ---------------------------------------------------------------------------

def test_security_context_super_admin_bypass():
    """Verify SUPER_ADMIN has tenant-wide branch access without explicit branch assignment."""
    tid = uuid.uuid4()
    user = User(
        id=uuid.uuid4(),
        tenant_id=tid,
        email="super@brand.com",
        hashed_password="hash",
        full_name="Super Admin",
        role=UserRole.SUPER_ADMIN,
        is_active=True,
    )
    context = SecurityContext(
        user=user,
        tenant_id=tid,
        role=UserRole.SUPER_ADMIN,
        allowed_branch_ids=frozenset(),
    )

    assert context.is_super_admin is True
    random_branch_id = uuid.uuid4()
    assert context.can_access_branch(random_branch_id) is True

    # assert_branch_access does not raise
    context.assert_branch_access(random_branch_id)

    # assert_roles passes for SUPER_ADMIN
    context.assert_roles([UserRole.SUPER_ADMIN, UserRole.CASHIER])


def test_security_context_regional_manager_branch_scoping():
    """Verify REGIONAL_MANAGER is strictly confined to explicitly assigned branch UUIDs."""
    tid = uuid.uuid4()
    b1 = uuid.uuid4()
    b2 = uuid.uuid4()
    b3_unassigned = uuid.uuid4()

    user = User(
        id=uuid.uuid4(),
        tenant_id=tid,
        email="regional@brand.com",
        hashed_password="hash",
        full_name="Regional Manager",
        role=UserRole.REGIONAL_MANAGER,
        is_active=True,
    )
    context = SecurityContext(
        user=user,
        tenant_id=tid,
        role=UserRole.REGIONAL_MANAGER,
        allowed_branch_ids=frozenset([b1, b2]),
    )

    assert context.is_super_admin is False
    assert context.can_access_branch(b1) is True
    assert context.can_access_branch(b2) is True
    assert context.can_access_branch(b3_unassigned) is False

    context.assert_branch_access(b1)

    with pytest.raises(HTTPException) as exc_info:
        context.assert_branch_access(b3_unassigned)
    assert exc_info.value.status_code == status.HTTP_403_FORBIDDEN


def test_security_context_role_enforcement():
    """Verify assert_roles raises 403 when user lacks designated role."""
    tid = uuid.uuid4()
    user = User(
        id=uuid.uuid4(),
        tenant_id=tid,
        email="cashier@brand.com",
        hashed_password="hash",
        full_name="Cashier",
        role=UserRole.CASHIER,
        is_active=True,
    )
    context = SecurityContext(
        user=user,
        tenant_id=tid,
        role=UserRole.CASHIER,
        allowed_branch_ids=frozenset(),
    )

    # Allowed roles includes CASHIER
    context.assert_roles([UserRole.CASHIER, UserRole.BRANCH_ADMIN])

    # Allowed roles excludes CASHIER
    with pytest.raises(HTTPException) as exc_info:
        context.assert_roles([UserRole.SUPER_ADMIN, UserRole.REGIONAL_MANAGER])
    assert exc_info.value.status_code == status.HTTP_403_FORBIDDEN


# ---------------------------------------------------------------------------
# 3. Row-Level Security (RLS) Query Utilities
# ---------------------------------------------------------------------------

def test_apply_tenancy_filter_select():
    """Verify tenancy filter injects WHERE model.tenant_id = :tenant_id."""
    tid = uuid.uuid4()
    stmt = select(Branch)
    scoped_stmt = apply_tenancy_filter(stmt, Branch, tid)

    compiled = str(scoped_stmt.compile())
    assert "branches.tenant_id =" in compiled


def test_apply_branch_scope_super_admin_unrestricted():
    """Verify apply_branch_scope leaves query unrestricted across branches for SUPER_ADMIN."""
    tid = uuid.uuid4()
    super_admin_context = SecurityContext(
        user=User(id=uuid.uuid4(), tenant_id=tid, email="a@a.com", hashed_password="h", full_name="A", role=UserRole.SUPER_ADMIN, is_active=True),
        tenant_id=tid,
        role=UserRole.SUPER_ADMIN,
        allowed_branch_ids=frozenset(),
    )

    stmt = select(Table)
    scoped_stmt = apply_branch_scope(stmt, Table, super_admin_context)
    compiled = str(scoped_stmt.compile())

    assert "WHERE" not in compiled


def test_apply_branch_scope_assigned_branches():
    """Verify apply_branch_scope scopes query to actor's assigned branch UUIDs."""
    tid = uuid.uuid4()
    b1 = uuid.uuid4()
    b2 = uuid.uuid4()

    manager_context = SecurityContext(
        user=User(id=uuid.uuid4(), tenant_id=tid, email="b@b.com", hashed_password="h", full_name="B", role=UserRole.REGIONAL_MANAGER, is_active=True),
        tenant_id=tid,
        role=UserRole.REGIONAL_MANAGER,
        allowed_branch_ids=frozenset([b1, b2]),
    )

    stmt = select(Table)
    scoped_stmt = apply_branch_scope(stmt, Table, manager_context)
    compiled = str(scoped_stmt.compile())

    assert "tables.branch_id IN" in compiled


def test_apply_branch_scope_unauthorized_branch_raises():
    """Verify requesting an unauthorized branch triggers 403 Forbidden."""
    tid = uuid.uuid4()
    b1 = uuid.uuid4()
    b_unauthorized = uuid.uuid4()

    context = SecurityContext(
        user=User(id=uuid.uuid4(), tenant_id=tid, email="c@c.com", hashed_password="h", full_name="C", role=UserRole.CASHIER, is_active=True),
        tenant_id=tid,
        role=UserRole.CASHIER,
        allowed_branch_ids=frozenset([b1]),
    )

    stmt = select(Table)
    with pytest.raises(HTTPException) as exc_info:
        apply_branch_scope(stmt, Table, context, branch_id=b_unauthorized)
    assert exc_info.value.status_code == status.HTTP_403_FORBIDDEN


def test_apply_branch_scope_update_and_delete():
    """Verify apply_branch_scope works on update and delete statements."""
    tid = uuid.uuid4()
    b1 = uuid.uuid4()

    context = SecurityContext(
        user=User(id=uuid.uuid4(), tenant_id=tid, email="c@c.com", hashed_password="h", full_name="C", role=UserRole.BRANCH_ADMIN, is_active=True),
        tenant_id=tid,
        role=UserRole.BRANCH_ADMIN,
        allowed_branch_ids=frozenset([b1]),
    )

    update_stmt = update(Table).values(status=TableStatus.NEEDS_CLEANING)
    scoped_update = apply_branch_scope(update_stmt, Table, context, branch_id=b1)
    assert "tables.branch_id =" in str(scoped_update.compile())

    delete_stmt = delete(Table)
    scoped_delete = apply_branch_scope(delete_stmt, Table, context, branch_id=b1)
    assert "tables.branch_id =" in str(scoped_delete.compile())


# ---------------------------------------------------------------------------
# 4. Async AuditLogger Service Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_audit_logger_writes_record(test_session: AsyncSession, seed_data: dict):
    """Verify decoupled AuditLogger writes AuditLog record without blocking."""
    tenant = seed_data["tenant"]
    super_admin = seed_data["super_admin"]

    with patch("app.services.audit_service.async_session_factory", lambda: test_session):
        entry = await AuditLogger.log(
            tenant_id=tenant.id,
            action="ITEM_PRICE_MODIFIED",
            resource_type="ITEM",
            user_id=super_admin.id,
            actor_role=super_admin.role.value,
            resource_id="item-uuid-123",
            ip_address="192.168.1.100",
            user_agent="PyTest/1.0",
            changes={"old_price": "15.00", "new_price": "18.50"},
            status="SUCCESS",
        )

        assert entry is not None
        assert entry.action == "ITEM_PRICE_MODIFIED"
        assert entry.changes["new_price"] == "18.50"
        assert entry.status == "SUCCESS"


@pytest.mark.asyncio
async def test_audit_logger_failsafe_swallows_errors():
    """Verify AuditLogger catches database exceptions and does not crash business workflows."""
    with patch("app.services.audit_service.async_session_factory", side_effect=RuntimeError("DB Connection Lost")):
        result = await AuditLogger.log(
            tenant_id=uuid.uuid4(),
            action="CRITICAL_EVENT",
            resource_type="SYSTEM",
        )
        assert result is None  # Swallows exception safely


# ---------------------------------------------------------------------------
# 5. FastAPI Endpoints Integration Tests (OAuth2 Token, Me, RBAC, Middleware)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_login_success_and_audit(client: AsyncClient, seed_data: dict, test_session: AsyncSession):
    """Verify /token issues JWT on valid credentials and creates an AUTH_LOGIN_SUCCESS audit log."""
    super_admin = seed_data["super_admin"]

    response = await client.post(
        f"{settings.API_V1_STR}/auth/token",
        data={
            "username": super_admin.email,
            "password": "Secret123!",
        },
    )
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"

    # Verify decoded token
    decoded = decode_access_token(data["access_token"])
    assert decoded["sub"] == str(super_admin.id)
    assert decoded["role"] == UserRole.SUPER_ADMIN.value

    # Verify audit log was recorded
    audit_stmt = select(AuditLog).where(
        AuditLog.tenant_id == super_admin.tenant_id,
        AuditLog.action == "AUTH_LOGIN_SUCCESS",
    )
    audit_res = await test_session.execute(audit_stmt)
    log = audit_res.scalar_one_or_none()
    assert log is not None
    assert log.actor_role == UserRole.SUPER_ADMIN.value


@pytest.mark.asyncio
async def test_login_invalid_password_audits_failure(client: AsyncClient, seed_data: dict, test_session: AsyncSession):
    """Verify /token returns 401 on bad password and creates an AUTH_LOGIN_FAILED audit log."""
    super_admin = seed_data["super_admin"]

    response = await client.post(
        f"{settings.API_V1_STR}/auth/token",
        data={
            "username": super_admin.email,
            "password": "WrongPassword!",
        },
    )
    assert response.status_code == status.HTTP_401_UNAUTHORIZED

    # Verify failure audit log
    audit_stmt = select(AuditLog).where(
        AuditLog.tenant_id == super_admin.tenant_id,
        AuditLog.action == "AUTH_LOGIN_FAILED",
    )
    audit_res = await test_session.execute(audit_stmt)
    log = audit_res.scalar_one_or_none()
    assert log is not None
    assert log.status == "FAILED"


@pytest.mark.asyncio
async def test_read_current_user_profile(client: AsyncClient, seed_data: dict):
    """Verify /me returns active user details and authorized branch IDs."""
    regional_manager = seed_data["regional_manager"]
    branch_1 = seed_data["branch_1"]

    token = create_access_token({
        "sub": str(regional_manager.id),
        "tenant_id": str(regional_manager.tenant_id),
        "role": regional_manager.role.value,
        "email": regional_manager.email,
    })

    response = await client.get(
        f"{settings.API_V1_STR}/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == status.HTTP_200_OK
    user_data = response.json()
    assert user_data["email"] == regional_manager.email
    assert user_data["role"] == UserRole.REGIONAL_MANAGER.value
    assert str(branch_1.id) in user_data["allowed_branch_ids"]


@pytest.mark.asyncio
async def test_require_roles_dependency(client: AsyncClient, seed_data: dict):
    """Verify RequireRoles allows authorized roles and returns 403 Forbidden for unauthorized roles."""
    cashier = seed_data["cashier"]
    super_admin = seed_data["super_admin"]

    cashier_token = create_access_token({
        "sub": str(cashier.id),
        "tenant_id": str(cashier.tenant_id),
        "role": cashier.role.value,
        "email": cashier.email,
    })
    admin_token = create_access_token({
        "sub": str(super_admin.id),
        "tenant_id": str(super_admin.tenant_id),
        "role": super_admin.role.value,
        "email": super_admin.email,
    })

    # Cashier attempts kitchen-only route (requires KITCHEN_STAFF or SUPER_ADMIN)
    res_cashier = await client.get(
        "/api/v1/test/kitchen-only",
        headers={"Authorization": f"Bearer {cashier_token}"},
    )
    assert res_cashier.status_code == status.HTTP_403_FORBIDDEN

    # Super Admin attempts kitchen-only route
    res_admin = await client.get(
        "/api/v1/test/kitchen-only",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res_admin.status_code == status.HTTP_200_OK
    assert res_admin.json()["message"] == "kitchen_authorized"


@pytest.mark.asyncio
async def test_enforce_branch_access_dependency(client: AsyncClient, seed_data: dict):
    """Verify EnforceBranchAccess validates branch presence and user's branch assignment."""
    regional_manager = seed_data["regional_manager"]
    branch_1 = seed_data["branch_1"]  # assigned to regional_manager
    branch_2 = seed_data["branch_2"]  # NOT assigned to regional_manager

    rm_token = create_access_token({
        "sub": str(regional_manager.id),
        "tenant_id": str(regional_manager.tenant_id),
        "role": regional_manager.role.value,
        "email": regional_manager.email,
    })

    # Case 1: Authorized branch
    res_allowed = await client.post(
        "/api/v1/test/branch-scoped-mutation",
        headers={
            "Authorization": f"Bearer {rm_token}",
            "X-Branch-ID": str(branch_1.id),
        },
    )
    assert res_allowed.status_code == status.HTTP_200_OK
    assert res_allowed.json()["branch_id"] == str(branch_1.id)

    # Case 2: Unauthorized branch (cross-branch violation)
    res_forbidden = await client.post(
        "/api/v1/test/branch-scoped-mutation",
        headers={
            "Authorization": f"Bearer {rm_token}",
            "X-Branch-ID": str(branch_2.id),
        },
    )
    assert res_forbidden.status_code == status.HTTP_403_FORBIDDEN

    # Case 3: Missing branch header
    res_missing = await client.post(
        "/api/v1/test/branch-scoped-mutation",
        headers={"Authorization": f"Bearer {rm_token}"},
    )
    assert res_missing.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.asyncio
async def test_audit_logs_query_endpoint(client: AsyncClient, seed_data: dict, test_session: AsyncSession):
    """Verify /api/v1/audit/logs allows SUPER_ADMIN to query logs and blocks unauthorized roles."""
    super_admin = seed_data["super_admin"]
    cashier = seed_data["cashier"]

    # Pre-seed audit log entries
    await AuditLogger.log(
        tenant_id=super_admin.tenant_id,
        action="TEST_ACTION_1",
        resource_type="MENU",
        status="SUCCESS",
    )
    await AuditLogger.log(
        tenant_id=super_admin.tenant_id,
        action="TEST_ACTION_2",
        resource_type="ORDER",
        status="SUCCESS",
    )

    admin_token = create_access_token({
        "sub": str(super_admin.id),
        "tenant_id": str(super_admin.tenant_id),
        "role": super_admin.role.value,
        "email": super_admin.email,
    })
    cashier_token = create_access_token({
        "sub": str(cashier.id),
        "tenant_id": str(cashier.tenant_id),
        "role": cashier.role.value,
        "email": cashier.email,
    })

    # Cashier cannot access audit logs (403)
    res_cashier = await client.get(
        f"{settings.API_V1_STR}/audit/logs",
        headers={"Authorization": f"Bearer {cashier_token}"},
    )
    assert res_cashier.status_code == status.HTTP_403_FORBIDDEN

    # Super admin can query audit logs (200)
    res_admin = await client.get(
        f"{settings.API_V1_STR}/audit/logs",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res_admin.status_code == status.HTTP_200_OK
    data = res_admin.json()
    assert data["total"] >= 2
    assert len(data["items"]) >= 2
