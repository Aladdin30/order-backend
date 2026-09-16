"""Menu service: Async hierarchical catalog retrieval and authoritative modifier validation."""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.catalog import Category, Item, ModifierGroup, ModifierOption
from app.schemas.i18n import resolve_localized_string
from app.schemas.menu import (
    MenuCategoryResponse,
    MenuTreeResponse,
    SelectedModifierOptionSnapshot,
    ValidateItemSelectionRequest,
    ValidatedItemSelectionResponse,
)


class MenuService:
    """High-performance catalog tree retrieval and server-side modifier validation engine."""

    @classmethod
    async def get_menu_tree(
        cls,
        db: AsyncSession,
        branch_id: uuid.UUID,
    ) -> MenuTreeResponse:
        """Fetch the full hierarchical catalog tree for a branch, preventing N+1 queries.

        Categories are filtered by active status and ordered by display_order.asc().
        Items, modifier groups, and options are sorted deterministically in-memory.
        Items and options with `is_available: false` are explicitly preserved for 86 UI rendering.
        """
        # Chained selectinload fetches Category -> Item -> ModifierGroup -> ModifierOption in 4 queries
        stmt = (
            select(Category)
            .where(
                Category.branch_id == branch_id,
                Category.is_active.is_(True),
            )
            .order_by(Category.display_order.asc())
            .options(
                selectinload(Category.items)
                .selectinload(Item.modifier_groups)
                .selectinload(ModifierGroup.options)
            )
        )
        result = await db.execute(stmt)
        categories = list(result.scalars().unique().all())

        # Deterministic secondary sorting within categories and modifier groups
        min_dt = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
        for category in categories:
            category.items.sort(key=lambda item: item.created_at or min_dt)
            for item in category.items:
                item.modifier_groups.sort(key=lambda g: g.created_at or min_dt)
                for group in item.modifier_groups:
                    group.options.sort(key=lambda opt: opt.created_at or min_dt)

        # Dynamic localization applied automatically by LocalizedStr via Pydantic model_validate
        category_responses = [MenuCategoryResponse.model_validate(c) for c in categories]

        return MenuTreeResponse(
            branch_id=branch_id,
            categories=category_responses,
        )

    @classmethod
    async def validate_item_selection(
        cls,
        db: AsyncSession,
        branch_id: uuid.UUID,
        table_id: uuid.UUID,
        payload: ValidateItemSelectionRequest,
    ) -> ValidatedItemSelectionResponse:
        """Authoritatively validate item customization rules, availability, and compute pricing.

        Rules Enforced:
        1. Item existence in the active branch catalog.
        2. Item 86 availability (Item.is_available must be True).
        3. Modifier group belonging (group must belong to item).
        4. No duplicate group submissions in a single request.
        5. No duplicate option selections within a group.
        6. Mandatory groups (is_required or min_choices >= 1).
        7. Min / Max choice boundaries (min_choices <= count <= max_choices).
        8. Option belonging to specified group.
        9. Option 86 availability (Option.is_available must be True).
        10. Authoritative price calculation: Unit Price = base_price + sum(deltas), Subtotal = unit_price * quantity.
        """
        # 1. Fetch item scoped to branch and active category with full modifier tree
        stmt = (
            select(Item)
            .join(Category, Item.category_id == Category.id)
            .where(
                Item.id == payload.item_id,
                Category.branch_id == branch_id,
                Category.is_active.is_(True),
            )
            .options(
                selectinload(Item.category),
                selectinload(Item.modifier_groups).selectinload(ModifierGroup.options),
            )
        )
        result = await db.execute(stmt)
        item = result.scalar_one_or_none()

        if item is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="ITEM_NOT_FOUND",
            )

        # 2. Check Item 86 availability
        if not item.is_available:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="ITEM_UNAVAILABLE",
            )

        # 3. Check for duplicate group submissions in request
        submitted_group_ids = [sel.group_id for sel in payload.selected_groups]
        if len(submitted_group_ids) != len(set(submitted_group_ids)):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="DUPLICATE_MODIFIER_GROUP",
            )

        # Map groups by UUID
        groups_by_id = {g.id: g for g in item.modifier_groups}

        # 4. Ensure all submitted groups actually belong to this item
        for sel in payload.selected_groups:
            if sel.group_id not in groups_by_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="MODIFIER_OPTION_INVALID",
                )

        submitted_map: dict[uuid.UUID, list[uuid.UUID]] = {
            sel.group_id: sel.option_ids for sel in payload.selected_groups
        }

        # 5. Validate every configured modifier group for boundaries and options
        selected_snapshots: list[SelectedModifierOptionSnapshot] = []
        total_modifier_delta = Decimal("0.00")

        # Sort groups deterministically for consistent snapshot ordering
        sorted_groups = sorted(
            item.modifier_groups,
            key=lambda g: g.created_at or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc),
        )

        for group in sorted_groups:
            selected_option_ids = submitted_map.get(group.id, [])

            # Check duplicate options within group
            if len(selected_option_ids) != len(set(selected_option_ids)):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="DUPLICATE_MODIFIER_OPTION",
                )

            # Determine minimum required choices
            min_choices = max(group.min_choices, 1 if group.is_required else 0)
            max_choices = group.max_choices

            # Validate min choices boundary
            if len(selected_option_ids) < min_choices:
                if len(selected_option_ids) == 0:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="MODIFIER_GROUP_REQUIRED",
                    )
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="MODIFIER_SELECTION_OUT_OF_BOUNDS",
                )

            # Validate max choices boundary
            if len(selected_option_ids) > max_choices:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="MODIFIER_SELECTION_OUT_OF_BOUNDS",
                )

            # Validate option belonging and availability
            options_by_id = {opt.id: opt for opt in group.options}
            for opt_id in selected_option_ids:
                if opt_id not in options_by_id:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="MODIFIER_OPTION_INVALID",
                    )
                option = options_by_id[opt_id]
                if not option.is_available:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="MODIFIER_OPTION_UNAVAILABLE",
                    )

                delta = Decimal(str(option.price_delta))
                total_modifier_delta += delta

                selected_snapshots.append(
                    SelectedModifierOptionSnapshot(
                        group_id=group.id,
                        group_name=resolve_localized_string(group.name),
                        option_id=option.id,
                        name=resolve_localized_string(option.name),
                        price_delta=delta,
                    )
                )

        # 6. Authoritative price computation
        base_price = Decimal(str(item.base_price))
        unit_price = base_price + total_modifier_delta
        subtotal = unit_price * Decimal(payload.quantity)

        resolved_station = item.station or (item.category.station if item.category else None) or KitchenStation.HOT_KITCHEN

        # 7. Construct and return immutable response snapshot
        return ValidatedItemSelectionResponse(
            item_id=item.id,
            item_name=resolve_localized_string(item.name),
            quantity=payload.quantity,
            base_price=base_price,
            unit_price=unit_price,
            subtotal=subtotal,
            station=resolved_station,
            selected_modifiers=selected_snapshots,
            table_id=table_id,
            client_session_id=payload.client_session_id,
            guest_label=payload.guest_label,
            special_instructions=payload.special_instructions,
        )
