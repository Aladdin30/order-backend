"""Financials API: Cash drawer shift management and End-of-Day Z-Reports."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_active_user, get_db, require_roles
from app.models.auth import User
from app.models.enums import UserRole
from app.schemas.financials import (
    CashDrawerSessionResponse,
    DrawerCloseRequest,
    DrawerOpenRequest,
    ZReportGenerateRequest,
    ZReportListResponse,
    ZReportResponse,
)
from app.services.financial_service import FinancialService

router = APIRouter(tags=["financials"])
financials_router = router

DRAWER_ROLES = [
    UserRole.CASHIER,
    UserRole.BRANCH_ADMIN,
    UserRole.SUPER_ADMIN,
]

MANAGEMENT_ROLES = [
    UserRole.BRANCH_ADMIN,
    UserRole.REGIONAL_MANAGER,
    UserRole.SUPER_ADMIN,
]


def _extract_and_validate_branch_id(
    x_branch_id: str | None,
    current_user: User,
) -> uuid.UUID:
    """Validate presence, format, and multi-tenant authorization of X-Branch-ID."""
    if not x_branch_id or not x_branch_id.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing required X-Branch-ID header.",
        )

    try:
        branch_id = uuid.UUID(x_branch_id.strip())
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid UUID format for X-Branch-ID header.",
        )

    if current_user.role == UserRole.SUPER_ADMIN:
        return branch_id

    # Check primary assigned branch
    user_branch_id = getattr(current_user, "branch_id", None)
    if user_branch_id == branch_id:
        return branch_id

    # Check multi-branch access list
    if (
        hasattr(current_user, "branch_access")
        and current_user.branch_access
        and any(ba.branch_id == branch_id for ba in current_user.branch_access)
    ):
        return branch_id

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="You do not have access to this branch.",
    )


# ---------------------------------------------------------------------------
# Drawer Endpoints
# ---------------------------------------------------------------------------

@router.post(
    "/drawer/open",
    response_model=CashDrawerSessionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Open a new cash drawer shift",
)
async def open_drawer(
    payload: DrawerOpenRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_roles(DRAWER_ROLES))],
    x_branch_id: Annotated[str | None, Header(alias="X-Branch-ID")] = None,
) -> CashDrawerSessionResponse:
    """Initialize a cash drawer shift with an opening balance. Permitted: CASHIER, BRANCH_ADMIN, SUPER_ADMIN."""
    branch_id = _extract_and_validate_branch_id(x_branch_id, current_user)
    return await FinancialService.open_cash_drawer(
        branch_id=branch_id,
        user_id=current_user.id,
        opening_balance=payload.opening_balance,
        db=db,
    )


@router.post(
    "/drawer/close",
    response_model=CashDrawerSessionResponse,
    status_code=status.HTTP_200_OK,
    summary="Close the active cash drawer shift and reconcile cash",
)
async def close_drawer(
    payload: DrawerCloseRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_roles(DRAWER_ROLES))],
    x_branch_id: Annotated[str | None, Header(alias="X-Branch-ID")] = None,
) -> CashDrawerSessionResponse:
    """Close active shift, reconcile declared physical cash against ledger, compute variance."""
    branch_id = _extract_and_validate_branch_id(x_branch_id, current_user)
    return await FinancialService.close_cash_drawer(
        branch_id=branch_id,
        user_id=current_user.id,
        declared_cash_amount=payload.declared_cash_amount,
        closing_notes=payload.closing_notes,
        db=db,
    )


@router.get(
    "/drawer/current",
    response_model=CashDrawerSessionResponse,
    status_code=status.HTTP_200_OK,
    summary="Get current active drawer session",
)
async def get_current_drawer(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_roles(DRAWER_ROLES))],
    x_branch_id: Annotated[str | None, Header(alias="X-Branch-ID")] = None,
) -> CashDrawerSessionResponse:
    """Fetch the active open drawer session for the branch (returns 404 if none is open)."""
    branch_id = _extract_and_validate_branch_id(x_branch_id, current_user)
    session = await FinancialService.get_current_drawer_session(branch_id=branch_id, db=db)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No active cash drawer session found for this branch.",
        )
    return session


# ---------------------------------------------------------------------------
# Z-Report Endpoints
# ---------------------------------------------------------------------------

@router.post(
    "/z-report/generate",
    response_model=ZReportResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Generate and freeze End-of-Day Z-Report",
)
async def generate_z_report(
    payload: ZReportGenerateRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_roles(MANAGEMENT_ROLES))],
    x_branch_id: Annotated[str | None, Header(alias="X-Branch-ID")] = None,
) -> ZReportResponse:
    """Generate immutable financial closing snapshot. Permitted: BRANCH_ADMIN, REGIONAL_MANAGER, SUPER_ADMIN."""
    branch_id = _extract_and_validate_branch_id(x_branch_id, current_user)
    return await FinancialService.generate_z_report(
        branch_id=branch_id,
        user_id=current_user.id,
        request=payload,
        db=db,
    )


@router.get(
    "/z-report/{report_id}",
    response_model=ZReportResponse,
    status_code=status.HTTP_200_OK,
    summary="Get historical Z-Report by ID",
)
async def get_z_report(
    report_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_roles(MANAGEMENT_ROLES))],
    x_branch_id: Annotated[str | None, Header(alias="X-Branch-ID")] = None,
) -> ZReportResponse:
    """Retrieve historical Z-Report for branch. Permitted: BRANCH_ADMIN, REGIONAL_MANAGER, SUPER_ADMIN."""
    branch_id = _extract_and_validate_branch_id(x_branch_id, current_user)
    return await FinancialService.get_z_report_by_id(
        branch_id=branch_id,
        report_id=report_id,
        db=db,
    )


@router.get(
    "/z-reports",
    response_model=ZReportListResponse,
    status_code=status.HTTP_200_OK,
    summary="List paginated historical Z-Reports",
)
async def list_z_reports(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_roles(MANAGEMENT_ROLES))],
    x_branch_id: Annotated[str | None, Header(alias="X-Branch-ID")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ZReportListResponse:
    """List historical Z-Reports for branch. Permitted: BRANCH_ADMIN, REGIONAL_MANAGER, SUPER_ADMIN."""
    branch_id = _extract_and_validate_branch_id(x_branch_id, current_user)
    return await FinancialService.list_z_reports(
        branch_id=branch_id,
        limit=limit,
        offset=offset,
        db=db,
    )
