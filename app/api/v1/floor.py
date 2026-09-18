"""Floor API: Live dining table state, occupancy aggregation, and floor summary endpoints."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_active_user, get_db, require_roles
from app.models.auth import User
from app.models.enums import UserRole
from app.schemas.floor import FloorSummaryResponse
from app.services.floor_table_service import FloorTableService

router = APIRouter(prefix="/tables", tags=["floor"])
floor_router = router


@router.get(
    "/live",
    response_model=FloorSummaryResponse,
    status_code=status.HTTP_200_OK,
    summary="Get live floor state with aggregated tables and occupancy",
)
async def get_live_floor_state(
    x_branch_id: Annotated[uuid.UUID, Header(alias="X-Branch-ID")],
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[
        User,
        Depends(
            require_roles(
                [
                    UserRole.WAITER,
                    UserRole.CASHIER,
                    UserRole.BRANCH_ADMIN,
                    UserRole.SUPER_ADMIN,
                ]
            )
        ),
    ],
) -> FloorSummaryResponse:
    """Retrieve real-time aggregated dining table occupancy, active orders, and service requests."""
    # Validate multi-tenant branch ownership
    if current_user.role != UserRole.SUPER_ADMIN:
        user_branch_id = getattr(current_user, "branch_id", None)
        has_access = False
        if user_branch_id == x_branch_id:
            has_access = True
        elif (
            hasattr(current_user, "branch_access")
            and current_user.branch_access
            and any(ba.branch_id == x_branch_id for ba in current_user.branch_access)
        ):
            has_access = True

        if not has_access:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access forbidden: user is not assigned to this branch",
            )

    return await FloorTableService.get_branch_live_tables(
        branch_id=x_branch_id,
        db=db,
    )
