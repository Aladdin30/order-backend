"""Redis Pub/Sub bridge for distributed real-time ASGI WebSocket event multiplexing.

Connects to Redis Pub/Sub, subscribes to scoped wildcard channels (branch_*),
and routes inbound event frames to the local WebSocketManager.
Supports local fallback when Redis is unavailable (e.g. isolated test environments).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

import redis.asyncio as aioredis

from app.core.config import settings
from app.core.websocket_manager import WebSocketManager, ws_manager

logger = logging.getLogger("app.core.redis_pubsub")


class RedisPubSubBridge:
    """Manages the asynchronous Redis Pub/Sub background listener and publisher."""

    def __init__(self, redis_url: str | None = None) -> None:
        self.redis_url = redis_url or settings.REDIS_URL
        self._redis: aioredis.Redis | None = None
        self._pubsub: aioredis.client.PubSub | None = None
        self._task: asyncio.Task | None = None
        self._ws_manager: WebSocketManager = ws_manager
        self._is_connected: bool = False
        self._lock: asyncio.Lock = asyncio.Lock()

    async def get_redis_client(self) -> aioredis.Redis | None:
        """Get or lazily initialize the asynchronous Redis client."""
        async with self._lock:
            if self._redis is None:
                try:
                    self._redis = aioredis.from_url(
                        self.redis_url,
                        decode_responses=True,
                        socket_connect_timeout=2.0,
                    )
                    await self._redis.ping()
                    self._is_connected = True
                except Exception as exc:
                    logger.warning("Failed to connect to Redis at %s: %s", self.redis_url, exc)
                    if self._redis is not None:
                        await self._redis.aclose()
                        self._redis = None
                    self._is_connected = False
            return self._redis

    async def start(self, manager: WebSocketManager | None = None) -> None:
        """Initialize Redis connection and start the background event loop reader task."""
        if manager is not None:
            self._ws_manager = manager

        # Attempt to establish Redis connection
        client = await self.get_redis_client()
        if client is None:
            logger.warning(
                "Redis is offline. Operating with local-only broadcast fallback."
            )
            return

        try:
            self._pubsub = client.pubsub()
            await self._pubsub.psubscribe("branch_*")
            self._task = asyncio.create_task(self._reader_loop(), name="redis_pubsub_reader")
            logger.info("Redis Pub/Sub bridge started and listening on pattern 'branch_*'")
        except Exception as exc:
            logger.warning("Error starting Redis Pub/Sub subscription: %s", exc)
            self._is_connected = False

    async def stop(self) -> None:
        """Gracefully stop the reader task and close Redis connections."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning("Exception awaiting cancelled Redis reader task: %s", exc)
            self._task = None

        if self._pubsub:
            try:
                await self._pubsub.punsubscribe("branch_*")
                await self._pubsub.aclose()
            except Exception as exc:
                logger.debug("Exception closing pubsub: %s", exc)
            self._pubsub = None

        if self._redis:
            try:
                await self._redis.aclose()
            except Exception as exc:
                logger.debug("Exception closing redis connection: %s", exc)
            self._redis = None

        self._is_connected = False
        logger.info("Redis Pub/Sub bridge stopped cleanly.")

    async def _reader_loop(self) -> None:
        """Background loop reading multiplexed events from Redis and broadcasting locally."""
        if not self._pubsub:
            return

        try:
            async for message in self._pubsub.listen():
                if not message:
                    continue

                msg_type = message.get("type")
                if msg_type != "pmessage":
                    continue

                channel = message.get("channel")
                raw_data = message.get("data")
                if not channel or not raw_data:
                    continue

                try:
                    payload = json.loads(raw_data)
                    if not isinstance(payload, dict):
                        payload = {"event": "MESSAGE", "data": payload, "channel": channel}
                except Exception:
                    payload = {"event": "MESSAGE", "data": raw_data, "channel": channel}

                # Deliver to local WebSockets subscribed to this channel
                await self._ws_manager.broadcast_local(channel, payload)

        except asyncio.CancelledError:
            logger.debug("Redis Pub/Sub reader loop cancelled.")
            raise
        except Exception as exc:
            logger.error("Unexpected error in Redis Pub/Sub reader loop: %s", exc, exc_info=True)
            self._is_connected = False

    async def publish(self, channel: str, event_type: str, data: dict[str, Any]) -> None:
        """Publish an event envelope across Redis Pub/Sub.

        If Redis is unreachable, falls back to local worker delivery.
        """
        envelope = {
            "event": event_type,
            "channel": channel,
            "data": data,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # Attempt to publish via Redis
        client = await self.get_redis_client()
        if client and self._is_connected:
            try:
                await client.publish(channel, json.dumps(envelope))
                return
            except Exception as exc:
                logger.warning(
                    "Redis publish failed on channel %s: %s. Falling back to local broadcast.",
                    channel,
                    exc,
                )
                self._is_connected = False

        # Local fallback if Redis is offline
        await self._ws_manager.broadcast_local(channel, envelope)


# Global singleton instance
redis_pubsub = RedisPubSubBridge()


async def publish(channel: str, event_type: str, data: dict[str, Any]) -> None:
    """Helper function to publish a real-time event through Redis Pub/Sub."""
    await redis_pubsub.publish(channel, event_type, data)
