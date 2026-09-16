"""Comprehensive test suite for Production-Ready Sliding-Window Rate Limiter.

Covers:
- Client IP resolution (Direct clients, Trusted proxies, Untrusted forwarded headers)
- Sliding window storage & multiple clients
- Window expiration & automated cleanup
- Concurrency & high request volume
- Fail-safe decorator behavior (rejecting missing Request parameters)
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException, Request
from httpx import ASGITransport, AsyncClient
from starlette.datastructures import Address, Headers

from app.core.config import settings
from app.core.rate_limit import (
    MemoryRateLimitStorage,
    SharedFileRateLimitStorage,
    _SlidingWindowCounter,
    parse_trusted_networks,
    rate_limit,
    resolve_client_ip,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def make_mock_request(
    client_ip: str | None = "192.168.1.100",
    headers: dict[str, str] | None = None,
) -> Request:
    """Construct a lightweight mock Request for IP resolution testing."""
    scope: dict = {
        "type": "http",
        "method": "GET",
        "path": "/test",
        "headers": [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in (headers or {}).items()],
    }
    if client_ip is not None:
        scope["client"] = (client_ip, 54321)
    else:
        scope["client"] = None

    return Request(scope)


# ---------------------------------------------------------------------------
# 1. Client IP Resolution & Trust Model Tests (Issue #2)
# ---------------------------------------------------------------------------
class TestClientIPResolution:
    """Validates reverse proxy trust modeling and X-Forwarded-For anti-spoofing."""

    def test_direct_client_without_headers(self) -> None:
        """Direct connection without proxy headers returns socket peer IP."""
        req = make_mock_request(client_ip="198.51.100.5")
        trusted = parse_trusted_networks(["127.0.0.1"])
        assert resolve_client_ip(req, trusted) == "198.51.100.5"

    def test_direct_client_untrusted_forwarded_headers_ignored(self) -> None:
        """Untrusted client attempting to spoof X-Forwarded-For must have it ignored."""
        req = make_mock_request(
            client_ip="198.51.100.5",
            headers={"x-forwarded-for": "8.8.8.8, 1.1.1.1"},
        )
        trusted = parse_trusted_networks(["127.0.0.1", "10.0.0.0/8"])
        # 198.51.100.5 is NOT in trusted networks, so 8.8.8.8 is NOT trusted
        assert resolve_client_ip(req, trusted) == "198.51.100.5"

    def test_direct_client_untrusted_real_ip_ignored(self) -> None:
        """Untrusted client attempting to spoof X-Real-IP must have it ignored."""
        req = make_mock_request(
            client_ip="198.51.100.5",
            headers={"x-real-ip": "8.8.8.8"},
        )
        trusted = parse_trusted_networks(["127.0.0.1"])
        assert resolve_client_ip(req, trusted) == "198.51.100.5"

    def test_trusted_reverse_proxy_single_hop(self) -> None:
        """Trusted reverse proxy connection returns genuine client IP from X-Forwarded-For."""
        req = make_mock_request(
            client_ip="127.0.0.1",
            headers={"x-forwarded-for": "203.0.113.195"},
        )
        trusted = parse_trusted_networks(["127.0.0.1"])
        assert resolve_client_ip(req, trusted) == "203.0.113.195"

    def test_trusted_reverse_proxy_multi_hop_chain(self) -> None:
        """Traversing multi-hop proxy chain from right-to-left stops at first untrusted IP."""
        # Chain: real_client, trusted_proxy_internal, connecting_proxy
        req = make_mock_request(
            client_ip="10.0.0.1",  # Connecting peer (trusted)
            headers={"x-forwarded-for": "203.0.113.42, 10.0.0.2"},
        )
        trusted = parse_trusted_networks(["10.0.0.0/8"])
        assert resolve_client_ip(req, trusted) == "203.0.113.42"

    def test_trusted_reverse_proxy_spoofed_client_chain(self) -> None:
        """Attacker prepends spoofed IP to proxy chain; traversal right-to-left identifies genuine client."""
        # Attacker 198.51.100.99 sent X-Forwarded-For: 1.2.3.4 to Nginx 10.0.0.1
        # Nginx appended attacker IP: "1.2.3.4, 198.51.100.99"
        req = make_mock_request(
            client_ip="10.0.0.1",
            headers={"x-forwarded-for": "1.2.3.4, 198.51.100.99"},
        )
        trusted = parse_trusted_networks(["10.0.0.0/8"])
        # Traversal from right finds 198.51.100.99 (untrusted), so 1.2.3.4 is discarded
        assert resolve_client_ip(req, trusted) == "198.51.100.99"

    def test_trusted_proxy_fallback_to_real_ip(self) -> None:
        """Trusted proxy with X-Real-IP and missing X-Forwarded-For resolves correctly."""
        req = make_mock_request(
            client_ip="127.0.0.1",
            headers={"x-real-ip": "198.51.100.77"},
        )
        trusted = parse_trusted_networks(["127.0.0.1"])
        assert resolve_client_ip(req, trusted) == "198.51.100.77"

    def test_trusted_proxy_with_malformed_ip(self) -> None:
        """Malformed or non-IP headers from proxy fallback to socket host or valid hop safely."""
        req = make_mock_request(
            client_ip="127.0.0.1",
            headers={"x-forwarded-for": "invalid-ip-string"},
        )
        trusted = parse_trusted_networks(["127.0.0.1"])
        assert resolve_client_ip(req, trusted) == "invalid-ip-string"

    def test_missing_client_in_request_scope(self) -> None:
        """Request without client scope defaults to 127.0.0.1 safely."""
        req = make_mock_request(client_ip=None)
        assert resolve_client_ip(req) == "127.0.0.1"


# ---------------------------------------------------------------------------
# 2. Sliding Window Counter, Expiration, & Cleanup Tests (Issue #3)
# ---------------------------------------------------------------------------
class TestSlidingWindowStorageAndCleanup:
    """Validates sliding window mechanics, key expiration, and memory management."""

    @pytest.mark.asyncio
    async def test_allows_up_to_max_requests_and_blocks_excess(self) -> None:
        """Requests up to max_requests are allowed; next request is blocked."""
        limiter = _SlidingWindowCounter(max_requests=3, window_seconds=10)

        assert await limiter.is_allowed("192.168.1.1") is True
        assert await limiter.is_allowed("192.168.1.1") is True
        assert await limiter.is_allowed("192.168.1.1") is True
        # 4th request must be blocked
        assert await limiter.is_allowed("192.168.1.1") is False

    @pytest.mark.asyncio
    async def test_multiple_clients_have_independent_buckets(self) -> None:
        """Client A hitting limit does not affect Client B."""
        limiter = _SlidingWindowCounter(max_requests=2, window_seconds=10)

        assert await limiter.is_allowed("client-A") is True
        assert await limiter.is_allowed("client-A") is True
        assert await limiter.is_allowed("client-A") is False

        # Client B still has full allowance
        assert await limiter.is_allowed("client-B") is True
        assert await limiter.is_allowed("client-B") is True
        assert await limiter.is_allowed("client-B") is False

    @pytest.mark.asyncio
    async def test_window_expiration_restores_allowance(self) -> None:
        """After window_seconds elapsed, expired timestamps are pruned and new requests allowed."""
        limiter = _SlidingWindowCounter(max_requests=2, window_seconds=1)

        assert await limiter.is_allowed("10.0.0.1") is True
        assert await limiter.is_allowed("10.0.0.1") is True
        assert await limiter.is_allowed("10.0.0.1") is False

        # Sleep past window
        await asyncio.sleep(1.05)

        # Allowance restored
        assert await limiter.is_allowed("10.0.0.1") is True

    @pytest.mark.asyncio
    async def test_cleanup_purges_stale_keys_and_frees_memory(self) -> None:
        """Explicit cleanup removes keys whose timestamps have expired, reducing tracked count."""
        limiter = _SlidingWindowCounter(max_requests=5, window_seconds=1)

        # Add entries for multiple clients
        for i in range(10):
            await limiter.is_allowed(f"client-{i}")

        assert limiter.tracked_key_count == 10

        # Wait for window to expire
        await asyncio.sleep(1.05)

        # Run cleanup
        removed = await limiter.cleanup()
        assert removed == 10
        assert limiter.tracked_key_count == 0

    @pytest.mark.asyncio
    async def test_idle_key_auto_deleted_on_subsequent_request(self) -> None:
        """A key whose timestamps have expired is immediately deleted on next access."""
        limiter = _SlidingWindowCounter(max_requests=5, window_seconds=1)

        await limiter.is_allowed("test-key")
        assert limiter.tracked_key_count == 1

        await asyncio.sleep(1.05)

        # Accessing after expiry records a new timestamp and does not accumulate stale history
        assert await limiter.is_allowed("test-key") is True
        assert limiter.tracked_key_count == 1

    @pytest.mark.asyncio
    async def test_max_tracked_keys_bounding_under_high_volume(self) -> None:
        """Counter strictly caps memory usage by evicting stale/oldest keys when limit exceeded."""
        max_keys = 20
        limiter = _SlidingWindowCounter(
            max_requests=5,
            window_seconds=60,
            max_tracked_keys=max_keys,
            cleanup_interval_seconds=100.0,  # disable periodic to test emergency cap
        )

        # Insert more keys than max_tracked_keys
        for i in range(50):
            await limiter.is_allowed(f"high-vol-client-{i}")

        # Tracked keys must never exceed max_keys
        assert limiter.tracked_key_count <= max_keys

    @pytest.mark.asyncio
    async def test_concurrency_high_request_volume(self) -> None:
        """Concurrent requests across multiple clients behave deterministically without race conditions."""
        limiter = _SlidingWindowCounter(max_requests=10, window_seconds=5)

        async def send_burst(client_id: str, count: int) -> list[bool]:
            results = []
            for _ in range(count):
                results.append(await limiter.is_allowed(client_id))
            return results

        # 5 clients send 15 requests concurrently each (total 75 requests)
        tasks = [send_burst(f"concurrent-client-{i}", 15) for i in range(5)]
        client_results = await asyncio.gather(*tasks)

        for results in client_results:
            # First 10 allowed, last 5 blocked
            assert results[:10] == [True] * 10
            assert results[10:] == [False] * 5


# ---------------------------------------------------------------------------
# 3. Decorator & HTTP Integration Tests
# ---------------------------------------------------------------------------
class TestRateLimitDecorator:
    """Validates FastAPI route decorator, fail-safe checks, and HTTP responses."""

    def test_decorator_raises_error_if_request_missing_from_signature(self) -> None:
        """Route decorated with @rate_limit without Request parameter raises RuntimeError at definition."""
        with pytest.raises(RuntimeError) as exc_info:
            @rate_limit(max_requests=5, window_seconds=60)
            async def invalid_endpoint(foo: str, bar: int) -> str:
                return "ok"

        assert "declare a 'request: Request' parameter" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_http_endpoint_returns_429_with_retry_after(self) -> None:
        """Exceeding route rate limit returns HTTP 429 Too Many Requests with Retry-After header."""
        app = FastAPI()

        @app.get("/limited")
        @rate_limit(max_requests=2, window_seconds=30)
        async def limited_route(request: Request) -> dict[str, str]:
            return {"status": "ok"}

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp1 = await client.get("/limited")
            assert resp1.status_code == 200

            resp2 = await client.get("/limited")
            assert resp2.status_code == 200

            resp3 = await client.get("/limited")
            assert resp3.status_code == 429
            assert resp3.headers.get("retry-after") == "30"
            assert "Too many requests" in resp3.json()["detail"]

    @pytest.mark.asyncio
    async def test_http_endpoint_untrusted_header_does_not_evade_limit(self) -> None:
        """An untrusted client cannot rotate X-Forwarded-For to evade rate limiting."""
        app = FastAPI()

        # Only 127.0.0.1 is trusted; client connects from 192.168.1.1
        @app.get("/secure-limited")
        @rate_limit(max_requests=2, window_seconds=60, trusted_proxies=["127.0.0.1"])
        async def secure_route(request: Request) -> dict[str, str]:
            return {"status": "ok"}

        transport = ASGITransport(app=app, client=("192.168.1.1", 12345))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Request 1 with spoofed header A
            r1 = await client.get("/secure-limited", headers={"X-Forwarded-For": "1.1.1.1"})
            assert r1.status_code == 200

            # Request 2 with spoofed header B
            r2 = await client.get("/secure-limited", headers={"X-Forwarded-For": "2.2.2.2"})
            assert r2.status_code == 200

            # Request 3 with spoofed header C must still be blocked (keyed to socket IP 192.168.1.1)
            r3 = await client.get("/secure-limited", headers={"X-Forwarded-For": "3.3.3.3"})
            assert r3.status_code == 429


# ---------------------------------------------------------------------------
# 4. Multi-Worker Shared Storage Tests (Issue #8)
# ---------------------------------------------------------------------------
class TestMultiWorkerSharedRateLimiting:
    """Validates multi-worker shared storage rate limiter synchronization."""

    @pytest.mark.asyncio
    async def test_shared_storage_single_worker(self, tmp_path) -> None:
        """Single worker correctly counts and blocks upon reaching max_requests."""
        db_file = str(tmp_path / "ratelimit.db")
        storage = SharedFileRateLimitStorage(db_path=db_file)

        # 3 requests allowed within 60s
        assert await storage.is_allowed("192.168.1.50", max_requests=3, window_seconds=60) is True
        assert await storage.is_allowed("192.168.1.50", max_requests=3, window_seconds=60) is True
        assert await storage.is_allowed("192.168.1.50", max_requests=3, window_seconds=60) is True
        # 4th request must be rejected
        assert await storage.is_allowed("192.168.1.50", max_requests=3, window_seconds=60) is False

    @pytest.mark.asyncio
    async def test_shared_storage_multi_worker_shared_limit(self, tmp_path) -> None:
        """Multiple worker instances sharing the same DB coordinate rate limits atomically."""
        db_file = str(tmp_path / "ratelimit_multi.db")

        # Simulate Worker 1 and Worker 2 processes
        worker_1 = SharedFileRateLimitStorage(db_path=db_file)
        worker_2 = SharedFileRateLimitStorage(db_path=db_file)

        client_ip = "192.168.1.100"

        # Worker 1 processes request 1 -> OK
        assert await worker_1.is_allowed(client_ip, max_requests=3, window_seconds=60) is True
        # Worker 2 processes request 2 -> OK
        assert await worker_2.is_allowed(client_ip, max_requests=3, window_seconds=60) is True
        # Worker 1 processes request 3 -> OK (limit reached)
        assert await worker_1.is_allowed(client_ip, max_requests=3, window_seconds=60) is True

        # Worker 2 processes request 4 -> BLOCKED across workers!
        assert await worker_2.is_allowed(client_ip, max_requests=3, window_seconds=60) is False
        # Worker 1 processes request 5 -> BLOCKED
        assert await worker_1.is_allowed(client_ip, max_requests=3, window_seconds=60) is False

    @pytest.mark.asyncio
    async def test_shared_storage_multiple_clients(self, tmp_path) -> None:
        """Different client IPs are tracked independently in shared storage."""
        db_file = str(tmp_path / "ratelimit_clients.db")
        storage = SharedFileRateLimitStorage(db_path=db_file)

        # Max 2 requests per client
        assert await storage.is_allowed("10.0.0.1", max_requests=2, window_seconds=60) is True
        assert await storage.is_allowed("10.0.0.1", max_requests=2, window_seconds=60) is True
        assert await storage.is_allowed("10.0.0.1", max_requests=2, window_seconds=60) is False

        # Client 2 is completely unaffected
        assert await storage.is_allowed("10.0.0.2", max_requests=2, window_seconds=60) is True
        assert await storage.is_allowed("10.0.0.2", max_requests=2, window_seconds=60) is True
        assert await storage.is_allowed("10.0.0.2", max_requests=2, window_seconds=60) is False

    @pytest.mark.asyncio
    async def test_shared_storage_window_expiration(self, tmp_path) -> None:
        """Expired entries in shared storage allow new requests once the window elapses."""
        db_file = str(tmp_path / "ratelimit_exp.db")
        storage = SharedFileRateLimitStorage(db_path=db_file)

        client_ip = "192.168.1.75"
        base_time = 1_700_000_000.0

        with patch("time.time", return_value=base_time):
            # 60-second window with max 1 request
            assert await storage.is_allowed(client_ip, max_requests=1, window_seconds=60) is True
            assert await storage.is_allowed(client_ip, max_requests=1, window_seconds=60) is False

        # Advance time past the 60-second window
        with patch("time.time", return_value=base_time + 61.0):
            # Now allowed again
            assert await storage.is_allowed(client_ip, max_requests=1, window_seconds=60) is True

    @pytest.mark.asyncio
    async def test_shared_storage_cleanup_and_reset(self, tmp_path) -> None:
        """cleanup() purges expired records and reset() clears all records."""
        db_file = str(tmp_path / "ratelimit_clean.db")
        storage = SharedFileRateLimitStorage(db_path=db_file)
        base_time = 1_700_000_000.0

        with patch("time.time", return_value=base_time):
            await storage.is_allowed("1.1.1.1", max_requests=10, window_seconds=60)
            await storage.is_allowed("2.2.2.2", max_requests=10, window_seconds=60)
            assert storage.tracked_key_count == 2

        # Advance time past 60s and cleanup
        with patch("time.time", return_value=base_time + 65.0):
            cleaned = await storage.cleanup(window_seconds=60)
            assert cleaned == 2
            assert storage.tracked_key_count == 0

        # Reset
        await storage.is_allowed("3.3.3.3", max_requests=10, window_seconds=60)
        await storage.reset()
        assert storage.tracked_key_count == 0

    @pytest.mark.asyncio
    async def test_shared_storage_concurrent_requests_across_workers(self, tmp_path) -> None:
        """Concurrent requests across simulated workers respect the exact threshold without race conditions."""
        db_file = str(tmp_path / "ratelimit_concurrency.db")
        workers = [SharedFileRateLimitStorage(db_path=db_file) for _ in range(3)]

        max_limit = 5
        total_requests = 12
        client_ip = "192.168.1.200"

        async def make_call(worker_idx: int) -> bool:
            return await workers[worker_idx % len(workers)].is_allowed(
                client_ip,
                max_requests=max_limit,
                window_seconds=60,
            )

        tasks = [make_call(i) for i in range(total_requests)]
        results = await asyncio.gather(*tasks)

        allowed_count = sum(1 for r in results if r is True)
        blocked_count = sum(1 for r in results if r is False)

        assert allowed_count == max_limit
        assert blocked_count == total_requests - max_limit

    @pytest.mark.asyncio
    async def test_http_endpoint_with_shared_storage(self, tmp_path) -> None:
        """Route decorated with rate_limit using shared storage enforces limits across worker instances."""
        db_file = str(tmp_path / "ratelimit_http.db")
        shared_storage = SharedFileRateLimitStorage(db_path=db_file)

        app = FastAPI()

        @app.get("/shared-limited")
        @rate_limit(max_requests=2, window_seconds=30, storage=shared_storage)
        async def shared_route(request: Request) -> dict[str, str]:
            return {"status": "ok"}

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp1 = await client.get("/shared-limited")
            assert resp1.status_code == 200

            resp2 = await client.get("/shared-limited")
            assert resp2.status_code == 200

            resp3 = await client.get("/shared-limited")
            assert resp3.status_code == 429
            assert resp3.headers.get("retry-after") == "30"

