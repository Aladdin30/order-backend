"""Row-Level Security (RLS) query helpers for multi-tenancy and branch scoping."""

from __future__ import annotations

import uuid
from typing import Any, TypeVar

from sqlalchemy import false
from sqlalchemy.sql import Delete, Select, Update

from app.core.context import SecurityContext

StatementType = TypeVar("StatementType", Select[Any], Update, Delete)


def apply_tenancy_filter(
    statement: StatementType,
    model: Any,
    tenant_id: uuid.UUID,
) -> StatementType:
    """Enforce strict tenant boundary on a SQLAlchemy statement.

    Args:
        statement: SQLAlchemy select, update, or delete construct.
        model: Declarative model class being targeted.
        tenant_id: Tenant UUID.

    Returns:
        Modified statement with tenant filtering applied.
    """
    if hasattr(model, "tenant_id"):
        return statement.where(model.tenant_id == tenant_id)
    return statement


def apply_branch_scope(
    statement: StatementType,
    model: Any,
    context: SecurityContext,
    branch_id: uuid.UUID | None = None,
) -> StatementType:
    """Enforce granular branch authorization boundary on a SQLAlchemy statement.

    Args:
        statement: SQLAlchemy select, update, or delete construct.
        model: Declarative model class being targeted.
        context: SecurityContext representing the actor.
        branch_id: Optional specific branch filter requested.

    Returns:
        Modified statement with branch scoping applied.

    Raises:
        HTTPException(403): If the actor attempts to access an unauthorized branch.
    """
    if not hasattr(model, "branch_id"):
        return statement

    # If an explicit branch is targeted, validate actor's authorization for that branch
    if branch_id is not None:
        context.assert_branch_access(branch_id)
        return statement.where(model.branch_id == branch_id)

    # Super Admin has brand-level visibility across all branches if no specific branch was requested
    if context.is_super_admin:
        return statement

    # For non-super admins (Regional Manager, Branch Admin, Cashier, Kitchen Staff),
    # scope query to their explicitly assigned branches
    if not context.allowed_branch_ids:
        return statement.where(false())

    return statement.where(model.branch_id.in_(context.allowed_branch_ids))
