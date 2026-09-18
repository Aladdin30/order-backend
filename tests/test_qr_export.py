"""Automated test suite for Task BE-4.4: Dynamic QR Batch Generation & Single Table Export Engine."""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from datetime import datetime, timezone
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
from app.api.deps import get_async_db, get_db
from app.core.security import create_access_token, get_password_hash
from app.main import create_app
from app.models.auth import Base, Branch, Table, Tenant, User, UserBranchAccess
from app.models.enums import TableStatus, UserRole
from app.models.financials import FinancialBase
from app.models.table import TableSession
from app.services.qr_generator_service import QRGeneratorService


@pytest_asyncio.fixture(scope="function")
async def async_test_engine() -> AsyncGenerator[AsyncEngine, None]:
    """In-memory SQLite async engine with all domain, floor, and financial tables created."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(TableSession.metadata.create_all)
        await conn.run_sync(FinancialBase.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(FinancialBase.metadata.drop_all)
        await conn.run_sync(TableSession.metadata.drop_all)
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
    session_factory = async_sessionmaker(bind=async_test_engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_async_db() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_async_db] = override_get_async_db
    app.dependency_overrides[get_db] = override_get_async_db

    transport = ASGITransport(app=app)
    with patch_engine_factories(session_factory):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client

    app.dependency_overrides.clear()


def patch_engine_factories(session_factory):
    from unittest.mock import patch
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(patch("app.core.database.async_session_factory", session_factory))
    stack.enter_context(patch("app.api.deps.async_session_factory", session_factory))
    return stack


@pytest_asyncio.fixture(scope="function")
async def seed_qr_export_data(test_session: AsyncSession) -> dict:
    """Seed tenant, two branches, users with distinct roles, and tables."""
    # 1. Tenant & Branches
    tenant = Tenant(
        id=uuid.uuid4(),
        name="Artisan Dining Co",
        slug="artisan-dining",
        is_active=True,
    )
    test_session.add(tenant)
    await test_session.flush()

    branch_1 = Branch(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        name={"en": "Branch Downtown", "ar": "فرع وسط البلد"},
        slug="branch-downtown",
        latitude=Decimal("24.7136"),
        longitude=Decimal("46.6753"),
        geofence_radius_meters=300,
        is_active=True,
    )
    branch_2 = Branch(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        name={"en": "Branch Uptown", "ar": "فرع شمال المدينة"},
        slug="branch-uptown",
        latitude=Decimal("24.7200"),
        longitude=Decimal("46.6800"),
        geofence_radius_meters=300,
        is_active=True,
    )
    test_session.add_all([branch_1, branch_2])
    await test_session.flush()

    # 2. Users (Super Admin, Branch Admin, Cashier, Waiter)
    pw = get_password_hash("Password123!")

    super_admin = User(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        email="super@artisan.com",
        hashed_password=pw,
        full_name="Super Administrator",
        role=UserRole.SUPER_ADMIN,
        is_active=True,
    )
    branch_admin = User(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        email="admin@downtown.com",
        hashed_password=pw,
        full_name="Downtown Admin",
        role=UserRole.BRANCH_ADMIN,
        is_active=True,
    )
    cashier = User(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        email="cashier@downtown.com",
        hashed_password=pw,
        full_name="Downtown Cashier",
        role=UserRole.CASHIER,
        is_active=True,
    )
    waiter = User(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        email="waiter@downtown.com",
        hashed_password=pw,
        full_name="Downtown Waiter",
        role=UserRole.WAITER,
        is_active=True,
    )
    test_session.add_all([super_admin, branch_admin, cashier, waiter])
    await test_session.flush()

    # Branch access associations
    acc_ba1 = UserBranchAccess(user_id=branch_admin.id, branch_id=branch_1.id)
    acc_c1 = UserBranchAccess(user_id=cashier.id, branch_id=branch_1.id)
    acc_w1 = UserBranchAccess(user_id=waiter.id, branch_id=branch_1.id)
    test_session.add_all([acc_ba1, acc_c1, acc_w1])
    await test_session.flush()

    # 3. Branch 1 Tables (3 tables)
    t1 = Table(
        id=uuid.uuid4(),
        branch_id=branch_1.id,
        table_number="1",
        capacity=4,
        status=TableStatus.AVAILABLE,
        is_active=True,
    )
    t2 = Table(
        id=uuid.uuid4(),
        branch_id=branch_1.id,
        table_number="2",
        capacity=2,
        status=TableStatus.AVAILABLE,
        is_active=True,
    )
    t3 = Table(
        id=uuid.uuid4(),
        branch_id=branch_1.id,
        table_number="3",
        capacity=6,
        status=TableStatus.AVAILABLE,
        is_active=True,
    )

    # Branch 2 Table (1 table)
    t_b2 = Table(
        id=uuid.uuid4(),
        branch_id=branch_2.id,
        table_number="B1",
        capacity=4,
        status=TableStatus.AVAILABLE,
        is_active=True,
    )
    test_session.add_all([t1, t2, t3, t_b2])
    await test_session.commit()

    # 4. Access Tokens
    super_token = create_access_token({"sub": str(super_admin.id), "tenant_id": str(tenant.id), "role": super_admin.role.value})
    admin_token = create_access_token({"sub": str(branch_admin.id), "tenant_id": str(tenant.id), "role": branch_admin.role.value})
    cashier_token = create_access_token({"sub": str(cashier.id), "tenant_id": str(tenant.id), "role": cashier.role.value})
    waiter_token = create_access_token({"sub": str(waiter.id), "tenant_id": str(tenant.id), "role": waiter.role.value})

    return {
        "tenant": tenant,
        "branch_1": branch_1,
        "branch_2": branch_2,
        "super_admin": super_admin,
        "branch_admin": branch_admin,
        "cashier": cashier,
        "waiter": waiter,
        "t1": t1,
        "t2": t2,
        "t3": t3,
        "t_b2": t_b2,
        "super_token": super_token,
        "admin_token": admin_token,
        "cashier_token": cashier_token,
        "waiter_token": waiter_token,
    }


# ---------------------------------------------------------------------------
# Test 1: Cryptographic Validation & Tamper Prevention
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cryptographic_validation_and_tamper_prevention(
    test_client: AsyncClient,
    seed_qr_export_data: dict,
) -> None:
    """Verify cryptographic HMAC-SHA256 signature signing, verification probe, and tampering rejection."""
    t1 = seed_qr_export_data["t1"]
    b1 = seed_qr_export_data["branch_1"]
    now_ts = int(datetime.now(timezone.utc).timestamp())

    # 1. Sign URL
    signed_url, sig, ts, _ = QRGeneratorService.create_signed_payload(
        table_id=t1.id,
        branch_id=b1.id,
        timestamp=now_ts,
    )
    assert sig is not None
    assert f"t/{t1.id}" in signed_url
    assert f"b={b1.id}" in signed_url
    assert f"ts={ts}" in signed_url
    assert f"sig={sig}" in signed_url

    # 2. Public verification probe with legitimate parameters -> 200 OK
    resp_valid = await test_client.get(
        "/api/v1/qr-export/verify",
        params={
            "table_id": str(t1.id),
            "branch_id": str(b1.id),
            "ts": ts,
            "sig": sig,
        },
    )
    assert resp_valid.status_code == 200, resp_valid.text
    data_valid = resp_valid.json()
    assert data_valid["valid"] is True
    assert data_valid["table_id"] == str(t1.id)
    assert data_valid["table_number"] == t1.table_number

    # 3. Tampering table_id -> 403 Forbidden
    fake_table_id = uuid.uuid4()
    resp_tamper_table = await test_client.get(
        "/api/v1/qr-export/verify",
        params={
            "table_id": str(fake_table_id),
            "branch_id": str(b1.id),
            "ts": ts,
            "sig": sig,
        },
    )
    assert resp_tamper_table.status_code == 403

    # 4. Tampering branch_id -> 403 Forbidden
    fake_branch_id = uuid.uuid4()
    resp_tamper_branch = await test_client.get(
        "/api/v1/qr-export/verify",
        params={
            "table_id": str(t1.id),
            "branch_id": str(fake_branch_id),
            "ts": ts,
            "sig": sig,
        },
    )
    assert resp_tamper_branch.status_code == 403

    # 5. Tampering timestamp -> 403 Forbidden
    resp_tamper_ts = await test_client.get(
        "/api/v1/qr-export/verify",
        params={
            "table_id": str(t1.id),
            "branch_id": str(b1.id),
            "ts": ts + 9999,
            "sig": sig,
        },
    )
    assert resp_tamper_ts.status_code == 403

    # 6. Tampering signature -> 403 Forbidden
    resp_tamper_sig = await test_client.get(
        "/api/v1/qr-export/verify",
        params={
            "table_id": str(t1.id),
            "branch_id": str(b1.id),
            "ts": ts,
            "sig": "invalid_signature_hex_value",
        },
    )
    assert resp_tamper_sig.status_code == 403


# ---------------------------------------------------------------------------
# Test 2: Single Table Direct Asset Export
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_single_table_direct_asset_export(
    test_client: AsyncClient,
    seed_qr_export_data: dict,
) -> None:
    """Verify single-table QR asset streaming in SVG and PNG formats with table labels."""
    t1 = seed_qr_export_data["t1"]
    b1 = seed_qr_export_data["branch_1"]
    headers = {
        "Authorization": f"Bearer {seed_qr_export_data['admin_token']}",
        "X-Branch-ID": str(b1.id),
    }

    # 1. Export SVG
    resp_svg = await test_client.get(
        f"/api/v1/qr-export/tables/{t1.id}/image?format=svg&scale=12&include_label=true",
        headers=headers,
    )
    assert resp_svg.status_code == 200, resp_svg.text
    assert resp_svg.headers["content-type"] == "image/svg+xml"
    assert f'filename="table_{t1.table_number}.svg"' in resp_svg.headers.get("content-disposition", "")
    svg_body = resp_svg.text
    assert "<svg" in svg_body
    assert f"Table {t1.table_number}" in svg_body

    # 2. Export PNG
    resp_png = await test_client.get(
        f"/api/v1/qr-export/tables/{t1.id}/image?format=png&scale=10&include_label=true",
        headers=headers,
    )
    assert resp_png.status_code == 200, resp_png.text
    assert resp_png.headers["content-type"] == "image/png"
    assert f'filename="table_{t1.table_number}.png"' in resp_png.headers.get("content-disposition", "")
    png_bytes = resp_png.content
    assert png_bytes[:8] == b"\x89PNG\r\n\x1a\n"

    # 3. Metadata URL endpoint
    resp_url = await test_client.get(
        f"/api/v1/qr-export/tables/{t1.id}/url",
        headers=headers,
    )
    assert resp_url.status_code == 200, resp_url.text
    url_data = resp_url.json()
    assert url_data["table_id"] == str(t1.id)
    assert url_data["table_number"] == t1.table_number
    assert url_data["branch_id"] == str(b1.id)
    assert "https://" in url_data["signed_url"]
    assert "sig=" in url_data["signed_url"]


# ---------------------------------------------------------------------------
# Test 3: Batch Branch ZIP Integrity
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_batch_branch_zip_integrity(
    test_client: AsyncClient,
    seed_qr_export_data: dict,
) -> None:
    """Verify batch ZIP export packages all active branch tables and valid manifest.json."""
    b1 = seed_qr_export_data["branch_1"]
    headers = {
        "Authorization": f"Bearer {seed_qr_export_data['admin_token']}",
        "X-Branch-ID": str(b1.id),
    }

    # Export all active tables in Branch 1 (3 tables)
    resp_batch = await test_client.post(
        "/api/v1/qr-export/batch",
        json={
            "format": "png",
            "scale": 10,
            "include_label": True,
        },
        headers=headers,
    )
    assert resp_batch.status_code == 200, resp_batch.text
    assert resp_batch.headers["content-type"] == "application/zip"
    assert f"branch_{b1.id}_qr_pack.zip" in resp_batch.headers.get("content-disposition", "")

    # Inspect in-memory ZIP package
    zip_buf = io.BytesIO(resp_batch.content)
    with zipfile.ZipFile(zip_buf, "r") as z:
        names = z.namelist()
        assert "manifest.json" in names
        assert len(names) == 4  # 3 tables + 1 manifest

        # Assert table files exist and are non-empty valid PNGs
        for fname in ["table_01.png", "table_02.png", "table_03.png"]:
            assert fname in names
            content = z.read(fname)
            assert content[:8] == b"\x89PNG\r\n\x1a\n"

        # Assert manifest JSON accuracy
        manifest_raw = z.read("manifest.json")
        manifest_data = json.loads(manifest_raw.decode("utf-8"))
        assert manifest_data["branch_id"] == str(b1.id)
        assert manifest_data["total_tables"] == 3
        assert manifest_data["format"] == "png"
        assert len(manifest_data["files"]) == 3


# ---------------------------------------------------------------------------
# Test 4: Multi-Tenant & Branch Isolation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_multi_tenant_and_branch_isolation(
    test_client: AsyncClient,
    seed_qr_export_data: dict,
) -> None:
    """Ensure accessing a table from Branch B while scoping to Branch A returns 404."""
    b1 = seed_qr_export_data["branch_1"]
    t_b2 = seed_qr_export_data["t_b2"]  # Belongs to Branch 2
    headers = {
        "Authorization": f"Bearer {seed_qr_export_data['admin_token']}",
        "X-Branch-ID": str(b1.id),
    }

    # Request Table B2 with Branch 1 header -> 404 Not Found
    resp_cross = await test_client.get(
        f"/api/v1/qr-export/tables/{t_b2.id}/image",
        headers=headers,
    )
    assert resp_cross.status_code == 404
    assert "Table not found" in resp_cross.json()["detail"]


# ---------------------------------------------------------------------------
# Test 5: RBAC Guardrails
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rbac_guardrails(
    test_client: AsyncClient,
    seed_qr_export_data: dict,
) -> None:
    """Verify strict access controls: reject unauthenticated and staff roles, allow admins."""
    t1 = seed_qr_export_data["t1"]
    b1 = seed_qr_export_data["branch_1"]

    # 1. Missing authentication token -> 401 Unauthorized
    resp_unauth = await test_client.get(
        f"/api/v1/qr-export/tables/{t1.id}/image",
        headers={"X-Branch-ID": str(b1.id)},
    )
    assert resp_unauth.status_code == 401

    # 2. Waiter role -> 403 Forbidden
    resp_waiter = await test_client.get(
        f"/api/v1/qr-export/tables/{t1.id}/image",
        headers={
            "Authorization": f"Bearer {seed_qr_export_data['waiter_token']}",
            "X-Branch-ID": str(b1.id),
        },
    )
    assert resp_waiter.status_code == 403

    # 3. Cashier role -> 403 Forbidden
    resp_cashier = await test_client.get(
        f"/api/v1/qr-export/tables/{t1.id}/image",
        headers={
            "Authorization": f"Bearer {seed_qr_export_data['cashier_token']}",
            "X-Branch-ID": str(b1.id),
        },
    )
    assert resp_cashier.status_code == 403

    # 4. Branch Admin -> 200 OK
    resp_admin = await test_client.get(
        f"/api/v1/qr-export/tables/{t1.id}/image",
        headers={
            "Authorization": f"Bearer {seed_qr_export_data['admin_token']}",
            "X-Branch-ID": str(b1.id),
        },
    )
    assert resp_admin.status_code == 200

    # 5. Super Admin -> 200 OK
    resp_super = await test_client.get(
        f"/api/v1/qr-export/tables/{t1.id}/image",
        headers={
            "Authorization": f"Bearer {seed_qr_export_data['super_token']}",
            "X-Branch-ID": str(b1.id),
        },
    )
    assert resp_super.status_code == 200
