"""Unit & Integration tests for daily branch-scoped pickup sequence generator (100 to 1000)."""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import app.core.database  # Registers SQLite JSONB compiler extension
from app.models.auth import Base, Branch, Tenant
from app.models.enums import OrderSource, OrderStatus, OrderType
from app.models.order import Order
from app.services.pickup_sequence_service import (
    LUA_PICKUP_SEQUENCE,
    SEQUENCE_TTL_SECONDS,
    PickupSequenceService,
)


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
async def db_session(async_test_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """Yields an active database session for testing."""
    session_factory = async_sessionmaker(async_test_engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session


@pytest_asyncio.fixture(scope="function")
async def seed_branch(db_session: AsyncSession) -> Branch:
    """Seed sample tenant and branch."""
    tenant = Tenant(
        name="Burger House",
        slug=f"burger-house-{uuid.uuid4().hex[:6]}",
        is_active=True,
    )
    db_session.add(tenant)
    await db_session.flush()

    branch = Branch(
        tenant_id=tenant.id,
        name={"en": "Downtown", "ar": "وسط البلد"},
        slug=f"downtown-{uuid.uuid4().hex[:6]}",
        latitude=Decimal("24.7136000"),
        longitude=Decimal("46.6753000"),
        geofence_radius_meters=150,
        is_active=True,
    )
    db_session.add(branch)
    await db_session.commit()
    return branch


class FakeRedisClient:
    """In-memory simulated Redis client faithfully executing the Lua sequence logic."""

    def __init__(self) -> None:
        self.store: dict[str, int] = {}
        self.ttls: dict[str, int] = {}

    async def eval(self, script: str, numkeys: int, key: str, ttl: int) -> int:
        if key not in self.store:
            self.store[key] = 100
            self.ttls[key] = ttl
            return 100
        else:
            current = self.store[key]
            if current >= 1000:
                self.store[key] = 100
                self.ttls[key] = ttl
                return 100
            else:
                self.store[key] = current + 1
                return self.store[key]


@pytest.mark.asyncio
async def test_pickup_sequence_redis_lua_bounds_and_wraparound(
    seed_branch: Branch,
    db_session: AsyncSession,
) -> None:
    """Verify sequence starts at 100, increments atomically, and wraps around at 1001 to 100."""
    fake_redis = FakeRedisClient()
    branch_id = seed_branch.id
    target_date = datetime.date(2026, 9, 18)

    # 1. First call starts at 100
    num1 = await PickupSequenceService.get_next_pickup_number(
        branch_id=branch_id,
        db=db_session,
        target_date=target_date,
        redis_override=fake_redis,  # type: ignore
    )
    assert num1 == 100

    # 2. Second call increments to 101
    num2 = await PickupSequenceService.get_next_pickup_number(
        branch_id=branch_id,
        db=db_session,
        target_date=target_date,
        redis_override=fake_redis,  # type: ignore
    )
    assert num2 == 101

    # 3. Simulate reaching 999 and 1000
    fake_redis.store[PickupSequenceService.get_redis_key(branch_id, target_date)] = 999
    num_1000 = await PickupSequenceService.get_next_pickup_number(
        branch_id=branch_id,
        db=db_session,
        target_date=target_date,
        redis_override=fake_redis,  # type: ignore
    )
    assert num_1000 == 1000

    # 4. 1001 resets to 100 (wrap-around)
    num_reset = await PickupSequenceService.get_next_pickup_number(
        branch_id=branch_id,
        db=db_session,
        target_date=target_date,
        redis_override=fake_redis,  # type: ignore
    )
    assert num_reset == 100


@pytest.mark.asyncio
async def test_pickup_sequence_branch_and_date_isolation(
    seed_branch: Branch,
    db_session: AsyncSession,
) -> None:
    """Verify sequence counters are partitioned by branch_id and calendar day."""
    fake_redis = FakeRedisClient()
    branch1_id = seed_branch.id
    branch2_id = uuid.uuid4()

    day1 = datetime.date(2026, 9, 18)
    day2 = datetime.date(2026, 9, 19)

    # Branch 1, Day 1 -> 100, 101
    b1_d1_1 = await PickupSequenceService.get_next_pickup_number(branch1_id, db_session, day1, fake_redis)  # type: ignore
    b1_d1_2 = await PickupSequenceService.get_next_pickup_number(branch1_id, db_session, day1, fake_redis)  # type: ignore
    assert b1_d1_1 == 100
    assert b1_d1_2 == 101

    # Branch 2, Day 1 -> independent 100
    b2_d1_1 = await PickupSequenceService.get_next_pickup_number(branch2_id, db_session, day1, fake_redis)  # type: ignore
    assert b2_d1_1 == 100

    # Branch 1, Day 2 -> independent 100
    b1_d2_1 = await PickupSequenceService.get_next_pickup_number(branch1_id, db_session, day2, fake_redis)  # type: ignore
    assert b1_d2_1 == 100


@pytest.mark.asyncio
async def test_pickup_sequence_db_fallback(
    seed_branch: Branch,
    db_session: AsyncSession,
) -> None:
    """Verify fallback to database when Redis client is unavailable or errors."""
    branch_id = seed_branch.id
    tenant_id = seed_branch.tenant_id
    today = datetime.datetime.now(datetime.timezone.utc).date()

    # Simulate broken Redis that raises ConnectionError on eval
    broken_redis = MagicMock()
    broken_redis.eval = AsyncMock(side_effect=ConnectionError("Redis connection lost"))

    # With no existing orders today, fallback returns 100
    num1 = await PickupSequenceService.get_next_pickup_number(
        branch_id=branch_id,
        db=db_session,
        redis_override=broken_redis,  # type: ignore
    )
    assert num1 == 100

    # Insert an order with pickup_number = 100
    order1 = Order(
        tenant_id=tenant_id,
        branch_id=branch_id,
        status=OrderStatus.SUBMITTED,
        order_type=OrderType.TAKEAWAY,
        order_source=OrderSource.CASHIER_POS,
        pickup_number=100,
        subtotal=Decimal("50.00"),
        tax_total=Decimal("7.50"),
        total_amount=Decimal("57.50"),
    )
    db_session.add(order1)
    await db_session.commit()

    # Next fallback should return 101
    num2 = await PickupSequenceService.get_next_pickup_number(
        branch_id=branch_id,
        db=db_session,
        redis_override=broken_redis,  # type: ignore
    )
    assert num2 == 101

    # Insert an order with pickup_number = 1000
    order2 = Order(
        tenant_id=tenant_id,
        branch_id=branch_id,
        status=OrderStatus.SUBMITTED,
        order_type=OrderType.TAKEAWAY,
        order_source=OrderSource.CASHIER_POS,
        pickup_number=1000,
        subtotal=Decimal("50.00"),
        tax_total=Decimal("7.50"),
        total_amount=Decimal("57.50"),
    )
    db_session.add(order2)
    await db_session.commit()

    # Next fallback should wrap back to 100
    num_reset = await PickupSequenceService.get_next_pickup_number(
        branch_id=branch_id,
        db=db_session,
        redis_override=broken_redis,  # type: ignore
    )
    assert num_reset == 100
