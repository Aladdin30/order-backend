"""Staff Kitchen Station Management API Endpoints.

Administrative CRUD endpoints for dynamic branch kitchen stations.
Guarded by RBAC (BRANCH_ADMIN, SUPER_ADMIN) and branch scoping.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import EnforceBranchAccess, RequireRoles, get_async_db
from app.core.context import SecurityContext
from app.models.enums import UserRole
from app.schemas.kitchen_station import (
    CreateKitchenStationRequest,
    KitchenStationResponse,
    UpdateKitchenStationRequest,
)
from app.services.kitchen_station_service import KitchenStationService

router = APIRouter(prefix="/staff/kitchen-stations", tags=["Staff Kitchen Stations"])


@router.post(
    "",
    response_model=KitchenStationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create Kitchen Station",
    description="Create a dynamic kitchen station scoped to the authorized branch.",
)
async def create_kitchen_station(
    payload: CreateKitchenStationRequest,
    db: Annotated[AsyncSession, Depends(get_async_db)],
    context: Annotated[
        SecurityContext,
        Depends(RequireRoles([UserRole.BRANCH_ADMIN, UserRole.SUPER_ADMIN])),
    ],
    branch_id: Annotated[uuid.UUID, Depends(EnforceBranchAccess())],
) -> KitchenStationResponse:
    """Create a new custom kitchen station for the caller's branch."""
    return await KitchenStationService.create_station(
        db=db,
        tenant_id=context.tenant_id,
        branch_id=branch_id,
        payload=payload,
    )


@router.get(
    "",
    response_model=list[KitchenStationResponse],
    status_code=status.HTTP_200_OK,
    summary="List Kitchen Stations",
    description="List all kitchen stations for the authorized branch. Defaults to active only.",
)
async def list_kitchen_stations(
    db: Annotated[AsyncSession, Depends(get_async_db)],
    context: Annotated[
        SecurityContext,
        Depends(RequireRoles([UserRole.BRANCH_ADMIN, UserRole.SUPER_ADMIN])),
    ],
    branch_id: Annotated[uuid.UUID, Depends(EnforceBranchAccess())],
    include_inactive: bool = Query(
        default=False,
        description="When true, includes inactive stations in the returned list.",
    ),
) -> list[KitchenStationResponse]:
    """List kitchen stations belonging to the branch."""
    return await KitchenStationService.list_stations(
        db=db,
        branch_id=branch_id,
        tenant_id=context.tenant_id,
        include_inactive=include_inactive,
    )


@router.patch(
    "/{station_id}",
    response_model=KitchenStationResponse,
    status_code=status.HTTP_200_OK,
    summary="Update Kitchen Station",
    description="Update a kitchen station's localized name or active state. Deactivation is guarded against active catalog references.",
)
async def update_kitchen_station(
    station_id: uuid.UUID,
    payload: UpdateKitchenStationRequest,
    db: Annotated[AsyncSession, Depends(get_async_db)],
    context: Annotated[
        SecurityContext,
        Depends(RequireRoles([UserRole.BRANCH_ADMIN, UserRole.SUPER_ADMIN])),
    ],
    branch_id: Annotated[uuid.UUID, Depends(EnforceBranchAccess())],
) -> KitchenStationResponse:
    """Update kitchen station attributes."""
    return await KitchenStationService.update_station(
        db=db,
        station_id=station_id,
        branch_id=branch_id,
        payload=payload,
    )


@router.delete(
    "/{station_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete or Deactivate Kitchen Station",
    description="Deactivate (default) or permanently delete a station. Strictly blocked with 409 Conflict if actively assigned to menu items or categories.",
)
async def delete_kitchen_station(
    station_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_async_db)],
    context: Annotated[
        SecurityContext,
        Depends(RequireRoles([UserRole.BRANCH_ADMIN, UserRole.SUPER_ADMIN])),
    ],
    branch_id: Annotated[uuid.UUID, Depends(EnforceBranchAccess())],
    soft: bool = Query(
        default=True,
        description="When true, deactivates the station. When false, permanently removes it.",
    ),
) -> None:
    """Deactivate or remove a kitchen station."""
    await KitchenStationService.delete_station(
        db=db,
        station_id=station_id,
        branch_id=branch_id,
        soft=soft,
    )
