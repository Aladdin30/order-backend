"""Audit query endpoints for administrative and managerial compliance oversight."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import RequireRoles, get_async_db
from app.core.context import SecurityContext
from app.crud.audit import get_audit_logs
from app.models.enums import UserRole
from app.schemas.audit import AuditLogListResponse, AuditLogResponse

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get(
    "/logs",
    response_model=AuditLogListResponse,
    summary="Query Audit Logs",
    description="Retrieve tenant audit records with branch scoping. Restricted to SUPER_ADMIN and REGIONAL_MANAGER.",
)
async def query_audit_logs(
    context: Annotated[
        SecurityContext,
        Depends(RequireRoles([UserRole.SUPER_ADMIN, UserRole.REGIONAL_MANAGER])),
    ],
    db: Annotated[AsyncSession, Depends(get_async_db)],
    branch_id: uuid.UUID | None = Query(None, description="Filter logs by specific branch UUID"),
    action: str | None = Query(None, description="Filter logs by action name"),
    resource_type: str | None = Query(None, description="Filter logs by affected resource type"),
    user_id: uuid.UUID | None = Query(None, description="Filter logs by user UUID"),
    limit: int = Query(50, ge=1, le=100, description="Items per page"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
) -> AuditLogListResponse:
    """Retrieve audit log stream honoring multi-tenant and branch boundaries."""
    # Resolve authorized branch scope
    scoped_branch_ids: set[uuid.UUID] | None = None

    if branch_id is not None:
        # Validate that the requested branch is within actor's authorization
        context.assert_branch_access(branch_id)
        scoped_branch_ids = {branch_id}
    elif not context.is_super_admin:
        # Regional manager is scoped to their explicitly authorized branches
        scoped_branch_ids = set(context.allowed_branch_ids)

    total, items = await get_audit_logs(
        db,
        tenant_id=context.tenant_id,
        branch_ids=scoped_branch_ids,
        action=action,
        resource_type=resource_type,
        user_id=user_id,
        limit=limit,
        offset=offset,
    )

    return AuditLogListResponse(
        total=total,
        items=[AuditLogResponse.model_validate(item) for item in items],
    )
