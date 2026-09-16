"""Staff Menu Management API Endpoints: Administrative Catalog CRUD and Item 86 Toggle."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import EnforceBranchAccess, RequireRoles, get_async_db
from app.core.context import SecurityContext
from app.models.enums import UserRole
from app.schemas.staff_menu import (
    StaffCategoryCreate,
    StaffCategoryResponse,
    StaffCategoryUpdate,
    StaffItemAvailabilityUpdate,
    StaffItemCreate,
    StaffItemResponse,
    StaffItemUpdate,
    StaffModifierGroupCreate,
    StaffModifierGroupResponse,
    StaffModifierOptionAvailabilityUpdate,
    StaffModifierOptionCreate,
    StaffModifierOptionResponse,
    StaffModifierOptionUpdate,
)
from app.services.staff_menu_service import StaffMenuService

router = APIRouter(prefix="/staff/menu", tags=["Staff Menu Management"])


# ---------------------------------------------------------------------------
# Category Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/categories",
    response_model=StaffCategoryResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create Menu Category",
    description="Create a new menu category assigned to a kitchen station and scoped to the authorized branch.",
)
async def create_category(
    payload: StaffCategoryCreate,
    db: Annotated[AsyncSession, Depends(get_async_db)],
    context: Annotated[
        SecurityContext,
        Depends(RequireRoles([UserRole.BRANCH_ADMIN, UserRole.SUPER_ADMIN])),
    ],
    branch_id: Annotated[uuid.UUID, Depends(EnforceBranchAccess())],
) -> StaffCategoryResponse:
    """Create a category under the caller's authorized branch."""
    return await StaffMenuService.create_category(
        db=db,
        branch_id=branch_id,
        tenant_id=context.tenant_id,
        actor_id=context.user.id,
        actor_role=context.role.value,
        payload=payload,
    )


@router.patch(
    "/categories/{category_id}",
    response_model=StaffCategoryResponse,
    summary="Update Menu Category",
    description="Partially update an existing category's name, display order, station, or active state.",
)
async def update_category(
    category_id: uuid.UUID,
    payload: StaffCategoryUpdate,
    db: Annotated[AsyncSession, Depends(get_async_db)],
    context: Annotated[
        SecurityContext,
        Depends(RequireRoles([UserRole.BRANCH_ADMIN, UserRole.SUPER_ADMIN])),
    ],
    branch_id: Annotated[uuid.UUID, Depends(EnforceBranchAccess())],
) -> StaffCategoryResponse:
    """Update category attributes within authorized branch."""
    return await StaffMenuService.update_category(
        db=db,
        branch_id=branch_id,
        tenant_id=context.tenant_id,
        actor_id=context.user.id,
        actor_role=context.role.value,
        category_id=category_id,
        payload=payload,
    )


@router.delete(
    "/categories/{category_id}",
    summary="Delete or Deactivate Menu Category",
    description="Soft-deactivates (default) or permanently deletes a category if no open orders reference its items.",
)
async def delete_category(
    category_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_async_db)],
    context: Annotated[
        SecurityContext,
        Depends(RequireRoles([UserRole.BRANCH_ADMIN, UserRole.SUPER_ADMIN])),
    ],
    branch_id: Annotated[uuid.UUID, Depends(EnforceBranchAccess())],
    soft: bool = Query(True, description="When true, deactivates category. When false, permanently removes it."),
) -> dict[str, Any]:
    """Deactivate or remove a category within authorized branch."""
    return await StaffMenuService.delete_category(
        db=db,
        branch_id=branch_id,
        tenant_id=context.tenant_id,
        actor_id=context.user.id,
        actor_role=context.role.value,
        category_id=category_id,
        soft=soft,
    )


# ---------------------------------------------------------------------------
# Item Endpoints & Item 86 Kill-Switch
# ---------------------------------------------------------------------------


@router.post(
    "/items",
    response_model=StaffItemResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create Menu Item",
    description="Create a new menu item with bilingual names, base pricing, kitchen routing, and allergen tags.",
)
async def create_item(
    payload: StaffItemCreate,
    db: Annotated[AsyncSession, Depends(get_async_db)],
    context: Annotated[
        SecurityContext,
        Depends(RequireRoles([UserRole.BRANCH_ADMIN, UserRole.SUPER_ADMIN])),
    ],
    branch_id: Annotated[uuid.UUID, Depends(EnforceBranchAccess())],
) -> StaffItemResponse:
    """Create a new item under a category in the authorized branch."""
    return await StaffMenuService.create_item(
        db=db,
        branch_id=branch_id,
        tenant_id=context.tenant_id,
        actor_id=context.user.id,
        actor_role=context.role.value,
        payload=payload,
    )


@router.patch(
    "/items/{item_id}",
    response_model=StaffItemResponse,
    summary="Update Menu Item",
    description="Partially update item pricing, bilingual copy, category, station, or allergens.",
)
async def update_item(
    item_id: uuid.UUID,
    payload: StaffItemUpdate,
    db: Annotated[AsyncSession, Depends(get_async_db)],
    context: Annotated[
        SecurityContext,
        Depends(RequireRoles([UserRole.BRANCH_ADMIN, UserRole.SUPER_ADMIN])),
    ],
    branch_id: Annotated[uuid.UUID, Depends(EnforceBranchAccess())],
) -> StaffItemResponse:
    """Update item attributes within authorized branch."""
    return await StaffMenuService.update_item(
        db=db,
        branch_id=branch_id,
        tenant_id=context.tenant_id,
        actor_id=context.user.id,
        actor_role=context.role.value,
        item_id=item_id,
        payload=payload,
    )


@router.patch(
    "/items/{item_id}/availability",
    response_model=StaffItemResponse,
    summary="Toggle Item 86 Availability",
    description="Instant Item 86 kill-switch: toggle item availability when out of stock or restocked.",
)
async def toggle_item_availability(
    item_id: uuid.UUID,
    payload: StaffItemAvailabilityUpdate,
    db: Annotated[AsyncSession, Depends(get_async_db)],
    context: Annotated[
        SecurityContext,
        Depends(RequireRoles([UserRole.BRANCH_ADMIN, UserRole.SUPER_ADMIN])),
    ],
    branch_id: Annotated[uuid.UUID, Depends(EnforceBranchAccess())],
) -> StaffItemResponse:
    """Toggle Item 86 status (is_available = True / False)."""
    return await StaffMenuService.set_item_availability(
        db=db,
        branch_id=branch_id,
        tenant_id=context.tenant_id,
        actor_id=context.user.id,
        actor_role=context.role.value,
        item_id=item_id,
        is_available=payload.is_available,
    )


@router.delete(
    "/items/{item_id}",
    summary="Delete or 86 Menu Item",
    description="Soft-deactivates (marks 86 / unavailable) or permanently deletes an item if not referenced in open orders.",
)
async def delete_item(
    item_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_async_db)],
    context: Annotated[
        SecurityContext,
        Depends(RequireRoles([UserRole.BRANCH_ADMIN, UserRole.SUPER_ADMIN])),
    ],
    branch_id: Annotated[uuid.UUID, Depends(EnforceBranchAccess())],
    soft: bool = Query(False, description="When true, sets is_available=False. When false, permanently removes item."),
) -> dict[str, Any]:
    """Deactivate or permanently remove an item within authorized branch."""
    return await StaffMenuService.delete_item(
        db=db,
        branch_id=branch_id,
        tenant_id=context.tenant_id,
        actor_id=context.user.id,
        actor_role=context.role.value,
        item_id=item_id,
        soft=soft,
    )


# ---------------------------------------------------------------------------
# Modifier Groups & Options Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/items/{item_id}/modifier-groups",
    response_model=StaffModifierGroupResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create Modifier Group for Item",
    description="Attach a customization modifier group (e.g. Size, Temperature, Doneness) with min/max selection boundaries.",
)
async def create_modifier_group(
    item_id: uuid.UUID,
    payload: StaffModifierGroupCreate,
    db: Annotated[AsyncSession, Depends(get_async_db)],
    context: Annotated[
        SecurityContext,
        Depends(RequireRoles([UserRole.BRANCH_ADMIN, UserRole.SUPER_ADMIN])),
    ],
    branch_id: Annotated[uuid.UUID, Depends(EnforceBranchAccess())],
) -> StaffModifierGroupResponse:
    """Create a modifier group for a specific item within authorized branch."""
    return await StaffMenuService.create_modifier_group(
        db=db,
        branch_id=branch_id,
        tenant_id=context.tenant_id,
        actor_id=context.user.id,
        actor_role=context.role.value,
        item_id=item_id,
        payload=payload,
    )


@router.delete(
    "/modifier-groups/{group_id}",
    summary="Delete Modifier Group",
    description="Permanently delete a modifier group and all associated options.",
)
async def delete_modifier_group(
    group_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_async_db)],
    context: Annotated[
        SecurityContext,
        Depends(RequireRoles([UserRole.BRANCH_ADMIN, UserRole.SUPER_ADMIN])),
    ],
    branch_id: Annotated[uuid.UUID, Depends(EnforceBranchAccess())],
) -> dict[str, Any]:
    """Remove modifier group within authorized branch."""
    return await StaffMenuService.delete_modifier_group(
        db=db,
        branch_id=branch_id,
        tenant_id=context.tenant_id,
        actor_id=context.user.id,
        actor_role=context.role.value,
        group_id=group_id,
    )


@router.post(
    "/modifier-groups/{group_id}/options",
    response_model=StaffModifierOptionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add Option to Modifier Group",
    description="Add an individual option choice with price delta adjustment to a modifier group.",
)
async def create_modifier_option(
    group_id: uuid.UUID,
    payload: StaffModifierOptionCreate,
    db: Annotated[AsyncSession, Depends(get_async_db)],
    context: Annotated[
        SecurityContext,
        Depends(RequireRoles([UserRole.BRANCH_ADMIN, UserRole.SUPER_ADMIN])),
    ],
    branch_id: Annotated[uuid.UUID, Depends(EnforceBranchAccess())],
) -> StaffModifierOptionResponse:
    """Add an option choice within authorized branch."""
    return await StaffMenuService.create_modifier_option(
        db=db,
        branch_id=branch_id,
        tenant_id=context.tenant_id,
        actor_id=context.user.id,
        actor_role=context.role.value,
        group_id=group_id,
        payload=payload,
    )


@router.patch(
    "/modifier-options/{option_id}",
    response_model=StaffModifierOptionResponse,
    summary="Update Modifier Option",
    description="Update modifier option attributes (name, price_delta, is_available).",
)
async def update_modifier_option(
    option_id: uuid.UUID,
    payload: StaffModifierOptionUpdate,
    db: Annotated[AsyncSession, Depends(get_async_db)],
    context: Annotated[
        SecurityContext,
        Depends(RequireRoles([UserRole.BRANCH_ADMIN, UserRole.SUPER_ADMIN])),
    ],
    branch_id: Annotated[uuid.UUID, Depends(EnforceBranchAccess())],
) -> StaffModifierOptionResponse:
    """Update modifier option attributes within authorized branch."""
    return await StaffMenuService.update_modifier_option(
        db=db,
        branch_id=branch_id,
        tenant_id=context.tenant_id,
        actor_id=context.user.id,
        actor_role=context.role.value,
        option_id=option_id,
        payload=payload,
    )


@router.patch(
    "/modifier-options/{option_id}/availability",
    response_model=StaffModifierOptionResponse,
    summary="Toggle Modifier Option Availability",
    description="Toggle individual modifier option availability (e.g. if a specific cheese or sauce ran out).",
)
async def toggle_option_availability(
    option_id: uuid.UUID,
    payload: StaffModifierOptionAvailabilityUpdate,
    db: Annotated[AsyncSession, Depends(get_async_db)],
    context: Annotated[
        SecurityContext,
        Depends(RequireRoles([UserRole.BRANCH_ADMIN, UserRole.SUPER_ADMIN])),
    ],
    branch_id: Annotated[uuid.UUID, Depends(EnforceBranchAccess())],
) -> StaffModifierOptionResponse:
    """Toggle modifier option availability (86 switch)."""
    return await StaffMenuService.set_option_availability(
        db=db,
        branch_id=branch_id,
        tenant_id=context.tenant_id,
        actor_id=context.user.id,
        actor_role=context.role.value,
        option_id=option_id,
        is_available=payload.is_available,
    )


@router.delete(
    "/modifier-options/{option_id}",
    summary="Delete Modifier Option",
    description="Permanently remove an individual modifier option.",
)
async def delete_modifier_option(
    option_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_async_db)],
    context: Annotated[
        SecurityContext,
        Depends(RequireRoles([UserRole.BRANCH_ADMIN, UserRole.SUPER_ADMIN])),
    ],
    branch_id: Annotated[uuid.UUID, Depends(EnforceBranchAccess())],
) -> dict[str, Any]:
    """Remove modifier option within authorized branch."""
    return await StaffMenuService.delete_modifier_option(
        db=db,
        branch_id=branch_id,
        tenant_id=context.tenant_id,
        actor_id=context.user.id,
        actor_role=context.role.value,
        option_id=option_id,
    )
