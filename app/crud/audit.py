"""CRUD queries for audit logs."""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog


async def get_audit_logs(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    branch_ids: set[uuid.UUID] | None = None,
    action: str | None = None,
    resource_type: str | None = None,
    user_id: uuid.UUID | None = None,
    start_date: datetime.datetime | None = None,
    end_date: datetime.datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[int, list[AuditLog]]:
    """Retrieve filtered and paginated audit records for a tenant."""
    base_query = select(AuditLog).where(AuditLog.tenant_id == tenant_id)

    if branch_ids is not None:
        base_query = base_query.where(AuditLog.branch_id.in_(branch_ids))

    if action:
        base_query = base_query.where(AuditLog.action == action)

    if resource_type:
        base_query = base_query.where(AuditLog.resource_type == resource_type)

    if user_id:
        base_query = base_query.where(AuditLog.user_id == user_id)

    if start_date:
        base_query = base_query.where(AuditLog.created_at >= start_date)

    if end_date:
        base_query = base_query.where(AuditLog.created_at <= end_date)

    # Total count
    count_stmt = select(func.count()).select_from(base_query.subquery())
    total_result = await session.execute(count_stmt)
    total = total_result.scalar_one()

    # Paginated records ordered newest first
    stmt = base_query.order_by(desc(AuditLog.created_at)).limit(limit).offset(offset)
    result = await session.execute(stmt)
    items = list(result.scalars().all())

    return total, items
