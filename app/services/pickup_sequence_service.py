"""Daily branch-scoped pickup sequence generator (100 to 1000) using Redis Lua script with ACID database fallback."""

from __future__ import annotations

import datetime
import logging
import uuid
from typing import Any

import redis.asyncio as aioredis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.order import Order

logger = logging.getLogger("app.services.pickup_sequence_service")

# 36-hour TTL in seconds (covers full business day and roll-over operations)
SEQUENCE_TTL_SECONDS: int = 36 * 3600

# Redis Lua script enforcing atomic sequence bounds between 100 and 1000
LUA_PICKUP_SEQUENCE = """
local key = KEYS[1]
local ttl = tonumber(ARGV[1])

local current = redis.call('GET', key)
local next_val

if not current then
    next_val = 100
    redis.call('SET', key, next_val, 'EX', ttl)
else
    local num = tonumber(current)
    if num >= 1000 then
        next_val = 100
        redis.call('SET', key, next_val, 'EX', ttl)
    else
        next_val = redis.call('INCR', key)
    end
end

return next_val
"""


class PickupSequenceService:
    """Manages daily sequential takeaway pickup token numbers strictly bounded between 100 and 1000."""

    _redis_client: aioredis.Redis | None = None

    @classmethod
    async def get_redis_client(cls) -> aioredis.Redis | None:
        """Lazily initialize Redis connection client."""
        if cls._redis_client is None:
            try:
                cls._redis_client = aioredis.from_url(
                    settings.REDIS_URL,
                    decode_responses=True,
                    socket_connect_timeout=2.0,
                )
                await cls._redis_client.ping()
            except Exception as exc:
                logger.warning("Redis unavailable for pickup sequence: %s", exc)
                cls._redis_client = None
        return cls._redis_client

    @classmethod
    def set_redis_client(cls, client: aioredis.Redis | None) -> None:
        """Inject or override Redis client (useful for unit tests)."""
        cls._redis_client = client

    @classmethod
    def get_redis_key(cls, branch_id: uuid.UUID, target_date: datetime.date) -> str:
        """Construct deterministic branch-scoped daily Redis sequence key."""
        date_str = target_date.strftime("%Y-%m-%d")
        return f"pickup_seq:{branch_id}:{date_str}"

    @classmethod
    async def get_next_pickup_number(
        cls,
        branch_id: uuid.UUID,
        db: AsyncSession,
        target_date: datetime.date | None = None,
        redis_override: aioredis.Redis | None = None,
    ) -> int:
        """Generate next atomic pickup number between 100 and 1000.
        
        Attempts atomic evaluation via Redis Lua script; transparently falls back to
        database inspection if Redis is unreachable or returns an error.
        """
        target_date = target_date or datetime.datetime.now(datetime.timezone.utc).date()
        key = cls.get_redis_key(branch_id, target_date)
        redis_client = redis_override or await cls.get_redis_client()

        if redis_client is not None:
            try:
                res = await redis_client.eval(
                    LUA_PICKUP_SEQUENCE,
                    1,
                    key,
                    SEQUENCE_TTL_SECONDS,
                )
                val = int(res)
                if 100 <= val <= 1000:
                    return val
                logger.warning("Redis Lua script returned out-of-bounds pickup sequence '%s', falling back to DB", val)
            except Exception as exc:
                logger.warning("Failed evaluating Redis pickup sequence: %s; falling back to DB", exc)

        return await cls._fallback_db_sequence(db=db, branch_id=branch_id, target_date=target_date)

    @classmethod
    async def _fallback_db_sequence(
        cls,
        db: AsyncSession,
        branch_id: uuid.UUID,
        target_date: datetime.date,
    ) -> int:
        """Safe ACID database sequence fallback using func.max(Order.pickup_number)."""
        start_dt = datetime.datetime.combine(target_date, datetime.time.min, tzinfo=datetime.timezone.utc)
        end_dt = datetime.datetime.combine(target_date, datetime.time.max, tzinfo=datetime.timezone.utc)

        stmt = (
            select(func.max(Order.pickup_number))
            .where(
                Order.branch_id == branch_id,
                Order.created_at >= start_dt,
                Order.created_at <= end_dt,
                Order.pickup_number.is_not(None),
            )
        )
        res = await db.execute(stmt)
        max_pickup = res.scalar_one_or_none()

        if max_pickup is None or max_pickup < 100 or max_pickup >= 1000:
            return 100
        return int(max_pickup) + 1
