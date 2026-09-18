"""Order Service: ACID checkout pipeline, concurrency control, station resolution, and FSM lifecycle."""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.state_machine import OrderStateMachine
from app.models.auth import Branch, Table
from app.models.catalog import Category, Item, ModifierGroup, ModifierOption
from app.models.enums import KitchenStation, OrderSource, OrderStatus, OrderType, TableStatus, UserRole
from app.models.order import Order, OrderItem
from app.schemas.i18n import resolve_localized_string
from app.schemas.order import (
    CheckoutRequest,
    OrderItemResponse,
    OrderResponse,
    OrderTransitionResponse,
)
from app.schemas.session import GuestSessionContext
from app.services.audit_service import AuditLogger
from app.services.station_routing_service import StationRoutingService

# Default fallback VAT rate (0%)
STANDARD_TAX_RATE = Decimal("0.0000")

OPEN_ORDER_STATUSES = {
    OrderStatus.DRAFT,
    OrderStatus.PENDING_STAFF_CONFIRMATION,
    OrderStatus.SUBMITTED,
    OrderStatus.PREPARING,
    OrderStatus.READY,
    OrderStatus.DELIVERED,
}


class OrderService:
    """ACID-compliant order processing, row locking, KDS station routing, and FSM transition management."""

    @classmethod
    def calculate_order_financials(
        cls,
        subtotal: Decimal,
        order_type: OrderType,
        tax_rate: Decimal,
        service_fee_rate: Decimal = Decimal("0.0000"),
        is_service_taxable: bool = False,
        service_fee_dine_in_only: bool = True,
    ) -> dict[str, Decimal]:
        """Atomically compute order financial ledger with Decimal ROUND_HALF_UP precision."""
        subtotal = subtotal.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        # Service fee logic: 0 for takeaway if dine-in exclusive
        if order_type == OrderType.TAKEAWAY and service_fee_dine_in_only:
            service_fee = Decimal("0.00")
            effective_service_rate = Decimal("0.0000")
        else:
            effective_service_rate = service_fee_rate
            service_fee = (subtotal * service_fee_rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        # Tax logic: simple vs compound (tax over service)
        if is_service_taxable:
            taxable_base = subtotal + service_fee
        else:
            taxable_base = subtotal

        tax = (taxable_base * tax_rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        total = subtotal + service_fee + tax

        return {
            "subtotal": subtotal,
            "service_fee_rate": effective_service_rate,
            "service_fee_total": service_fee,
            "applied_tax_rate": tax_rate,
            "tax_total": tax,
            "total_amount": total,
        }

    @classmethod
    async def checkout_order(
        cls,
        db: AsyncSession,
        session: GuestSessionContext,
        payload: CheckoutRequest,
    ) -> OrderResponse:
        """Execute atomic order checkout with pessimistic table row locking and server-side validation.

        Pipeline:
        1. Acquire SELECT ... FOR UPDATE lock on the Table record.
        2. Verify table exists, belongs to branch, and is active.
        3. Check for existing active/open order on the table (live shared cart / re-ordering support).
        4. Authoritatively validate each submitted item, modifier group bounds, and availability.
        5. Resolve item station automatically (item.station or item.category.station).
        6. Capture immutable historical pricing and modifier snapshots.
        7. Compute subtotal, tax_total, and total_amount using Decimal with ROUND_HALF_UP.
        8. Advance order and table status based on presence verification status.
        9. Commit and log decoupled audit entry.
        """
        if not payload.items:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="EMPTY_ORDER",
            )

        # 1. Pessimistic row-level lock on Table record
        table_stmt = (
            select(Table)
            .options(selectinload(Table.branch))
            .where(Table.id == session.table_id)
            .with_for_update()
        )
        table_res = await db.execute(table_stmt)
        table = table_res.scalar_one_or_none()

        if table is None or table.branch_id != session.branch_id or not table.is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="TABLE_INACTIVE",
            )

        # Guard: Departed guest session / table availability invalidation
        if table.status == TableStatus.AVAILABLE and table.current_session_token is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="SESSION_TERMINATED_TABLE_AVAILABLE",
            )

        if (
            table.current_session_token is not None
            and table.current_session_token != str(session.session_id)
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="SESSION_TERMINATED_TABLE_AVAILABLE",
            )

        # 2. Check for active/open order on this table
        existing_order_stmt = (
            select(Order)
            .where(
                Order.table_id == table.id,
                Order.branch_id == session.branch_id,
                Order.status.in_(OPEN_ORDER_STATUSES),
            )
            .order_by(Order.created_at.desc())
            .options(
                selectinload(Order.order_items).selectinload(OrderItem.item)
            )
        )
        existing_res = await db.execute(existing_order_stmt)
        order = existing_res.scalars().first()
        is_reorder = order is not None

        if order is None:
            initial_status = (
                OrderStatus.SUBMITTED
                if session.is_presence_verified
                else OrderStatus.PENDING_STAFF_CONFIRMATION
            )
            order = Order(
                tenant_id=session.tenant_id,
                branch_id=session.branch_id,
                table_id=table.id,
                status=initial_status,
                order_type=OrderType.DINE_IN,
                order_source=OrderSource.QR_CUSTOMER,
                subtotal=Decimal("0.00"),
                service_fee_rate=Decimal("0.0000"),
                service_fee_total=Decimal("0.00"),
                applied_tax_rate=Decimal("0.0000"),
                tax_total=Decimal("0.00"),
                total_amount=Decimal("0.00"),
                customer_notes=payload.customer_notes,
            )
            db.add(order)
            await db.flush()  # Obtain order.id
            existing_items: list[OrderItem] = []
        else:
            existing_items = list(order.order_items)
            if payload.customer_notes:
                order.customer_notes = (
                    f"{order.customer_notes}\n{payload.customer_notes}".strip()
                    if order.customer_notes
                    else payload.customer_notes
                )

        # 3. Process and authoritatively validate each line item
        new_items: list[OrderItem] = []
        for item_input in payload.items:
            # Query item with active category and full modifier tree
            item_stmt = (
                select(Item)
                .join(Category, Item.category_id == Category.id)
                .where(
                    Item.id == item_input.item_id,
                    Category.branch_id == session.branch_id,
                    Category.is_active.is_(True),
                )
                .options(
                    selectinload(Item.kitchen_station),
                    selectinload(Item.category).selectinload(Category.kitchen_station),
                    selectinload(Item.modifier_groups).selectinload(ModifierGroup.options),
                )
            )
            item_res = await db.execute(item_stmt)
            catalog_item = item_res.scalar_one_or_none()

            if not catalog_item:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="ITEM_NOT_FOUND",
                )

            if not catalog_item.is_available:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="ITEM_UNAVAILABLE",
                )

            # Validate modifier groups
            total_modifier_delta = Decimal("0.00")
            selected_snapshots: list[dict[str, Any]] = []

            # Resolve option IDs from selected_groups or selected_option_ids
            explicit_option_ids: set[uuid.UUID] = set(item_input.selected_option_ids)
            for grp_sel in getattr(item_input, "selected_groups", []):
                oids = getattr(grp_sel, "option_ids", None) or getattr(grp_sel, "selected_option_ids", [])
                for oid in oids:
                    explicit_option_ids.add(oid)

            for group in catalog_item.modifier_groups:
                group_option_ids = {opt.id for opt in group.options}
                selected_option_ids = explicit_option_ids.intersection(group_option_ids)

                if group.is_required and not selected_option_ids:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="MODIFIER_GROUP_REQUIRED",
                    )

                min_choices = max(group.min_choices, 1 if group.is_required else 0)
                max_choices = group.max_choices

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

                if len(selected_option_ids) > max_choices:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="MODIFIER_SELECTION_OUT_OF_BOUNDS",
                    )

                options_by_id = {opt.id for opt in group.options}
                options_map = {opt.id: opt for opt in group.options}
                for opt_id in selected_option_ids:
                    if opt_id not in options_map:
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail="MODIFIER_OPTION_INVALID",
                        )
                    opt = options_map[opt_id]
                    if not opt.is_available:
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail="MODIFIER_OPTION_UNAVAILABLE",
                        )

                    delta = Decimal(str(opt.price_delta))
                    total_modifier_delta += delta

                    selected_snapshots.append({
                        "group_id": str(group.id),
                        "group_name": resolve_localized_string(group.name),
                        "option_id": str(opt.id),
                        "name": resolve_localized_string(opt.name),
                        "price_delta": float(delta),
                    })

            # Calculate line pricing
            base_price = Decimal(str(catalog_item.base_price))
            unit_price = base_price + total_modifier_delta
            line_subtotal = unit_price * Decimal(item_input.quantity)

            # Category-driven station inheritance: item.station -> item.category.station -> HOT_KITCHEN
            resolved_station = StationRoutingService.resolve_item_station(
                catalog_item,
                catalog_item.category,
            )
            station_code_val = str(resolved_station.value if hasattr(resolved_station, "value") else resolved_station)
            try:
                legacy_enum_station = KitchenStation(station_code_val)
            except ValueError:
                legacy_enum_station = KitchenStation.HOT_KITCHEN

            station_id_val = catalog_item.station_id or (catalog_item.category.station_id if catalog_item.category else None)

            order_item = OrderItem(
                order_id=order.id,
                item_id=catalog_item.id,
                quantity=item_input.quantity,
                unit_price=unit_price,
                subtotal=line_subtotal,
                station=legacy_enum_station,
                station_code=station_code_val,
                station_id=station_id_val,
                is_bumped=False,
                selected_modifiers=selected_snapshots,
                special_instructions=item_input.special_instructions,
            )
            order_item.item = catalog_item
            db.add(order_item)
            new_items.append(order_item)

        await db.flush()
        all_items = existing_items + new_items

        # 4. Recompute financial totals across all items using dynamic branch settings
        order_subtotal = sum(Decimal(str(item.subtotal)) for item in all_items)
        branch = table.branch if table else None
        tax_rate = getattr(branch, "tax_rate", STANDARD_TAX_RATE)
        service_rate = getattr(branch, "service_fee_rate", Decimal("0.0000"))
        is_service_taxable = getattr(branch, "is_service_taxable", False)
        service_fee_dine_in_only = getattr(branch, "service_fee_dine_in_only", True)

        fin = cls.calculate_order_financials(
            subtotal=order_subtotal,
            order_type=order.order_type,
            tax_rate=tax_rate,
            service_fee_rate=service_rate,
            is_service_taxable=is_service_taxable,
            service_fee_dine_in_only=service_fee_dine_in_only,
        )

        order_tax = fin["tax_total"]
        order_total = fin["total_amount"]

        order.subtotal = fin["subtotal"]
        order.service_fee_rate = fin["service_fee_rate"]
        order.service_fee_total = fin["service_fee_total"]
        order.applied_tax_rate = fin["applied_tax_rate"]
        order.tax_total = order_tax
        order.total_amount = order_total

        # 5. Presence-driven status synchronization
        if session.is_presence_verified:
            if order.status in (OrderStatus.DRAFT, OrderStatus.DELIVERED):
                order.status = OrderStatus.SUBMITTED
            table.status = TableStatus.AWAITING_FOOD
        else:
            if not is_reorder:
                order.status = OrderStatus.PENDING_STAFF_CONFIRMATION

        now = datetime.datetime.now(datetime.timezone.utc)
        order_id = order.id
        tenant_id = order.tenant_id
        branch_id = order.branch_id
        table_id = order.table_id
        order_status = order.status
        order_type = order.order_type
        notes = order.customer_notes
        created_at = getattr(order, "created_at", None) or now
        updated_at = getattr(order, "updated_at", None) or now

        await db.commit()

        # 6. Audit Logging
        action_name = "ORDER_ITEMS_APPENDED" if is_reorder else "ORDER_CREATED"
        await AuditLogger.log(
            tenant_id=tenant_id,
            branch_id=branch_id,
            action=action_name,
            resource_type="orders",
            resource_id=str(order_id),
            actor_role="GUEST",
            changes={
                "status": order_status.value,
                "table_id": str(table_id),
                "subtotal": str(order_subtotal),
                "total_amount": str(order_total),
                "is_reorder": is_reorder,
            },
        )

        # 7. Real-Time KDS Station Decomposition Dispatch
        # Directive 2: Route only newly added items on subsequent checkouts
        if order_status in (OrderStatus.SUBMITTED, OrderStatus.PREPARING):
            try:
                await StationRoutingService.dispatch_order_to_kds(
                    order,
                    items=new_items,
                    table_number=table.table_number,
                )
            except Exception as kds_exc:
                import logging
                logging.getLogger("app.services.order_service").warning("Failed to dispatch KDS sub-tickets: %s", kds_exc)

        # 8. Real-Time Floor State Notification Hook
        if table_id:
            try:
                from app.services.floor_table_service import FloorTableService
                await FloorTableService.broadcast_floor_event(
                    branch_id=branch_id,
                    event_type="TABLE_STATUS_CHANGED",
                    table_id=table_id,
                    payload={"state": "AWAITING_FOOD", "order_id": str(order_id)},
                )
            except Exception as floor_exc:
                import logging
                logging.getLogger("app.services.order_service").warning("Failed to broadcast floor state event: %s", floor_exc)

        return cls._build_order_response(
            order,
            items=all_items,
            order_id=order_id,
            tenant_id=tenant_id,
            branch_id=branch_id,
            table_id=table_id,
            status=order_status,
            order_type=order_type,
            subtotal=order_subtotal,
            tax_total=order_tax,
            total_amount=order_total,
            customer_notes=notes,
            created_at=created_at,
            updated_at=updated_at,
        )

    @classmethod
    async def transition_order_status(
        cls,
        db: AsyncSession,
        order_id: uuid.UUID,
        target_status: OrderStatus,
        reason: str | None = None,
        actor_role: UserRole | str | None = None,
        actor_id: uuid.UUID | None = None,
        tenant_id: uuid.UUID | None = None,
        branch_id: uuid.UUID | None = None,
    ) -> OrderTransitionResponse:
        """Execute authoritative FSM order status transition with cancellation guards and audit logging."""
        # Non-locking lookup of order references to identify parent table_id
        order_ref_stmt = (
            select(Order.table_id, Order.branch_id)
            .where(Order.id == order_id)
        )
        order_ref_res = await db.execute(order_ref_stmt)
        order_ref = order_ref_res.first()

        if order_ref is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="ORDER_NOT_FOUND",
            )

        if branch_id is not None and order_ref.branch_id != branch_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="ORDER_NOT_FOUND",
            )

        # 1. Pessimistic Lock on Table first (Parent)
        t_stmt = (
            select(Table)
            .where(Table.id == order_ref.table_id)
            .with_for_update()
        )
        t_res = await db.execute(t_stmt)
        table = t_res.scalar_one_or_none()

        # 2. Pessimistic Lock on Order second (Child)
        stmt = (
            select(Order)
            .where(Order.id == order_id)
            .with_for_update()
        )
        res = await db.execute(stmt)
        order = res.scalar_one()

        from_status = order.status

        # 1. Structural FSM validation
        OrderStateMachine.assert_valid_transition(from_status, target_status)

        # 2. Cancellation guards
        if target_status == OrderStatus.CANCELLED:
            OrderStateMachine.assert_can_cancel(
                current_status=from_status,
                actor_role=actor_role,
                reason=reason,
            )

        # 3. Apply order status mutation
        order.status = target_status

        # 4. Synchronize Table status
        new_table_status: TableStatus | None = None
        if table is not None:
            other_orders_stmt = select(Order).where(
                Order.table_id == table.id,
                Order.id != order.id,
                Order.status.in_(OPEN_ORDER_STATUSES),
            )
            has_other = bool((await db.execute(other_orders_stmt)).first())

            new_table_status = OrderStateMachine.get_table_status_for_order_transition(
                target_status=target_status,
                has_other_active_orders=has_other,
            )
            if new_table_status is not None:
                table.status = new_table_status

        await db.commit()

        # 5. Record audit trail
        effective_tenant_id = tenant_id or order.tenant_id
        effective_branch_id = branch_id or order.branch_id
        await AuditLogger.log(
            tenant_id=effective_tenant_id,
            branch_id=effective_branch_id,
            user_id=actor_id,
            actor_role=str(actor_role) if actor_role else "STAFF",
            action="ORDER_STATUS_CHANGED",
            resource_type="orders",
            resource_id=str(order.id),
            changes={
                "from_status": from_status.value,
                "to_status": target_status.value,
                "reason": reason,
                "table_status": new_table_status.value if new_table_status else None,
            },
        )

        # 6. Real-Time KDS Dispatch when order enters active kitchen lifecycle
        if from_status in (OrderStatus.PENDING_STAFF_CONFIRMATION, OrderStatus.DRAFT) and target_status in (OrderStatus.SUBMITTED, OrderStatus.PREPARING):
            try:
                items_stmt = select(OrderItem).options(selectinload(OrderItem.item)).where(OrderItem.order_id == order.id)
                items_res = await db.execute(items_stmt)
                all_order_items = list(items_res.scalars().all())
                await StationRoutingService.dispatch_order_to_kds(
                    order,
                    items=all_order_items,
                    table_number=table.table_number if table else None,
                )
            except Exception as kds_exc:
                import logging
                logging.getLogger("app.services.order_service").warning("Failed to dispatch KDS tickets on transition: %s", kds_exc)

        # 7. Real-Time Floor State Transition Hook
        if order_ref.table_id:
            try:
                from app.services.floor_table_service import FloorTableService
                if target_status in (OrderStatus.PREPARING, OrderStatus.SUBMITTED):
                    await FloorTableService.broadcast_floor_event(
                        branch_id=effective_branch_id,
                        event_type="TABLE_STATUS_CHANGED",
                        table_id=order_ref.table_id,
                        payload={"state": "AWAITING_FOOD", "order_id": str(order.id)},
                    )
                elif target_status in (OrderStatus.READY, OrderStatus.DELIVERED, OrderStatus.SERVED):
                    await FloorTableService.broadcast_floor_event(
                        branch_id=effective_branch_id,
                        event_type="TABLE_STATUS_CHANGED",
                        table_id=order_ref.table_id,
                        payload={"state": "FOOD_SERVED", "order_id": str(order.id)},
                    )
                elif target_status in (OrderStatus.CLOSED, OrderStatus.CANCELLED, OrderStatus.PAID):
                    if table is not None and not has_other:
                        await FloorTableService.broadcast_floor_event(
                            branch_id=effective_branch_id,
                            event_type="TABLE_CLEARED",
                            table_id=order_ref.table_id,
                            payload={"state": "AVAILABLE"},
                        )
            except Exception as floor_exc:
                import logging
                logging.getLogger("app.services.order_service").warning("Failed to broadcast floor event on transition: %s", floor_exc)

        return OrderTransitionResponse(
            order_id=order.id,
            from_status=from_status,
            to_status=target_status,
            table_status=new_table_status,
            message=f"Order transitioned successfully from {from_status.value} to {target_status.value}.",
        )

    @classmethod
    async def get_active_table_order(
        cls,
        db: AsyncSession,
        branch_id: uuid.UUID,
        table_id: uuid.UUID,
    ) -> OrderResponse | None:
        """Retrieve the live active open order for a dining table."""
        stmt = (
            select(Order)
            .where(
                Order.table_id == table_id,
                Order.branch_id == branch_id,
                Order.status.in_(OPEN_ORDER_STATUSES),
            )
            .order_by(Order.created_at.desc())
            .options(
                selectinload(Order.order_items).selectinload(OrderItem.item)
            )
        )
        res = await db.execute(stmt)
        order = res.scalars().first()

        if order is None:
            return None

        return cls._build_order_response(order)

    @staticmethod
    def _build_order_response(
        order: Order,
        items: list[OrderItem] | None = None,
        order_id: uuid.UUID | None = None,
        tenant_id: uuid.UUID | None = None,
        branch_id: uuid.UUID | None = None,
        table_id: uuid.UUID | None = None,
        status: OrderStatus | None = None,
        order_type: OrderType | None = None,
        subtotal: Decimal | None = None,
        tax_total: Decimal | None = None,
        total_amount: Decimal | None = None,
        customer_notes: str | None = None,
        created_at: datetime.datetime | None = None,
        updated_at: datetime.datetime | None = None,
    ) -> OrderResponse:
        """Construct standard OrderResponse with resolved localized item names."""
        items_response: list[OrderItemResponse] = []
        source_items = items if items is not None else order.order_items
        for oi in source_items:
            item_name = resolve_localized_string(oi.item.name) if (oi.item and getattr(oi.item, "name", None)) else "Menu Item"
            items_response.append(
                OrderItemResponse(
                    id=oi.id,
                    order_id=oi.order_id,
                    item_id=oi.item_id,
                    item_name=item_name,
                    quantity=oi.quantity,
                    unit_price=Decimal(str(oi.unit_price)),
                    subtotal=Decimal(str(oi.subtotal)),
                    station=oi.station,
                    station_code=getattr(oi, "station_code", None),
                    station_id=getattr(oi, "station_id", None),
                    is_bumped=oi.is_bumped,
                    selected_modifiers=oi.selected_modifiers,
                    special_instructions=oi.special_instructions,
                )
            )

        now = datetime.datetime.now(datetime.timezone.utc)
        od = getattr(order, "__dict__", {})
        return OrderResponse(
            id=order_id or od.get("id") or order.id,
            tenant_id=tenant_id or od.get("tenant_id") or order.tenant_id,
            branch_id=branch_id or od.get("branch_id") or order.branch_id,
            table_id=table_id if table_id is not None else od.get("table_id"),
            status=status or od.get("status") or order.status,
            order_type=order_type or od.get("order_type") or order.order_type,
            order_source=od.get("order_source") or getattr(order, "order_source", OrderSource.QR_CUSTOMER),
            pickup_number=od.get("pickup_number") or getattr(order, "pickup_number", None),
            subtotal=subtotal if subtotal is not None else Decimal(str(od.get("subtotal") or order.subtotal)),
            service_fee_rate=od.get("service_fee_rate") or getattr(order, "service_fee_rate", Decimal("0.0000")),
            service_fee_total=od.get("service_fee_total") or getattr(order, "service_fee_total", Decimal("0.00")),
            applied_tax_rate=od.get("applied_tax_rate") or getattr(order, "applied_tax_rate", Decimal("0.0000")),
            tax_total=tax_total if tax_total is not None else Decimal(str(od.get("tax_total") or order.tax_total)),
            total_amount=total_amount if total_amount is not None else Decimal(str(od.get("total_amount") or order.total_amount)),
            is_paid=od.get("is_paid", getattr(order, "is_paid", False)),
            customer_notes=customer_notes if customer_notes is not None else od.get("customer_notes", getattr(order, "customer_notes", None)),
            cancellation_reason=od.get("cancellation_reason", getattr(order, "cancellation_reason", None)),
            items=items_response,
            created_at=created_at or od.get("created_at") or now,
            updated_at=updated_at or od.get("updated_at") or now,
        )
