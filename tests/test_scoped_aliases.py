"""Comprehensive test suite for SQLAlchemy Aliased Models in Tenancy and Branch Scoping (Issue #7).

Verifies:
- Normal ORM model queries (apply_tenancy_filter & apply_branch_scope)
- aliased() ORM model queries without raising AttributeError
- Joined queries with aliases
- Multi-tenant isolation with aliases
- Branch-scoped queries with aliases
- Cross-tenant access prevention with aliases
"""

import uuid
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import aliased

import app.core.database  # Registers SQLite JSONB compiler extension
from app.crud.base_scoped import apply_branch_scope, apply_tenancy_filter
from app.models import Base, Branch, Order, OrderStatus, OrderType, Table, Tenant


@pytest_asyncio.fixture(scope="function")
async def async_test_engine() -> AsyncGenerator[AsyncEngine, None]:
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
    session_factory = async_sessionmaker(
        bind=async_test_engine,
        class_=AsyncSession,
        autoflush=False,
        expire_on_commit=False,
    )
    async with session_factory() as session:
        yield session


@pytest_asyncio.fixture(scope="function")
async def scoping_seed_data(test_session: AsyncSession) -> dict:
    """Seed 2 Tenants, multiple Branches, Tables, and Orders."""
    tenant_1 = Tenant(name="Tenant One", slug="tenant-1", is_active=True)
    tenant_2 = Tenant(name="Tenant Two", slug="tenant-2", is_active=True)
    test_session.add_all([tenant_1, tenant_2])
    await test_session.flush()

    branch_1a = Branch(
        tenant_id=tenant_1.id,
        name={"en": "Branch 1A", "ar": "فرع 1أ"},
        slug="branch-1a",
        latitude=24.7135,
        longitude=46.6753,
        is_active=True,
    )
    branch_1b = Branch(
        tenant_id=tenant_1.id,
        name={"en": "Branch 1B", "ar": "فرع 1ب"},
        slug="branch-1b",
        latitude=24.7200,
        longitude=46.6800,
        is_active=True,
    )
    branch_2a = Branch(
        tenant_id=tenant_2.id,
        name={"en": "Branch 2A", "ar": "فرع 2أ"},
        slug="branch-2a",
        latitude=25.0000,
        longitude=47.0000,
        is_active=True,
    )
    test_session.add_all([branch_1a, branch_1b, branch_2a])
    await test_session.flush()

    table_1a = Table(branch_id=branch_1a.id, table_number="T-1A", capacity=4, is_active=True)
    table_1b = Table(branch_id=branch_1b.id, table_number="T-1B", capacity=4, is_active=True)
    table_2a = Table(branch_id=branch_2a.id, table_number="T-2A", capacity=4, is_active=True)
    test_session.add_all([table_1a, table_1b, table_2a])
    await test_session.flush()

    order_1a = Order(
        tenant_id=tenant_1.id,
        branch_id=branch_1a.id,
        table_id=table_1a.id,
        status=OrderStatus.SUBMITTED,
        order_type=OrderType.DINE_IN,
        customer_notes="ORD-1A",
    )
    order_1b = Order(
        tenant_id=tenant_1.id,
        branch_id=branch_1b.id,
        table_id=table_1b.id,
        status=OrderStatus.SUBMITTED,
        order_type=OrderType.DINE_IN,
        customer_notes="ORD-1B",
    )
    order_2a = Order(
        tenant_id=tenant_2.id,
        branch_id=branch_2a.id,
        table_id=table_2a.id,
        status=OrderStatus.SUBMITTED,
        order_type=OrderType.DINE_IN,
        customer_notes="ORD-2A",
    )
    test_session.add_all([order_1a, order_1b, order_2a])
    await test_session.commit()

    return {
        "tenant_1": tenant_1,
        "tenant_2": tenant_2,
        "branch_1a": branch_1a,
        "branch_1b": branch_1b,
        "branch_2a": branch_2a,
        "table_1a": table_1a,
        "table_1b": table_1b,
        "table_2a": table_2a,
        "order_1a": order_1a,
        "order_1b": order_1b,
        "order_2a": order_2a,
    }


@pytest.mark.asyncio
async def test_normal_model_query(test_session: AsyncSession, scoping_seed_data: dict) -> None:
    """Normal declarative models support tenancy and branch scoping."""
    t1 = scoping_seed_data["tenant_1"]
    b1a = scoping_seed_data["branch_1a"]

    stmt = select(Order)
    stmt = apply_tenancy_filter(stmt, Order, tenant_id=t1.id)
    stmt = apply_branch_scope(stmt, Order, branch_id=b1a.id)

    result = await test_session.execute(stmt)
    orders = result.scalars().all()
    assert len(orders) == 1
    assert orders[0].customer_notes == "ORD-1A"


@pytest.mark.asyncio
async def test_aliased_model_query(test_session: AsyncSession, scoping_seed_data: dict) -> None:
    """aliased(Model) supports tenancy and branch scoping without raising AttributeError."""
    t1 = scoping_seed_data["tenant_1"]
    b1a = scoping_seed_data["branch_1a"]

    ord_alias = aliased(Order)
    stmt = select(ord_alias)
    stmt = apply_tenancy_filter(stmt, ord_alias, tenant_id=t1.id)
    stmt = apply_branch_scope(stmt, ord_alias, branch_id=b1a.id)

    result = await test_session.execute(stmt)
    orders = result.scalars().all()
    assert len(orders) == 1
    assert orders[0].customer_notes == "ORD-1A"


@pytest.mark.asyncio
async def test_joined_query_with_alias(test_session: AsyncSession, scoping_seed_data: dict) -> None:
    """Joined queries with aliased models filter correctly on both model and alias."""
    t1 = scoping_seed_data["tenant_1"]
    b1a = scoping_seed_data["branch_1a"]

    branch_alias = aliased(Branch)
    stmt = (
        select(Table, branch_alias)
        .join(branch_alias, Table.branch_id == branch_alias.id)
    )
    # Apply tenancy filter on the aliased Branch
    stmt = apply_tenancy_filter(stmt, branch_alias, tenant_id=t1.id)
    # Apply branch filter on Table
    stmt = apply_branch_scope(stmt, Table, branch_id=b1a.id)

    result = await test_session.execute(stmt)
    rows = result.all()
    assert len(rows) == 1
    table_row, branch_row = rows[0]
    assert branch_row.id == b1a.id
    assert table_row.branch_id == b1a.id


@pytest.mark.asyncio
async def test_multi_tenant_query_with_alias(test_session: AsyncSession, scoping_seed_data: dict) -> None:
    """apply_tenancy_filter on Branch alias filters only the requested tenant."""
    t1 = scoping_seed_data["tenant_1"]
    branch_alias = aliased(Branch)

    stmt = select(branch_alias)
    stmt = apply_tenancy_filter(stmt, branch_alias, tenant_id=t1.id)

    result = await test_session.execute(stmt)
    branches = result.scalars().all()
    assert len(branches) == 2
    for b in branches:
        assert b.tenant_id == t1.id


@pytest.mark.asyncio
async def test_branch_scoped_query_with_alias(test_session: AsyncSession, scoping_seed_data: dict) -> None:
    """apply_branch_scope on Table alias filters only the matching branch."""
    b1b = scoping_seed_data["branch_1b"]
    table_alias = aliased(Table)

    stmt = select(table_alias)
    stmt = apply_branch_scope(stmt, table_alias, branch_id=b1b.id)

    result = await test_session.execute(stmt)
    tables = result.scalars().all()
    assert len(tables) == 1
    assert tables[0].table_number == "T-1B"


@pytest.mark.asyncio
async def test_cross_tenant_access_prevented_with_alias(
    test_session: AsyncSession,
    scoping_seed_data: dict,
) -> None:
    """Querying with tenant 1 context using an alias never returns records from tenant 2."""
    t1 = scoping_seed_data["tenant_1"]
    ord_alias = aliased(Order)

    stmt = select(ord_alias)
    stmt = apply_tenancy_filter(stmt, ord_alias, tenant_id=t1.id)

    result = await test_session.execute(stmt)
    orders = result.scalars().all()
    assert len(orders) == 2
    for o in orders:
        assert o.customer_notes != "ORD-2A"
        assert o.tenant_id == t1.id
