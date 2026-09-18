"""Analytics API: Super Admin and Regional Manager cross-branch analytics & menu engineering."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require_roles
from app.models.auth import User
from app.models.enums import UserRole
from app.schemas.analytics import (
    BranchesMatrixResponse,
    DashboardConsolidatedResponse,
    MenuPerformanceResponse,
    TimePeriod,
)
from app.services.analytics_service import AnalyticsService

router = APIRouter(tags=["analytics"])
analytics_router = router

ANALYTICS_ROLES = [
    UserRole.SUPER_ADMIN,
    UserRole.BRAND_ADMIN,
    UserRole.REGIONAL_MANAGER,
]


def _verify_regional_manager_branch_scope(user: User, branch_id: uuid.UUID | None) -> None:
    """Verify that a regional manager or brand admin has explicit access to the queried branch."""
    if branch_id is None or user.role in (UserRole.SUPER_ADMIN, UserRole.BRAND_ADMIN):
        return


    user_branch_id = getattr(user, "branch_id", None)
    if user_branch_id == branch_id:
        return

    if (
        hasattr(user, "branch_access")
        and user.branch_access
        and any(ba.branch_id == branch_id for ba in user.branch_access)
    ):
        return

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Branch falls outside of assigned regional management scope.",
    )


@router.get(
    "/dashboard",
    response_model=DashboardConsolidatedResponse,
    status_code=status.HTTP_200_OK,
    summary="Get consolidated executive analytics dashboard",
)
async def get_analytics_dashboard(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_roles(ANALYTICS_ROLES))],
    period: Annotated[TimePeriod, Query()] = TimePeriod.LAST_30_DAYS,
    branch_id: Annotated[uuid.UUID | None, Query()] = None,
    start_date: Annotated[datetime | None, Query()] = None,
    end_date: Annotated[datetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 10,
    force_refresh: Annotated[bool, Query()] = False,
) -> DashboardConsolidatedResponse:
    """Retrieve enterprise KPIs, top and bottom moving menu items, and branch rankings."""
    _verify_regional_manager_branch_scope(current_user, branch_id)
    return await AnalyticsService.get_dashboard(
        db=db,
        branch_id=branch_id,
        period=period,
        start_date=start_date,
        end_date=end_date,
        limit=limit,
        force_refresh=force_refresh,
    )


@router.get(
    "/menu-performance",
    response_model=MenuPerformanceResponse,
    status_code=status.HTTP_200_OK,
    summary="Get menu engineering performance and dead-stock analytics",
)
async def get_menu_performance(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_roles(ANALYTICS_ROLES))],
    period: Annotated[TimePeriod, Query()] = TimePeriod.LAST_30_DAYS,
    branch_id: Annotated[uuid.UUID | None, Query()] = None,
    start_date: Annotated[datetime | None, Query()] = None,
    end_date: Annotated[datetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 10,
) -> MenuPerformanceResponse:
    """Retrieve top-selling items, underperforming/dead-stock items, and category revenue distribution."""
    _verify_regional_manager_branch_scope(current_user, branch_id)
    return await AnalyticsService.get_menu_performance(
        db=db,
        branch_id=branch_id,
        period=period,
        start_date=start_date,
        end_date=end_date,
        limit=limit,
    )


@router.get(
    "/branches-matrix",
    response_model=BranchesMatrixResponse,
    status_code=status.HTTP_200_OK,
    summary="Get cross-branch comparative ranking matrix",
)
async def get_branches_matrix(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_roles(ANALYTICS_ROLES))],
    period: Annotated[TimePeriod, Query()] = TimePeriod.LAST_30_DAYS,
    start_date: Annotated[datetime | None, Query()] = None,
    end_date: Annotated[datetime | None, Query()] = None,
) -> BranchesMatrixResponse:
    """Retrieve ranked comparative performance across all active branches by GMV."""
    return await AnalyticsService.get_branches_matrix(
        db=db,
        period=period,
        start_date=start_date,
        end_date=end_date,
    )
