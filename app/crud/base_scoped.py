"""Row-Level Security (RLS) query helpers for multi-tenancy and branch scoping."""

from __future__ import annotations

import uuid
from typing import Any, TypeVar

from sqlalchemy import false, inspect as sa_inspect
from sqlalchemy.sql import Delete, Select, Update

from app.core.context import SecurityContext

StatementType = TypeVar("StatementType", Select[Any], Update, Delete)


def _safe_inspect(model: Any) -> Any:
    """Safely inspect a model or aliased class without raising compile/inspection errors."""
    try:
        return sa_inspect(model, raiseerr=False)
    except TypeError:
        try:
            return sa_inspect(model)
        except Exception:
            return None
    except Exception:
        return None


def _get_model_name(model: Any) -> str:
    """Safely extract entity/model name from declarative classes, AliasedClass, or tables."""
    if hasattr(model, "__name__"):
        return model.__name__
    insp = _safe_inspect(model)
    if insp is not None:
        if hasattr(insp, "mapper") and hasattr(insp.mapper, "class_"):
            return f"Aliased({insp.mapper.class_.__name__})"
        if hasattr(insp, "class_"):
            return insp.class_.__name__
    return str(model)


def _has_column(model: Any, column_name: str) -> bool:
    """Check if model or aliased class possesses the specified column attribute."""
    if hasattr(model, column_name):
        return True
    insp = _safe_inspect(model)
    if insp is not None:
        if hasattr(insp, "columns") and column_name in insp.columns:
            return True
        if hasattr(insp, "mapper") and hasattr(insp.mapper, "columns") and column_name in insp.mapper.columns:
            return True
    return False


def apply_tenancy_filter(
    statement: StatementType,
    model: Any,
    tenant_id: uuid.UUID,
    *,
    strict: bool = True,
) -> StatementType:
    """Enforce strict tenant boundary on a SQLAlchemy statement (supports models & aliases).

    Args:
        statement: SQLAlchemy select, update, or delete construct.
        model: Declarative model class or aliased() model being targeted.
        tenant_id: Tenant UUID.
        strict: If True (default), raise ValueError when model lacks tenant_id.
            Set to False only when intentionally querying unscoped models.

    Returns:
        Modified statement with tenant filtering applied.

    Raises:
        ValueError: If strict=True and model does not have a tenant_id attribute.
    """
    if _has_column(model, "tenant_id"):
        return statement.where(model.tenant_id == tenant_id)
    if strict:
        model_name = _get_model_name(model)
        raise ValueError(
            f"Model '{model_name}' does not have a 'tenant_id' column. "
            f"Cannot apply tenancy filter. If this model is intentionally unscoped, "
            f"pass strict=False."
        )
    return statement


def apply_branch_scope(
    statement: StatementType,
    model: Any,
    context: SecurityContext | None = None,
    branch_id: uuid.UUID | None = None,
    *,
    strict: bool = True,
) -> StatementType:
    """Enforce granular branch authorization boundary on a SQLAlchemy statement (supports models & aliases).

    Args:
        statement: SQLAlchemy select, update, or delete construct.
        model: Declarative model class or aliased() model being targeted.
        context: Optional SecurityContext representing the actor.
        branch_id: Optional specific branch filter requested.
        strict: If True (default), raise ValueError when model lacks branch_id.
            Set to False only when intentionally querying unscoped models.

    Returns:
        Modified statement with branch scoping applied.

    Raises:
        ValueError: If strict=True and model does not have a branch_id attribute.
        HTTPException(403): If the actor attempts to access an unauthorized branch.
    """
    if not _has_column(model, "branch_id"):
        if strict:
            model_name = _get_model_name(model)
            raise ValueError(
                f"Model '{model_name}' does not have a 'branch_id' column. "
                f"Cannot apply branch scope. If this model is intentionally unscoped, "
                f"pass strict=False."
            )
        return statement

    # If an explicit branch is targeted, validate actor's authorization for that branch (if context provided)
    if branch_id is not None:
        if context is not None:
            context.assert_branch_access(branch_id)
        return statement.where(model.branch_id == branch_id)

    if context is None:
        return statement

    # Super Admin has brand-level visibility across all branches if no specific branch was requested
    if context.is_super_admin:
        return statement

    # For non-super admins (Regional Manager, Branch Admin, Cashier, Kitchen Staff),
    # scope query to their explicitly assigned branches
    if not context.allowed_branch_ids:
        return statement.where(false())

    return statement.where(model.branch_id.in_(context.allowed_branch_ids))

