"""Menu service: Async hierarchical catalog retrieval and authoritative modifier validation."""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.auth import Branch
from app.models.catalog import Category, Item, ModifierGroup, ModifierOption
from app.models.enums import KitchenStation, MenuItemScope
from app.models.menu import BranchMenuOverride
from app.schemas.i18n import resolve_localized_string
from app.schemas.menu import (
    BranchMenuCategoryGroup,
    BranchMenuItemResponse,
    BranchMenuOverrideUpdate,
    BranchMenuResponse,
    MenuCategoryResponse,
    MenuTreeResponse,
    ScopedItemCreateRequest,
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

    @classmethod
    async def get_branch_menu(
        cls,
        db: AsyncSession,
        branch_id: uuid.UUID,
    ) -> BranchMenuResponse:
        """Fetch the effective scoped catalog menu for a branch in a single pass (Zero N+1).

        Resolves catalog items scoped to the brand and branch, applying BranchMenuOverride
        for dynamic price_override and is_available switches.
        """
        # 1. Fetch branch to retrieve brand_id and currency
        branch_stmt = select(Branch).where(Branch.id == branch_id)
        branch_res = await db.execute(branch_stmt)
        branch = branch_res.scalar_one_or_none()
        if not branch:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="BRANCH_NOT_FOUND",
            )

        # 2. Single-pass query: Item joined with Category and BranchMenuOverride
        # LEFT OUTER JOIN on BranchMenuOverride.menu_item_id == Item.id AND BranchMenuOverride.branch_id == branch_id
        final_price_col = func.coalesce(BranchMenuOverride.price_override, Item.base_price).label("final_price")
        is_avail_col = func.coalesce(BranchMenuOverride.is_available, Item.is_available).label("effective_is_available")
        is_vis_col = func.coalesce(BranchMenuOverride.is_visible, True).label("effective_is_visible")

        stmt = (
            select(
                Item,
                Category,
                BranchMenuOverride,
                final_price_col,
                is_avail_col,
                is_vis_col,
            )
            .join(Category, Item.category_id == Category.id)
            .outerjoin(
                BranchMenuOverride,
                and_(
                    BranchMenuOverride.menu_item_id == Item.id,
                    BranchMenuOverride.branch_id == branch_id,
                ),
            )
            .where(
                Item.is_active.is_(True),
                Category.is_active.is_(True),
                or_(
                    Item.scope == MenuItemScope.ALL_BRANCHES,
                    BranchMenuOverride.branch_id.is_not(None),
                ),
                func.coalesce(BranchMenuOverride.is_visible, True).is_(True),
            )
        )

        if branch.brand_id is not None:
            stmt = stmt.where(
                or_(
                    Item.brand_id == branch.brand_id,
                    Category.branch_id == branch_id,
                )
            )
        else:
            stmt = stmt.where(Category.branch_id == branch_id)

        stmt = stmt.order_by(
            Category.display_order.asc(),
            Category.created_at.asc(),
            Item.created_at.asc(),
        )

        rows = (await db.execute(stmt)).all()

        # 3. Group items by Category in memory
        categories_map: dict[uuid.UUID, BranchMenuCategoryGroup] = {}
        for item, cat, override, final_price, effective_available, effective_visible in rows:
            if cat.id not in categories_map:
                categories_map[cat.id] = BranchMenuCategoryGroup(
                    category_id=cat.id,
                    category_name=cat.name,
                    display_order=cat.display_order,
                    items=[],
                )

            categories_map[cat.id].items.append(
                BranchMenuItemResponse(
                    id=item.id,
                    category_id=cat.id,
                    category_name=cat.name,
                    name=item.name,
                    description=item.description,
                    base_price=item.base_price,
                    final_price=final_price,
                    scope=item.scope,
                    is_available=bool(effective_available),
                    is_visible=bool(effective_visible),
                    has_override=override is not None,
                    price_override=override.price_override if override else None,
                    image_url=item.image_url,
                    allergens=item.allergens or [],
                    dietary_badges=item.dietary_badges or [],
                )
            )

        category_list = list(categories_map.values())

        return BranchMenuResponse(
            branch_id=branch_id,
            brand_id=branch.brand_id,
            currency=getattr(branch, "currency", "EGP") or "EGP",
            categories=category_list,
        )

    @classmethod
    async def create_catalog_item(
        cls,
        db: AsyncSession,
        payload: ScopedItemCreateRequest,
    ) -> Item:
        """Create a new catalog item with ALL_BRANCHES or SPECIFIC_BRANCHES scope.

        If SPECIFIC_BRANCHES is chosen, bulk creates BranchMenuOverride entries for each target branch.
        """
        item = Item(
            name=payload.name,
            description=payload.description,
            base_price=payload.base_price,
            category_id=payload.category_id,
            brand_id=payload.brand_id,
            scope=payload.scope,
            station_id=payload.station_id,
            image_url=payload.image_url,
            is_active=True,
            is_available=True,
        )
        db.add(item)
        await db.flush()

        if payload.scope == MenuItemScope.SPECIFIC_BRANCHES and payload.target_branch_ids:
            for b_id in payload.target_branch_ids:
                override = BranchMenuOverride(
                    branch_id=b_id,
                    menu_item_id=item.id,
                    price_override=None,
                    is_available=True,
                    is_visible=True,
                )
                db.add(override)
            await db.flush()

        await db.commit()
        await db.refresh(item)
        return item

    @classmethod
    async def set_branch_override(
        cls,
        db: AsyncSession,
        branch_id: uuid.UUID,
        menu_item_id: uuid.UUID,
        price_override: Decimal | None = None,
        is_available: bool | None = None,
        is_visible: bool | None = None,
    ) -> BranchMenuOverride:
        """Modify branch-specific availability (86), price override, or visibility."""
        stmt = select(BranchMenuOverride).where(
            BranchMenuOverride.branch_id == branch_id,
            BranchMenuOverride.menu_item_id == menu_item_id,
        )
        result = await db.execute(stmt)
        override = result.scalar_one_or_none()

        if override is None:
            override = BranchMenuOverride(
                branch_id=branch_id,
                menu_item_id=menu_item_id,
                price_override=price_override,
                is_available=is_available if is_available is not None else True,
                is_visible=is_visible if is_visible is not None else True,
            )
            db.add(override)
        else:
            if price_override is not None:
                override.price_override = price_override
            if is_available is not None:
                override.is_available = is_available
            if is_visible is not None:
                override.is_visible = is_visible

        await db.commit()
        await db.refresh(override)
        return override

    @classmethod
    async def bulk_assign_item_branches(
        cls,
        db: AsyncSession,
        menu_item_id: uuid.UUID,
        branch_ids: list[uuid.UUID],
    ) -> list[BranchMenuOverride]:
        """Bulk assign an item to specific branches via BranchMenuOverride."""
        overrides = []
        for b_id in branch_ids:
            stmt = select(BranchMenuOverride).where(
                BranchMenuOverride.branch_id == b_id,
                BranchMenuOverride.menu_item_id == menu_item_id,
            )
            existing = (await db.execute(stmt)).scalar_one_or_none()
            if not existing:
                override = BranchMenuOverride(
                    branch_id=b_id,
                    menu_item_id=menu_item_id,
                    is_visible=True,
                    is_available=True,
                )
                db.add(override)
                overrides.append(override)
            else:
                existing.is_visible = True
                overrides.append(existing)
        await db.commit()
        return overrides

