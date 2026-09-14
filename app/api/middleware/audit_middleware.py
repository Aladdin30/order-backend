"""FastAPI middleware for automated HTTP mutation and security access auditing."""

from __future__ import annotations

import inspect
import logging
import time
import uuid
from typing import Any, Callable

from starlette.background import BackgroundTask
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.config import settings
from app.core.rate_limit import resolve_client_ip
from app.services.audit_service import AuditLogger

logger = logging.getLogger(__name__)


class AuditMiddleware(BaseHTTPMiddleware):
    """Intercept HTTP requests to record mutations and security exceptions (401/403).

    Uses only verified claims from request.state (populated by downstream
    authentication dependencies) to prevent audit log poisoning from forged tokens.
    Audit writes are offloaded to background tasks to avoid blocking responses.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if not settings.AUDIT_ENABLED:
            return await call_next(request)

        start_time = time.perf_counter()
        client_ip = self._get_client_ip(request)
        user_agent = request.headers.get("user-agent", "unknown")
        method = request.method
        path = request.url.path

        response = await call_next(request)

        duration_ms = round((time.perf_counter() - start_time) * 1000, 2)

        # Extract only verified actor context from request.state
        # These are set by downstream auth dependencies (get_current_user_context)
        tenant_id = getattr(request.state, "tenant_id", None)
        user_id = getattr(request.state, "user_id", None)
        branch_id = getattr(request.state, "branch_id", None)
        actor_role = getattr(request.state, "actor_role", None) or "ANONYMOUS"

        status_code = response.status_code
        audit_task: BackgroundTask | None = None

        # Condition 1: Security access failure (401 Unauthorized or 403 Forbidden)
        if status_code in (401, 403):
            action = "AUTH_UNAUTHORIZED" if status_code == 401 else "ACCESS_FORBIDDEN"
            if tenant_id is not None:
                audit_task = BackgroundTask(
                    AuditLogger.log,
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
            else:
                # Security logging for unauthenticated 401 events where no verified tenant exists
                logger.warning(
                    "SECURITY AUDIT [%s]: Unauthorized access attempt to %s %s from IP %s (User-Agent: %s, duration: %.2fms)",
                    action,
                    method,
                    path,
                    client_ip,
                    user_agent,
                    duration_ms,
                )

        # Condition 2: Mutation requests (POST, PUT, PATCH, DELETE)
        elif method in ("POST", "PUT", "PATCH", "DELETE") and tenant_id is not None:
            # Skip redundant logging for auth token endpoint
            if not path.endswith("/auth/token"):
                clean_path = path.strip("/").replace("/", "_").upper()
                action = f"HTTP_{method}_{clean_path}"
                status_label = "SUCCESS" if status_code < 400 else "FAILED"
                audit_task = BackgroundTask(
                    AuditLogger.log,
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

        # Attach audit as background task with fault isolation
        if audit_task is not None:
            isolated_audit_task = BackgroundTask(safe_execute_background_task, audit_task)
            existing_bg = response.background
            if existing_bg is not None:
                response.background = BackgroundTask(
                    _chain_background_tasks, existing_bg, isolated_audit_task
                )
            else:
                response.background = isolated_audit_task

        return response

    def _get_client_ip(self, request: Request) -> str:
        """Resolve originating client IP addressing proxies securely."""
        return resolve_client_ip(request)


async def safe_execute_background_task(
    task: Any,
    *args: Any,
    task_name: str | None = None,
    **kwargs: Any,
) -> None:
    """Execute an independent background task with exception isolation and structured logging."""
    if task is None:
        return
    resolved_name = task_name or getattr(task, "func", getattr(task, "__name__", repr(task)))
    try:
        if callable(task):
            res = task(*args, **kwargs)
            if inspect.isawaitable(res):
                await res
    except Exception as exc:
        logger.exception(
            "Audit background task '%s' failed: %s",
            resolved_name,
            exc,
            extra={"task_target": str(resolved_name), "error": str(exc)},
        )


async def _chain_background_tasks(*tasks: Any) -> None:
    """Execute multiple background tasks sequentially with complete fault isolation."""
    for task in tasks:
        await safe_execute_background_task(task)

