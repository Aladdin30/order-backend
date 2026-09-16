"""Staff Menu Service: Authoritative CRUD operations and Item 86 toggle engine."""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.catalog import Category, Item, ModifierGroup, ModifierOption
from app.models.enums import OrderStatus
from app.models.kitchen_station import KitchenStation
from app.models.order import Order, OrderItem
from app.schemas.staff_menu import (
    StaffCategoryCreate,
    StaffCategoryResponse,
    StaffCategoryUpdate,
    StaffItemCreate,
    StaffItemResponse,
    StaffItemUpdate,
    StaffModifierGroupCreate,
    StaffModifierGroupResponse,
    StaffModifierGroupUpdate,
    StaffModifierOptionCreate,
    StaffModifierOptionResponse,
    StaffModifierOptionUpdate,
)
from app.services.audit_service import AuditLogger
from app.services.catalog_broadcast_service import (
    broadcast_item_availability_change,
    broadcast_modifier_availability_change,
)

OPEN_ORDER_STATUSES = [
    OrderStatus.SUBMITTED,
    OrderStatus.PREPARING,
    OrderStatus.READY,
    OrderStatus.DELIVERED,
    OrderStatus.PENDING_STAFF_CONFIRMATION,
]


class StaffMenuService:
    """Service governing administrative menu mutations, branch scoping, and 86 toggles."""

    # -------------------------------------------------------------------------
    # Category Management
    # -------------------------------------------------------------------------

    @classmethod
    async def create_category(
        cls,
        db: AsyncSession,
        branch_id: uuid.UUID,
        tenant_id: uuid.UUID,
        actor_id: uuid.UUID,
        actor_role: str,
        payload: StaffCategoryCreate,
    ) -> StaffCategoryResponse:
        """Create a new menu category scoped to the authorized branch."""
        if payload.station_id is not None:
            st_stmt = select(KitchenStation).where(
                KitchenStation.id == payload.station_id,
                KitchenStation.branch_id == branch_id,
            )
            if (await db.execute(st_stmt)).scalar_one_or_none() is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Kitchen station '{payload.station_id}' not found within authorized branch.",
                )

        now = datetime.datetime.now(datetime.timezone.utc)
        category = Category(
            id=uuid.uuid4(),
            branch_id=branch_id,
            name=payload.name,
            display_order=payload.display_order,
            station=payload.station,
            station_id=payload.station_id,
            is_active=payload.is_active,
            created_at=now,
            updated_at=now,
        )
        db.add(category)
        await db.commit()
        await db.refresh(category)

        await AuditLogger.log(
            tenant_id=tenant_id,
            branch_id=branch_id,
            user_id=actor_id,
            actor_role=actor_role,
            action="CATEGORY_CREATED",
            resource_type="category",
            resource_id=str(category.id),
            changes={"name": category.name, "station": category.station.value},
        )

        return StaffCategoryResponse.model_validate(category)

    @classmethod
    async def update_category(
        cls,
        db: AsyncSession,
        branch_id: uuid.UUID,
        tenant_id: uuid.UUID,
        actor_id: uuid.UUID,
        actor_role: str,
        category_id: uuid.UUID,
        payload: StaffCategoryUpdate,
    ) -> StaffCategoryResponse:
        """Update an existing category scoped to the authorized branch."""
        stmt = select(Category).where(
            Category.id == category_id,
            Category.branch_id == branch_id,
        )
        category = (await db.execute(stmt)).scalar_one_or_none()
        if not category:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Category '{category_id}' not found within authorized branch.",
            )

        changes: dict[str, Any] = {}
        if payload.name is not None:
            category.name = payload.name
            changes["name"] = payload.name
        if payload.display_order is not None:
            category.display_order = payload.display_order
            changes["display_order"] = payload.display_order
        if payload.station is not None:
            category.station = payload.station
            changes["station"] = payload.station.value
        if payload.station_id is not None:
            st_stmt = select(KitchenStation).where(
                KitchenStation.id == payload.station_id,
                KitchenStation.branch_id == branch_id,
            )
            if (await db.execute(st_stmt)).scalar_one_or_none() is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Kitchen station '{payload.station_id}' not found within authorized branch.",
                )
            category.station_id = payload.station_id
            changes["station_id"] = str(payload.station_id)
        if payload.is_active is not None:
            category.is_active = payload.is_active
            changes["is_active"] = payload.is_active

        category.updated_at = datetime.datetime.now(datetime.timezone.utc)
        await db.commit()
        await db.refresh(category)

        await AuditLogger.log(
            tenant_id=tenant_id,
            branch_id=branch_id,
            user_id=actor_id,
            actor_role=actor_role,
            action="CATEGORY_UPDATED",
            resource_type="category",
            resource_id=str(category.id),
            changes=changes,
        )

        return StaffCategoryResponse.model_validate(category)

    @classmethod
    async def delete_category(
        cls,
        db: AsyncSession,
        branch_id: uuid.UUID,
        tenant_id: uuid.UUID,
        actor_id: uuid.UUID,
        actor_role: str,
        category_id: uuid.UUID,
        soft: bool = True,
    ) -> dict[str, Any]:
        """Soft-deactivate or delete a category."""
        stmt = select(Category).where(
            Category.id == category_id,
            Category.branch_id == branch_id,
        )
        category = (await db.execute(stmt)).scalar_one_or_none()
        if not category:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Category '{category_id}' not found within authorized branch.",
            )

        if soft:
            category.is_active = False
            category.updated_at = datetime.datetime.now(datetime.timezone.utc)
            await db.commit()
            action = "CATEGORY_DEACTIVATED"
            message = "Category deactivated successfully."
        else:
            # Validate no active orders reference items in this category
            stmt_orders = (
                select(OrderItem.id)
                .join(Order, OrderItem.order_id == Order.id)
                .join(Item, OrderItem.item_id == Item.id)
                .where(
                    Item.category_id == category_id,
                    Order.status.in_(OPEN_ORDER_STATUSES),
                )
                .limit(1)
            )
            has_active_orders = bool((await db.execute(stmt_orders)).first())
            if has_active_orders:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="CATEGORY_HAS_ACTIVE_ORDERS: Cannot permanently delete category while open orders reference its items.",
                )

            await db.delete(category)
            await db.commit()
            action = "CATEGORY_DELETED"
            message = "Category permanently deleted."

        await AuditLogger.log(
            tenant_id=tenant_id,
            branch_id=branch_id,
            user_id=actor_id,
            actor_role=actor_role,
            action=action,
            resource_type="category",
            resource_id=str(category_id),
            changes={"soft": soft},
        )

        return {"status": "success", "message": message, "category_id": str(category_id)}

    # -------------------------------------------------------------------------
    # Item Management & Item 86 Kill-Switch
    # -------------------------------------------------------------------------

    @classmethod
    async def create_item(
        cls,
        db: AsyncSession,
        branch_id: uuid.UUID,
        tenant_id: uuid.UUID,
        actor_id: uuid.UUID,
        actor_role: str,
        payload: StaffItemCreate,
    ) -> StaffItemResponse:
        """Create a new catalog item under a valid branch category."""
        # Ensure category belongs to branch
        stmt_cat = select(Category).where(
            Category.id == payload.category_id,
            Category.branch_id == branch_id,
        )
        category = (await db.execute(stmt_cat)).scalar_one_or_none()
        if not category:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Category '{payload.category_id}' not found within authorized branch.",
            )

        resolved_station_id = payload.station_id if payload.station_id is not None else category.station_id
        if resolved_station_id is not None:
            st_stmt = select(KitchenStation).where(
                KitchenStation.id == resolved_station_id,
                KitchenStation.branch_id == branch_id,
            )
            if (await db.execute(st_stmt)).scalar_one_or_none() is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Kitchen station '{resolved_station_id}' not found within authorized branch.",
                )

        now = datetime.datetime.now(datetime.timezone.utc)
        item = Item(
            id=uuid.uuid4(),
            category_id=category.id,
            name=payload.name,
            description=payload.description,
            base_price=payload.base_price,
            station=payload.station or category.station,
            station_id=resolved_station_id,
            image_url=payload.image_url,
            is_available=payload.is_available,
            allergens=payload.allergens,
            dietary_badges=payload.dietary_badges,
            created_at=now,
            updated_at=now,
        )
        db.add(item)
        await db.commit()
        await db.refresh(item)

        await AuditLogger.log(
            tenant_id=tenant_id,
            branch_id=branch_id,
            user_id=actor_id,
            actor_role=actor_role,
            action="ITEM_CREATED",
            resource_type="item",
            resource_id=str(item.id),
            changes={"name": item.name, "base_price": str(item.base_price)},
        )

        return StaffItemResponse.model_validate(item)

    @classmethod
    async def update_item(
        cls,
        db: AsyncSession,
        branch_id: uuid.UUID,
        tenant_id: uuid.UUID,
        actor_id: uuid.UUID,
        actor_role: str,
        item_id: uuid.UUID,
        payload: StaffItemUpdate,
    ) -> StaffItemResponse:
        """Update an existing item scoped to the authorized branch."""
        stmt = (
            select(Item)
            .join(Category, Item.category_id == Category.id)
            .where(Item.id == item_id, Category.branch_id == branch_id)
        )
        item = (await db.execute(stmt)).scalar_one_or_none()
        if not item:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Item '{item_id}' not found within authorized branch.",
            )

        changes: dict[str, Any] = {}
        if payload.category_id is not None and payload.category_id != item.category_id:
            # Confirm new category belongs to branch
            c_stmt = select(Category).where(
                Category.id == payload.category_id,
                Category.branch_id == branch_id,
            )
            new_cat = (await db.execute(c_stmt)).scalar_one_or_none()
            if not new_cat:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Target category '{payload.category_id}' not found in authorized branch.",
                )
            item.category_id = payload.category_id
            changes["category_id"] = str(payload.category_id)

        if payload.name is not None:
            item.name = payload.name
            changes["name"] = payload.name
        if payload.description is not None:
            item.description = payload.description
            changes["description"] = payload.description
        if payload.base_price is not None:
            item.base_price = payload.base_price
            changes["base_price"] = str(payload.base_price)
        if payload.station is not None:
            item.station = payload.station
            changes["station"] = payload.station.value
        if payload.station_id is not None:
            st_stmt = select(KitchenStation).where(
                KitchenStation.id == payload.station_id,
                KitchenStation.branch_id == branch_id,
            )
            if (await db.execute(st_stmt)).scalar_one_or_none() is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Kitchen station '{payload.station_id}' not found within authorized branch.",
                )
            item.station_id = payload.station_id
            changes["station_id"] = str(payload.station_id)
        if payload.image_url is not None:
            item.image_url = payload.image_url
            changes["image_url"] = payload.image_url
        if payload.is_available is not None:
            item.is_available = payload.is_available
            changes["is_available"] = payload.is_available
        if payload.allergens is not None:
            item.allergens = payload.allergens
            changes["allergens"] = payload.allergens
        if payload.dietary_badges is not None:
            item.dietary_badges = payload.dietary_badges
            changes["dietary_badges"] = payload.dietary_badges

        item.updated_at = datetime.datetime.now(datetime.timezone.utc)
        await db.commit()
        await db.refresh(item)

        if payload.is_available is not None:
            await broadcast_item_availability_change(
                branch_id=branch_id,
                item_id=item.id,
                is_available=item.is_available,
            )

        await AuditLogger.log(
            tenant_id=tenant_id,
            branch_id=branch_id,
            user_id=actor_id,
            actor_role=actor_role,
            action="ITEM_UPDATED",
            resource_type="item",
            resource_id=str(item.id),
            changes=changes,
        )

        return StaffItemResponse.model_validate(item)

    @classmethod
    async def set_item_availability(
        cls,
        db: AsyncSession,
        branch_id: uuid.UUID,
        tenant_id: uuid.UUID,
        actor_id: uuid.UUID,
        actor_role: str,
        item_id: uuid.UUID,
        is_available: bool,
    ) -> StaffItemResponse:
        """Instant Item 86 toggle (out-of-stock switch)."""
        stmt = (
            select(Item)
            .join(Category, Item.category_id == Category.id)
            .where(Item.id == item_id, Category.branch_id == branch_id)
        )
        item = (await db.execute(stmt)).scalar_one_or_none()
        if not item:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Item '{item_id}' not found within authorized branch.",
            )

        item.is_available = is_available
        item.updated_at = datetime.datetime.now(datetime.timezone.utc)
        await db.commit()
        await db.refresh(item)

        await broadcast_item_availability_change(
            branch_id=branch_id,
            item_id=item.id,
            is_available=is_available,
        )

        action = "ITEM_RESTOCKED" if is_available else "ITEM_86_TRIGGERED"
        await AuditLogger.log(
            tenant_id=tenant_id,
            branch_id=branch_id,
            user_id=actor_id,
            actor_role=actor_role,
            action=action,
            resource_type="item",
            resource_id=str(item.id),
            changes={"is_available": is_available},
        )

        return StaffItemResponse.model_validate(item)

    @classmethod
    async def delete_item(
        cls,
        db: AsyncSession,
        branch_id: uuid.UUID,
        tenant_id: uuid.UUID,
        actor_id: uuid.UUID,
        actor_role: str,
        item_id: uuid.UUID,
        soft: bool = True,
    ) -> dict[str, Any]:
        """Soft-deactivate (86) or delete an item."""
        stmt = (
            select(Item)
            .join(Category, Item.category_id == Category.id)
            .where(Item.id == item_id, Category.branch_id == branch_id)
        )
        item = (await db.execute(stmt)).scalar_one_or_none()
        if not item:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Item '{item_id}' not found within authorized branch.",
            )

        if soft:
            item.is_available = False
            item.updated_at = datetime.datetime.now(datetime.timezone.utc)
            await db.commit()
            await broadcast_item_availability_change(
                branch_id=branch_id,
                item_id=item.id,
                is_available=False,
            )
            action = "ITEM_DEACTIVATED"
            message = "Item marked as unavailable (86'd)."
        else:
            # Check open orders
            stmt_orders = (
                select(OrderItem.id)
                .join(Order, OrderItem.order_id == Order.id)
                .where(
                    OrderItem.item_id == item_id,
                    Order.status.in_(OPEN_ORDER_STATUSES),
                )
                .limit(1)
            )
            has_active_orders = bool((await db.execute(stmt_orders)).first())
            if has_active_orders:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="ITEM_HAS_ACTIVE_ORDERS: Cannot delete item while active orders reference it.",
                )

            await db.delete(item)
            await db.commit()
            action = "ITEM_DELETED"
            message = "Item permanently deleted."

        await AuditLogger.log(
            tenant_id=tenant_id,
            branch_id=branch_id,
            user_id=actor_id,
            actor_role=actor_role,
            action=action,
            resource_type="item",
            resource_id=str(item_id),
            changes={"soft": soft},
        )

        return {"status": "success", "message": message, "item_id": str(item_id)}

    # -------------------------------------------------------------------------
    # Modifier Groups & Options Management
    # -------------------------------------------------------------------------

    @classmethod
    async def create_modifier_group(
        cls,
        db: AsyncSession,
        branch_id: uuid.UUID,
        tenant_id: uuid.UUID,
        actor_id: uuid.UUID,
        actor_role: str,
        item_id: uuid.UUID,
        payload: StaffModifierGroupCreate,
    ) -> StaffModifierGroupResponse:
        """Create a new modifier group for an item scoped to the branch."""
        stmt = (
            select(Item)
            .join(Category, Item.category_id == Category.id)
            .where(Item.id == item_id, Category.branch_id == branch_id)
        )
        item = (await db.execute(stmt)).scalar_one_or_none()
        if not item:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Item '{item_id}' not found within authorized branch.",
            )

        if payload.min_choices > payload.max_choices:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="min_choices cannot exceed max_choices.",
            )

        now = datetime.datetime.now(datetime.timezone.utc)
        group = ModifierGroup(
            id=uuid.uuid4(),
            item_id=item.id,
            name=payload.name,
            min_choices=payload.min_choices,
            max_choices=payload.max_choices,
            is_required=payload.is_required,
            created_at=now,
            updated_at=now,
        )
        db.add(group)
        await db.commit()
        await db.refresh(group)

        await AuditLogger.log(
            tenant_id=tenant_id,
            branch_id=branch_id,
            user_id=actor_id,
            actor_role=actor_role,
            action="MODIFIER_GROUP_CREATED",
            resource_type="modifier_group",
            resource_id=str(group.id),
            changes={"item_id": str(item.id), "name": group.name},
        )

        return StaffModifierGroupResponse.model_validate(group)

    @classmethod
    async def create_modifier_option(
        cls,
        db: AsyncSession,
        branch_id: uuid.UUID,
        tenant_id: uuid.UUID,
        actor_id: uuid.UUID,
        actor_role: str,
        group_id: uuid.UUID,
        payload: StaffModifierOptionCreate,
    ) -> StaffModifierOptionResponse:
        """Add an option to a modifier group within the authorized branch."""
        stmt = (
            select(ModifierGroup)
            .join(Item, ModifierGroup.item_id == Item.id)
            .join(Category, Item.category_id == Category.id)
            .where(ModifierGroup.id == group_id, Category.branch_id == branch_id)
        )
        group = (await db.execute(stmt)).scalar_one_or_none()
        if not group:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Modifier group '{group_id}' not found within authorized branch.",
            )

        now = datetime.datetime.now(datetime.timezone.utc)
        option = ModifierOption(
            id=uuid.uuid4(),
            modifier_group_id=group.id,
            name=payload.name,
            price_delta=payload.price_delta,
            is_available=payload.is_available,
            created_at=now,
            updated_at=now,
        )
        db.add(option)
        await db.commit()
        await db.refresh(option)

        await AuditLogger.log(
            tenant_id=tenant_id,
            branch_id=branch_id,
            user_id=actor_id,
            actor_role=actor_role,
            action="MODIFIER_OPTION_CREATED",
            resource_type="modifier_option",
            resource_id=str(option.id),
            changes={"group_id": str(group.id), "name": option.name, "price_delta": str(option.price_delta)},
        )

        return StaffModifierOptionResponse.model_validate(option)

    @classmethod
    async def update_modifier_option(
        cls,
        db: AsyncSession,
        branch_id: uuid.UUID,
        tenant_id: uuid.UUID,
        actor_id: uuid.UUID,
        actor_role: str,
        option_id: uuid.UUID,
        payload: StaffModifierOptionUpdate,
    ) -> StaffModifierOptionResponse:
        """Update modifier option attributes (name, price_delta, is_available)."""
        stmt = (
            select(ModifierOption)
            .join(ModifierGroup, ModifierOption.modifier_group_id == ModifierGroup.id)
            .join(Item, ModifierGroup.item_id == Item.id)
            .join(Category, Item.category_id == Category.id)
            .where(ModifierOption.id == option_id, Category.branch_id == branch_id)
        )
        option = (await db.execute(stmt)).scalar_one_or_none()
        if not option:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Modifier option '{option_id}' not found within authorized branch.",
            )

        changes: dict[str, Any] = {}
        if payload.name is not None:
            option.name = payload.name
            changes["name"] = payload.name
        if payload.price_delta is not None:
            option.price_delta = payload.price_delta
            changes["price_delta"] = str(payload.price_delta)
        if payload.is_available is not None:
            option.is_available = payload.is_available
            changes["is_available"] = payload.is_available

        option.updated_at = datetime.datetime.now(datetime.timezone.utc)
        await db.commit()
        await db.refresh(option)

        if payload.is_available is not None:
            await broadcast_modifier_availability_change(
                branch_id=branch_id,
                modifier_option_id=option.id,
                group_id=option.modifier_group_id,
                is_available=option.is_available,
            )

        await AuditLogger.log(
            tenant_id=tenant_id,
            branch_id=branch_id,
            user_id=actor_id,
            actor_role=actor_role,
            action="MODIFIER_OPTION_UPDATED",
            resource_type="modifier_option",
            resource_id=str(option.id),
            changes=changes,
        )

        return StaffModifierOptionResponse.model_validate(option)

    @classmethod
    async def set_option_availability(
        cls,
        db: AsyncSession,
        branch_id: uuid.UUID,
        tenant_id: uuid.UUID,
        actor_id: uuid.UUID,
        actor_role: str,
        option_id: uuid.UUID,
        is_available: bool,
    ) -> StaffModifierOptionResponse:
        """Toggle availability for a modifier option within the authorized branch."""
        stmt = (
            select(ModifierOption)
            .join(ModifierGroup, ModifierOption.modifier_group_id == ModifierGroup.id)
            .join(Item, ModifierGroup.item_id == Item.id)
            .join(Category, Item.category_id == Category.id)
            .where(ModifierOption.id == option_id, Category.branch_id == branch_id)
        )
        option = (await db.execute(stmt)).scalar_one_or_none()
        if not option:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Modifier option '{option_id}' not found within authorized branch.",
            )

        option.is_available = is_available
        option.updated_at = datetime.datetime.now(datetime.timezone.utc)
        await db.commit()
        await db.refresh(option)

        await broadcast_modifier_availability_change(
            branch_id=branch_id,
            modifier_option_id=option.id,
            group_id=option.modifier_group_id,
            is_available=is_available,
        )

        action = "MODIFIER_OPTION_RESTOCKED" if is_available else "MODIFIER_OPTION_86_TRIGGERED"
        await AuditLogger.log(
            tenant_id=tenant_id,
            branch_id=branch_id,
            user_id=actor_id,
            actor_role=actor_role,
            action=action,
            resource_type="modifier_option",
            resource_id=str(option.id),
            changes={"is_available": is_available},
        )

        return StaffModifierOptionResponse.model_validate(option)

    @classmethod
    async def delete_modifier_group(
        cls,
        db: AsyncSession,
        branch_id: uuid.UUID,
        tenant_id: uuid.UUID,
        actor_id: uuid.UUID,
        actor_role: str,
        group_id: uuid.UUID,
    ) -> dict[str, Any]:
        """Permanently remove a modifier group and its options."""
        stmt = (
            select(ModifierGroup)
            .join(Item, ModifierGroup.item_id == Item.id)
            .join(Category, Item.category_id == Category.id)
            .where(ModifierGroup.id == group_id, Category.branch_id == branch_id)
        )
        group = (await db.execute(stmt)).scalar_one_or_none()
        if not group:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Modifier group '{group_id}' not found within authorized branch.",
            )

        await db.delete(group)
        await db.commit()

        await AuditLogger.log(
            tenant_id=tenant_id,
            branch_id=branch_id,
            user_id=actor_id,
            actor_role=actor_role,
            action="MODIFIER_GROUP_DELETED",
            resource_type="modifier_group",
            resource_id=str(group_id),
        )

        return {"status": "success", "message": "Modifier group deleted successfully.", "group_id": str(group_id)}

    @classmethod
    async def delete_modifier_option(
        cls,
        db: AsyncSession,
        branch_id: uuid.UUID,
        tenant_id: uuid.UUID,
        actor_id: uuid.UUID,
        actor_role: str,
        option_id: uuid.UUID,
    ) -> dict[str, Any]:
        """Permanently remove an individual modifier option."""
        stmt = (
            select(ModifierOption)
            .join(ModifierGroup, ModifierOption.modifier_group_id == ModifierGroup.id)
            .join(Item, ModifierGroup.item_id == Item.id)
            .join(Category, Item.category_id == Category.id)
            .where(ModifierOption.id == option_id, Category.branch_id == branch_id)
        )
        option = (await db.execute(stmt)).scalar_one_or_none()
        if not option:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Modifier option '{option_id}' not found within authorized branch.",
            )

        await db.delete(option)
        await db.commit()

        await AuditLogger.log(
            tenant_id=tenant_id,
            branch_id=branch_id,
            user_id=actor_id,
            actor_role=actor_role,
            action="MODIFIER_OPTION_DELETED",
            resource_type="modifier_option",
            resource_id=str(option_id),
        )

        return {"status": "success", "message": "Modifier option deleted successfully.", "option_id": str(option_id)}
