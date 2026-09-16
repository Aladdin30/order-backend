"""Decoupled asynchronous audit logging engine."""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

from app.core.config import settings
from app.core.database import async_session_factory
from app.models.audit import AuditLog

if TYPE_CHECKING:
    from app.core.context import SecurityContext

logger = logging.getLogger(__name__)


class AuditLogger:
    """Asynchronous audit logger providing non-blocking, fail-safe audit persistence."""

    @staticmethod
    async def log(
        tenant_id: uuid.UUID,
        action: str,
        resource_type: str,
        *,
        branch_id: uuid.UUID | None = None,
        user_id: uuid.UUID | None = None,
        actor_role: str | None = None,
        resource_id: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        changes: dict[str, Any] | None = None,
        status: str = "SUCCESS",
    ) -> AuditLog | None:
        """Write an audit entry in a dedicated decoupled transaction.

        Any exception is logged and swallowed so business operations are never halted.
        """
        if not settings.AUDIT_ENABLED:
            return None

        try:
            audit_entry = AuditLog(
                tenant_id=tenant_id,
                branch_id=branch_id,
                user_id=user_id,
                actor_role=actor_role,
                action=action,
                resource_type=resource_type,
                resource_id=str(resource_id) if resource_id is not None else None,
                ip_address=ip_address,
                user_agent=user_agent,
                changes=changes or {},
                status=status,
            )

            async with async_session_factory() as session:
                session.add(audit_entry)
                await session.commit()
                return audit_entry

        except Exception as exc:
            logger.warning(
                "AuditLogger failed to persist audit record for action '%s': %s",
                action,
                exc,
                exc_info=True,
            )
            return None

    @classmethod
    async def log_from_context(
        cls,
        context: SecurityContext | None,
        action: str,
        resource_type: str,
        *,
        tenant_id: uuid.UUID | None = None,
        branch_id: uuid.UUID | None = None,
        resource_id: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        changes: dict[str, Any] | None = None,
        status: str = "SUCCESS",
    ) -> AuditLog | None:
        """Convenience method resolving actor metadata from an active SecurityContext."""
        resolved_tenant_id = context.tenant_id if context else tenant_id
        if resolved_tenant_id is None:
            logger.warning("AuditLogger requires a tenant_id; log omitted for action '%s'", action)
            return None

        user_id = context.user.id if context and context.user else None
        actor_role = context.role.value if context else "ANONYMOUS"

        return await cls.log(
            tenant_id=resolved_tenant_id,
            action=action,
            resource_type=resource_type,
            branch_id=branch_id,
            user_id=user_id,
            actor_role=actor_role,
            resource_id=resource_id,
            ip_address=ip_address,
            user_agent=user_agent,
            changes=changes,
            status=status,
        )
