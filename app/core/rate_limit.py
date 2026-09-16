"""Production-ready in-process sliding-window rate limiter with reverse proxy trust modeling.

Key features:
1. Strict reverse proxy trust model: X-Forwarded-For and X-Real-IP are ONLY inspected
   when the immediate socket connection originates from a configured trusted proxy network.
   Direct untrusted clients cannot spoof forwarded headers.
2. Memory-bounded sliding window counter: Keys with expired timestamps are pruned immediately;
   periodic inline cleanup and LRU/oldest eviction prevent memory exhaustion under high volume.
3. Concurrency-safe: Protected by asyncio.Lock per counter instance.
4. Fail-safe decorator: Rejects route definitions missing 'request: Request' at startup/invocation
   to prevent accidental silent fail-open security bypass.
"""

from __future__ import annotations

import asyncio
import inspect
import ipaddress
import logging
import sqlite3
import time
from abc import ABC, abstractmethod
from functools import wraps
from typing import Any, Callable, Sequence

from fastapi import HTTPException, Request, status

from app.core.config import settings

logger = logging.getLogger(__name__)

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def parse_trusted_networks(
    trusted_proxies: Sequence[str] | str | None = None,
) -> list[IPNetwork]:
    """Parse list of IP addresses or CIDR blocks into IPNetwork objects."""
    if trusted_proxies is None:
        trusted_proxies = settings.TRUSTED_PROXIES

    if isinstance(trusted_proxies, str):
        raw_items = [p.strip() for p in trusted_proxies.split(",") if p.strip()]
    else:
        raw_items = list(trusted_proxies)

    networks: list[IPNetwork] = []
    for item in raw_items:
        try:
            # strict=False allows host IPs like '127.0.0.1' (as /32) or networks like '10.0.0.0/8'
            networks.append(ipaddress.ip_network(item.strip(), strict=False))
        except ValueError:
            logger.warning("Invalid trusted proxy network configuration ignored: '%s'", item)
            continue

    return networks


def is_ip_in_networks(ip: IPAddress, networks: Sequence[IPNetwork]) -> bool:
    """Check if an IP address belongs to any configured network."""
    return any(ip in net for net in networks)


def resolve_client_ip(
    request: Request,
    trusted_networks: Sequence[IPNetwork] | None = None,
) -> str:
    """Resolve genuine client IP using trusted reverse proxy traversal.

    Security model:
    1. Extract the direct socket connection IP (request.client.host).
    2. If request.client is absent, fallback to '127.0.0.1'.
    3. Check if direct socket IP belongs to a configured trusted reverse proxy network.
    4. If direct IP is NOT trusted, NEVER trust X-Forwarded-For or X-Real-IP headers,
       and return direct socket IP immediately (prevents client IP spoofing).
    5. If direct IP IS trusted:
       a. Parse X-Forwarded-For header (comma-separated chain: client, proxy1, proxy2).
       b. Traverse the chain from right-to-left.
       c. The first IP encountered that is NOT in the trusted proxy list is the real client IP.
       d. If all IPs in the chain are trusted, use the leftmost IP.
       e. If X-Forwarded-For is absent, check X-Real-IP (if valid IP).
       f. Fallback to direct socket IP if headers are absent or malformed.
    """
    if trusted_networks is None:
        trusted_networks = parse_trusted_networks()

    raw_socket_host = request.client.host if request.client else "127.0.0.1"
    clean_socket_host = raw_socket_host.strip("[]")

    try:
        direct_ip = ipaddress.ip_address(clean_socket_host)
    except ValueError:
        return clean_socket_host

    # If the direct peer connection is NOT a trusted proxy, DO NOT trust forwarded headers
    if not is_ip_in_networks(direct_ip, trusted_networks):
        return str(direct_ip)

    # Direct peer connection IS a trusted proxy: inspect X-Forwarded-For
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        raw_hops = [hop.strip().strip("[]") for hop in forwarded_for.split(",") if hop.strip()]
        parsed_hops: list[tuple[str, IPAddress | None]] = []
        for hop in raw_hops:
            try:
                parsed_hops.append((hop, ipaddress.ip_address(hop)))
            except ValueError:
                parsed_hops.append((hop, None))

        # Traverse right-to-left: find first IP not in trusted networks
        for hop_str, hop_ip in reversed(parsed_hops):
            if hop_ip is None:
                # Malformed IP in chain: untrusted
                return hop_str
            if not is_ip_in_networks(hop_ip, trusted_networks):
                return str(hop_ip)

        # All hops were trusted proxies: return leftmost client IP
        if parsed_hops:
            return parsed_hops[0][0]

    # Fallback to X-Real-IP if present
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        clean_real_ip = real_ip.strip().strip("[]")
        try:
            return str(ipaddress.ip_address(clean_real_ip))
        except ValueError:
            pass

    return str(direct_ip)


class BaseRateLimitStorage(ABC):
    """Abstract base class for rate limiter storage backends."""

    @abstractmethod
    async def is_allowed(self, key: str, max_requests: int, window_seconds: int) -> bool:
        """Check if request is allowed and increment counter."""
        pass

    @abstractmethod
    async def cleanup(self, window_seconds: int) -> int:
        """Purge stale timestamps/keys."""
        pass

    @abstractmethod
    async def reset(self) -> None:
        """Reset all state."""
        pass

    @property
    @abstractmethod
    def tracked_key_count(self) -> int:
        """Return number of currently tracked keys."""
        pass


class MemoryRateLimitStorage(BaseRateLimitStorage):
    """Thread/async-safe in-memory sliding window rate limiter storage."""

    def __init__(
        self,
        max_tracked_keys: int = 10_000,
        cleanup_interval_seconds: float = 30.0,
    ) -> None:
        self.max_tracked_keys = max_tracked_keys
        self.cleanup_interval_seconds = cleanup_interval_seconds
        self._requests: dict[str, list[float]] = {}
        self._lock = asyncio.Lock()
        self._last_cleanup: float = time.monotonic()

    def _cleanup_locked(self, cutoff: float) -> int:
        """Remove all keys whose timestamps have all expired (must hold self._lock)."""
        stale_keys: list[str] = []
        for k, timestamps in self._requests.items():
            while timestamps and timestamps[0] <= cutoff:
                timestamps.pop(0)
            if not timestamps:
                stale_keys.append(k)

        for k in stale_keys:
            self._requests.pop(k, None)

        return len(stale_keys)

    async def is_allowed(self, key: str, max_requests: int, window_seconds: int) -> bool:
        now = time.monotonic()
        cutoff = now - window_seconds

        async with self._lock:
            # Periodic inline cleanup
            if now - self._last_cleanup >= self.cleanup_interval_seconds:
                self._cleanup_locked(cutoff)
                self._last_cleanup = now

            # Prune expired timestamps for this key
            timestamps = self._requests.get(key)
            if timestamps is not None:
                valid_timestamps = [t for t in timestamps if t > cutoff]
                if not valid_timestamps:
                    del self._requests[key]
                    timestamps = []
                else:
                    self._requests[key] = valid_timestamps
                    timestamps = valid_timestamps
            else:
                timestamps = []

            # Check threshold
            if len(timestamps) >= max_requests:
                return False

            # Enforce max tracked keys boundary (prevent unbounded memory exhaustion)
            if key not in self._requests and len(self._requests) >= self.max_tracked_keys:
                self._cleanup_locked(cutoff)
                if len(self._requests) >= self.max_tracked_keys:
                    oldest_key = min(
                        self._requests,
                        key=lambda k: self._requests[k][-1] if self._requests[k] else 0.0,
                    )
                    del self._requests[oldest_key]

            # Record timestamp
            if key not in self._requests:
                self._requests[key] = [now]
            else:
                self._requests[key].append(now)

            return True

    async def cleanup(self, window_seconds: int) -> int:
        now = time.monotonic()
        cutoff = now - window_seconds
        async with self._lock:
            removed = self._cleanup_locked(cutoff)
            self._last_cleanup = now
            return removed

    async def reset(self) -> None:
        async with self._lock:
            self._requests.clear()
            self._last_cleanup = time.monotonic()

    @property
    def tracked_key_count(self) -> int:
        return len(self._requests)


class SharedFileRateLimitStorage(BaseRateLimitStorage):
    """Multi-worker synchronized sliding window storage using SQLite in WAL mode.

    Safe across multiple Uvicorn/Gunicorn worker processes without external services.
    Uses BEGIN IMMEDIATE transactions to prevent concurrent check-and-insert race conditions.
    """

    def __init__(
        self,
        db_path: str | None = None,
        cleanup_interval_seconds: float = 30.0,
    ) -> None:
        self.db_path = db_path or settings.RATE_LIMIT_STORAGE_PATH
        self.cleanup_interval_seconds = cleanup_interval_seconds
        self._last_cleanup: float = time.time()
        self._lock = asyncio.Lock()
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15.0, isolation_level=None)
        conn.execute("PRAGMA busy_timeout=15000;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _init_db(self) -> None:
        conn = sqlite3.connect(self.db_path, timeout=15.0, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=15000;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS rate_limit_events (
                    key TEXT NOT NULL,
                    timestamp REAL NOT NULL
                );
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_rate_limit_key_ts
                ON rate_limit_events(key, timestamp);
            """)
        finally:
            conn.close()

    def _sync_is_allowed(self, key: str, max_requests: int, window_seconds: int) -> bool:
        now = time.time()
        cutoff = now - window_seconds

        conn = self._get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            cursor = conn.cursor()

            # Opportunistic cleanup of stale timestamps for this key
            cursor.execute(
                "DELETE FROM rate_limit_events WHERE key = ? AND timestamp <= ?",
                (key, cutoff),
            )

            # Count recent requests
            cursor.execute(
                "SELECT COUNT(*) FROM rate_limit_events WHERE key = ? AND timestamp > ?",
                (key, cutoff),
            )
            count = cursor.fetchone()[0]

            if count >= max_requests:
                conn.execute("COMMIT;")
                return False

            cursor.execute(
                "INSERT INTO rate_limit_events (key, timestamp) VALUES (?, ?)",
                (key, now),
            )
            conn.execute("COMMIT;")
            return True
        except Exception:
            try:
                conn.execute("ROLLBACK;")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    async def is_allowed(self, key: str, max_requests: int, window_seconds: int) -> bool:
        async with self._lock:
            now = time.time()
            if now - self._last_cleanup >= self.cleanup_interval_seconds:
                await asyncio.to_thread(self._sync_cleanup, window_seconds)
                self._last_cleanup = now

            return await asyncio.to_thread(
                self._sync_is_allowed, key, max_requests, window_seconds
            )

    def _sync_cleanup(self, window_seconds: int) -> int:
        cutoff = time.time() - window_seconds
        conn = self._get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            cursor = conn.cursor()
            cursor.execute("DELETE FROM rate_limit_events WHERE timestamp <= ?", (cutoff,))
            deleted = cursor.rowcount
            conn.execute("COMMIT;")
            return max(deleted, 0)
        except Exception:
            try:
                conn.execute("ROLLBACK;")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    async def cleanup(self, window_seconds: int) -> int:
        async with self._lock:
            return await asyncio.to_thread(self._sync_cleanup, window_seconds)

    def _sync_reset(self) -> None:
        conn = self._get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            conn.execute("DELETE FROM rate_limit_events;")
            conn.execute("COMMIT;")
        except Exception:
            try:
                conn.execute("ROLLBACK;")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    async def reset(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._sync_reset)

    def _sync_tracked_key_count(self) -> int:
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(DISTINCT key) FROM rate_limit_events;")
            return cursor.fetchone()[0]
        finally:
            conn.close()

    @property
    def tracked_key_count(self) -> int:
        return self._sync_tracked_key_count()


class _SlidingWindowCounter:
    """Sliding window rate limiter with pluggable memory or shared multi-worker storage."""

    def __init__(
        self,
        max_requests: int,
        window_seconds: int,
        max_tracked_keys: int = 10_000,
        cleanup_interval_seconds: float = 30.0,
        storage: BaseRateLimitStorage | None = None,
    ) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.max_tracked_keys = max_tracked_keys
        self.cleanup_interval_seconds = cleanup_interval_seconds

        if storage is not None:
            self._storage: BaseRateLimitStorage = storage
        elif settings.RATE_LIMIT_STORAGE_TYPE.lower() == "shared":
            self._storage = SharedFileRateLimitStorage(
                db_path=settings.RATE_LIMIT_STORAGE_PATH,
                cleanup_interval_seconds=cleanup_interval_seconds,
            )
        else:
            self._storage = MemoryRateLimitStorage(
                max_tracked_keys=max_tracked_keys,
                cleanup_interval_seconds=cleanup_interval_seconds,
            )

    @property
    def storage(self) -> BaseRateLimitStorage:
        return self._storage

    async def is_allowed(self, key: str) -> bool:
        return await self._storage.is_allowed(key, self.max_requests, self.window_seconds)

    async def cleanup(self) -> int:
        return await self._storage.cleanup(self.window_seconds)

    async def reset(self) -> None:
        await self._storage.reset()

    @property
    def tracked_key_count(self) -> int:
        return self._storage.tracked_key_count


def rate_limit(
    max_requests: int = 10,
    window_seconds: int = 60,
    trusted_proxies: Sequence[str] | str | None = None,
    max_tracked_keys: int | None = None,
    cleanup_interval_seconds: float | None = None,
    storage: BaseRateLimitStorage | None = None,
) -> Callable:
    """Decorator factory applying secure sliding-window rate limiting to a FastAPI route.

    Args:
        max_requests: Maximum requests allowed per IP within the window.
        window_seconds: Duration of the sliding window in seconds.
        trusted_proxies: Optional override for trusted proxy IPs/networks.
        max_tracked_keys: Maximum unique IP keys held in memory before oldest eviction.
        cleanup_interval_seconds: Minimum interval between automatic inline cleanups.
        storage: Optional explicit storage backend (MemoryRateLimitStorage or SharedFileRateLimitStorage).
    """
    effective_max_keys = max_tracked_keys or settings.RATE_LIMIT_MAX_TRACKED_KEYS
    effective_cleanup_interval = cleanup_interval_seconds or settings.RATE_LIMIT_CLEANUP_INTERVAL_SECONDS

    limiter = _SlidingWindowCounter(
        max_requests=max_requests,
        window_seconds=window_seconds,
        max_tracked_keys=effective_max_keys,
        cleanup_interval_seconds=effective_cleanup_interval,
        storage=storage,
    )
    parsed_networks = parse_trusted_networks(trusted_proxies)

    def decorator(func: Callable) -> Callable:
        # Validate that the wrapped route function accepts a Request parameter
        sig = inspect.signature(func)
        has_request_param = any(
            param.annotation is Request or param.name == "request"
            for param in sig.parameters.values()
        )
        if not has_request_param:
            raise RuntimeError(
                f"Function '{func.__name__}' is decorated with @rate_limit but does not "
                f"declare a 'request: Request' parameter. Rate limiting cannot inspect client IP."
            )

        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            if not settings.RATE_LIMIT_ENABLED:
                return await func(*args, **kwargs)

            # Locate Request instance
            request: Request | None = kwargs.get("request")
            if request is None:
                for arg in args:
                    if isinstance(arg, Request):
                        request = arg
                        break

            if request is None:
                raise RuntimeError(
                    f"Unable to find Request instance in arguments for route '{func.__name__}'."
                )

            client_ip = resolve_client_ip(request, parsed_networks)
            allowed = await limiter.is_allowed(client_ip)

            if not allowed:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Too many requests. Please try again later.",
                    headers={"Retry-After": str(window_seconds)},
                )

            return await func(*args, **kwargs)

        # Expose limiter on wrapper for introspection and test control
        wrapper._limiter = limiter  # type: ignore[attr-defined]
        return wrapper

    return decorator
