"""FastAPI middleware for automated HTTP mutation and security access auditing."""

from __future__ import annotations

import logging
import time
import uuid
from typing import Callable

import jwt
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.config import settings
from app.services.audit_service import AuditLogger

logger = logging.getLogger(__name__)


class AuditMiddleware(BaseHTTPMiddleware):
    """Intercept HTTP requests to record mutations and security exceptions (401/403)."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if not settings.AUDIT_ENABLED:
            return await call_next(request)

        start_time = time.perf_counter()
        client_ip = self._get_client_ip(request)
        user_agent = request.headers.get("user-agent", "unknown")
        method = request.method
        path = request.url.path

        # Attempt extracting actor context from Authorization token or headers
        token_claims = self._extract_token_claims(request)
        tenant_id = token_claims.get("tenant_id") or self._extract_uuid(request.headers.get("X-Tenant-ID"))
        user_id = token_claims.get("user_id")
        actor_role = token_claims.get("role") or "ANONYMOUS"
        branch_id = self._extract_uuid(request.headers.get("X-Branch-ID"))

        response = await call_next(request)

        duration_ms = round((time.perf_counter() - start_time) * 1000, 2)

        # Check for request state overrides set by inner dependencies/routes
        if hasattr(request.state, "tenant_id") and request.state.tenant_id:
            tenant_id = request.state.tenant_id
        if hasattr(request.state, "branch_id") and request.state.branch_id:
            branch_id = request.state.branch_id
        if hasattr(request.state, "user_id") and request.state.user_id:
            user_id = request.state.user_id

        status_code = response.status_code

        # Condition 1: Security access failure (401 Unauthorized or 403 Forbidden)
        if status_code in (401, 403) and tenant_id:
            action = "AUTH_UNAUTHORIZED" if status_code == 401 else "ACCESS_FORBIDDEN"
            await AuditLogger.log(
                tenant_id=tenant_id,
                action=action,
                resource_type="HTTP_ENDPOINT",
                branch_id=branch_id,
                user_id=user_id,
                actor_role=actor_role,
                resource_id=path,
                ip_address=client_ip,
                user_agent=user_agent,
                changes={
                    "method": method,
                    "path": path,
                    "status_code": status_code,
                    "latency_ms": duration_ms,
                },
                status="BLOCKED" if status_code == 403 else "FAILED",
            )

        # Condition 2: Mutation requests (POST, PUT, PATCH, DELETE)
        elif method in ("POST", "PUT", "PATCH", "DELETE") and tenant_id:
            # Skip redundant logging if route explicitly handles domain action or for auth token
            if not path.endswith("/auth/token"):
                clean_path = path.strip("/").replace("/", "_").upper()
                action = f"HTTP_{method}_{clean_path}"
                status_label = "SUCCESS" if status_code < 400 else "FAILED"
                await AuditLogger.log(
                    tenant_id=tenant_id,
                    action=action,
                    resource_type="HTTP_MUTATION",
                    branch_id=branch_id,
                    user_id=user_id,
                    actor_role=actor_role,
                    resource_id=path,
                    ip_address=client_ip,
                    user_agent=user_agent,
                    changes={
                        "method": method,
                        "path": path,
                        "status_code": status_code,
                        "latency_ms": duration_ms,
                    },
                    status=status_label,
                )

        return response

    def _get_client_ip(self, request: Request) -> str:
        """Resolve originating client IP addressing proxies."""
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
        real_ip = request.headers.get("x-real-ip")
        if real_ip:
            return real_ip.strip()
        if request.client:
            return request.client.host
        return "127.0.0.1"

    def _extract_token_claims(self, request: Request) -> dict[str, Any]:
        """Extract unverified claims from Bearer token for observational audit logging."""
        auth_header = request.headers.get("authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            return {}

        token = auth_header[7:].strip()
        try:
            # Read claims without verifying signature/expiration solely for logging context
            unverified = jwt.decode(token, options={"verify_signature": False})
            claims: dict[str, Any] = {}
            if "tenant_id" in unverified:
                claims["tenant_id"] = self._extract_uuid(unverified["tenant_id"])
            if "sub" in unverified:
                claims["user_id"] = self._extract_uuid(unverified["sub"])
            if "role" in unverified:
                claims["role"] = unverified["role"]
            return claims
        except Exception:
            return {}

    def _extract_uuid(self, value: str | None) -> uuid.UUID | None:
        """Safely parse a UUID string."""
        if not value:
            return None
        try:
            return uuid.UUID(value)
        except (ValueError, TypeError):
            return None
