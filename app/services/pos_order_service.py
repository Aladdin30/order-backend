"""POS Order Service: Counter & Table-side cashier ordering, atomic pickup sequencing, and cancellation/refunds."""

from __future__ import annotations

import datetime
import logging
import uuid
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.context import SecurityContext
from app.core.redis_pubsub import publish
from app.models.auth import Branch, Table
from app.models.catalog import Category, Item, ModifierGroup, ModifierOption
from app.models.enums import (
    KitchenStation,
    OrderSource,
    OrderStatus,
    OrderType,
    PaymentMethod,
    PaymentStatus,
    TableStatus,
    UserRole,
)
from app.models.order import Order, OrderItem, Payment
from app.schemas.i18n import resolve_localized_string
from app.schemas.order import OrderResponse
from app.schemas.pos_order import (
    POSCancelOrderRequest,
    POSCancelOrderResponse,
    POSCheckoutRequest,
)
from app.services.audit_service import AuditLogger
from app.services.order_service import OPEN_ORDER_STATUSES, OrderService
from app.services.pickup_sequence_service import PickupSequenceService
from app.services.station_routing_service import StationRoutingService

logger = logging.getLogger("app.services.pos_order_service")


class POSOrderService:
    """Service governing staff counter POS checkouts, immediate takeaway billing, and cancellation/refunds."""

    @classmethod
    async def checkout_pos_order(
        cls,
        db: AsyncSession,
        context: SecurityContext,
        branch_id: uuid.UUID,
        payload: POSCheckoutRequest,
    ) -> OrderResponse:
        """Atomic POS order placement pipeline for dine-in and takeaway channels."""
        if not payload.items:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="EMPTY_ORDER",
            )

        # 1. Fetch branch settings
        branch_stmt = select(Branch).where(
            Branch.id == branch_id,
            Branch.tenant_id == context.tenant_id,
        )
        branch_res = await db.execute(branch_stmt)
        branch = branch_res.scalar_one_or_none()
        if not branch or not branch.is_active:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="BRANCH_INACTIVE_OR_NOT_FOUND",
            )

        table: Table | None = None
        is_reorder = False
        pickup_number: int | None = None

        # 2. Fulfillment channel routing
        if payload.order_type == OrderType.DINE_IN:
            if not payload.table_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="TABLE_ID_REQUIRED_FOR_DINE_IN",
                )
            # Lock table row
            table_stmt = (
                select(Table)
                .where(Table.id == payload.table_id)
                .with_for_update()
            )
            table_res = await db.execute(table_stmt)
            table = table_res.scalar_one_or_none()

            if table is None or table.branch_id != branch_id or not table.is_active:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="TABLE_INACTIVE_OR_NOT_FOUND",
                )

            # Check for existing open order on this table (Shared cart / Re-order)
            existing_order_stmt = (
                select(Order)
                .where(
                    Order.table_id == table.id,
                    Order.branch_id == branch_id,
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
                order = Order(
                    tenant_id=context.tenant_id,
                    branch_id=branch_id,
                    table_id=table.id,
                    status=OrderStatus.SUBMITTED,
                    order_type=OrderType.DINE_IN,
                    order_source=OrderSource.CASHIER_POS,
                    created_by_user_id=context.user.id if context.user else None,
                    pickup_number=None,
                    subtotal=Decimal("0.00"),
                    service_fee_rate=Decimal("0.0000"),
                    service_fee_total=Decimal("0.00"),
                    applied_tax_rate=Decimal("0.0000"),
                    tax_total=Decimal("0.00"),
                    total_amount=Decimal("0.00"),
                    customer_notes=payload.customer_notes,
                )
                db.add(order)
                await db.flush()
                existing_items: list[OrderItem] = []
            else:
                existing_items = list(order.order_items)
                if payload.customer_notes:
                    order.customer_notes = (
                        f"{order.customer_notes}\n{payload.customer_notes}".strip()
                        if order.customer_notes
                        else payload.customer_notes
                    )

            # Update table operational status
            table.status = TableStatus.AWAITING_FOOD

        else:
            # TAKEAWAY: Immediate payment is required
            if payload.immediate_payment is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="PAYMENT_REQUIRED_FOR_TAKEAWAY",
                )

            # Generate atomic daily sequence number (100 to 1000)
            pickup_number = await PickupSequenceService.get_next_pickup_number(
                branch_id=branch_id,
                db=db,
            )

            order = Order(
                tenant_id=context.tenant_id,
                branch_id=branch_id,
                table_id=None,
                status=OrderStatus.SUBMITTED,
                order_type=OrderType.TAKEAWAY,
                order_source=OrderSource.CASHIER_POS,
                created_by_user_id=context.user.id if context.user else None,
                pickup_number=pickup_number,
                is_paid=True,
                subtotal=Decimal("0.00"),
                service_fee_rate=Decimal("0.0000"),
                service_fee_total=Decimal("0.00"),
                applied_tax_rate=Decimal("0.0000"),
                tax_total=Decimal("0.00"),
                total_amount=Decimal("0.00"),
                customer_notes=payload.customer_notes,
            )
            db.add(order)
            await db.flush()
            existing_items = []

        # 3. Process line items with authoritative validation
        new_items: list[OrderItem] = []
        for item_input in payload.items:
            item_stmt = (
                select(Item)
                .join(Category, Item.category_id == Category.id)
                .where(
                    Item.id == item_input.item_id,
                    Category.branch_id == branch_id,
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

            total_modifier_delta = Decimal("0.00")
            selected_snapshots: list[dict[str, Any]] = []

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
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="MODIFIER_SELECTION_OUT_OF_BOUNDS",
                    )
                if len(selected_option_ids) > max_choices:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="MODIFIER_SELECTION_OUT_OF_BOUNDS",
                    )

                options_map = {opt.id: opt for opt in group.options}
                for opt_id in selected_option_ids:
                    opt = options_map.get(opt_id)
                    if not opt or not opt.is_available:
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

            base_price = Decimal(str(catalog_item.base_price))
            unit_price = base_price + total_modifier_delta
            line_subtotal = unit_price * Decimal(item_input.quantity)

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

        # 4. Financial Calculations using dynamic branch settings
        order_subtotal = sum(Decimal(str(item.subtotal)) for item in all_items)
        fin = OrderService.calculate_order_financials(
            subtotal=order_subtotal,
            order_type=order.order_type,
            tax_rate=branch.tax_rate,
            service_fee_rate=branch.service_fee_rate,
            is_service_taxable=branch.is_service_taxable,
            service_fee_dine_in_only=branch.service_fee_dine_in_only,
        )

        order.subtotal = fin["subtotal"]
        order.service_fee_rate = fin["service_fee_rate"]
        order.service_fee_total = fin["service_fee_total"]
        order.applied_tax_rate = fin["applied_tax_rate"]
        order.tax_total = fin["tax_total"]
        order.total_amount = fin["total_amount"]

        # 5. Handle immediate settlement if provided or mandatory for takeaway
        if payload.immediate_payment is not None:
            order.is_paid = True
            payment = Payment(
                tenant_id=context.tenant_id,
                branch_id=branch_id,
                order_id=order.id,
                payment_method=payload.immediate_payment,
                amount=order.total_amount,
                currency="SAR",
                status=PaymentStatus.COMPLETED,
                transaction_reference=f"POS-{uuid.uuid4().hex[:12].upper()}",
                verified_by_user_id=context.user.id if context.user else None,
                verified_at=datetime.datetime.now(datetime.timezone.utc),
            )
            db.add(payment)
            await db.flush()

        now = datetime.datetime.now(datetime.timezone.utc)
        order_id = order.id
        tenant_id = order.tenant_id
        order_branch_id = order.branch_id
        table_id = order.table_id
        order_status = order.status
        order_type = order.order_type
        order_source = order.order_source
        pickup_num = order.pickup_number
        is_paid_val = order.is_paid
        notes = order.customer_notes
        created_at = getattr(order, "__dict__", {}).get("created_at") or now
        updated_at = getattr(order, "__dict__", {}).get("updated_at") or now
        subtotal_val = order.subtotal
        tax_val = order.tax_total
        total_val = order.total_amount

        await db.commit()

        # 6. Audit Logging
        action_name = "POS_ORDER_ITEMS_APPENDED" if is_reorder else "POS_ORDER_CREATED"
        await AuditLogger.log(
            tenant_id=context.tenant_id,
            branch_id=branch_id,
            action=action_name,
            resource_type="orders",
            resource_id=str(order_id),
            user_id=context.user.id if context.user else None,
            actor_role=context.role.value,
            changes={
                "order_type": order_type.value,
                "order_source": order_source.value,
                "table_id": str(table_id) if table_id else None,
                "pickup_number": pickup_num,
                "subtotal": str(subtotal_val),
                "total_amount": str(total_val),
                "is_paid": is_paid_val,
            },
            status="SUCCESS",
        )

        # 7. KDS Station Routing & WebSocket Broadcast
        try:
            resolved_table_number = table.table_number if table else None
            await StationRoutingService.dispatch_order_to_kds(
                order,
                items=new_items,
                table_number=resolved_table_number,
            )
        except Exception as kds_exc:
            logger.warning("Failed to dispatch POS order to KDS: %s", kds_exc)

        return OrderService._build_order_response(
            order,
            items=all_items,
            order_id=order_id,
            tenant_id=tenant_id,
            branch_id=order_branch_id,
            table_id=table_id,
            status=order_status,
            order_type=order_type,
            subtotal=subtotal_val,
            tax_total=tax_val,
            total_amount=total_val,
            customer_notes=notes,
            created_at=created_at,
            updated_at=updated_at,
        )

    @classmethod
    async def cancel_pos_order(
        cls,
        db: AsyncSession,
        context: SecurityContext,
        branch_id: uuid.UUID,
        order_id: uuid.UUID,
        payload: POSCancelOrderRequest,
    ) -> POSCancelOrderResponse:
        """Cancel order from cashier, optionally refund payment, free table, and notify KDS."""
        stmt = (
            select(Order)
            .options(selectinload(Order.table), selectinload(Order.payments))
            .where(
                Order.id == order_id,
                Order.branch_id == branch_id,
                Order.tenant_id == context.tenant_id,
            )
        )
        res = await db.execute(stmt)
        order = res.scalar_one_or_none()

        if not order:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="ORDER_NOT_FOUND",
            )

        if order.status in (OrderStatus.CANCELLED, OrderStatus.CLOSED):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="ORDER_ALREADY_CLOSED_OR_CANCELLED",
            )

        order.status = OrderStatus.CANCELLED
        order.cancellation_reason = payload.reason

        # Refund payments if requested
        is_refunded = False
        if payload.refund_payment and order.payments:
            for p in order.payments:
                if p.status == PaymentStatus.COMPLETED:
                    p.status = PaymentStatus.REFUNDED
                    is_refunded = True

        # Free table if dine-in
        table_freed = False
        if order.table:
            # Check if any other active orders exist on this table
            active_stmt = select(Order).where(
                Order.table_id == order.table.id,
                Order.id != order.id,
                Order.status.in_(OPEN_ORDER_STATUSES),
            )
            act_res = await db.execute(active_stmt)
            if not act_res.scalars().first():
                order.table.status = TableStatus.AVAILABLE
                order.table.current_session_token = None
                table_freed = True

        order_id_val = order.id
        order_status_val = order.status.value

        await db.commit()

        # Audit logging
        await AuditLogger.log(
            tenant_id=context.tenant_id,
            branch_id=branch_id,
            action="POS_ORDER_CANCELLED",
            resource_type="orders",
            resource_id=str(order_id_val),
            user_id=context.user.id if context.user else None,
            actor_role=context.role.value,
            changes={
                "reason": payload.reason,
                "is_refunded": is_refunded,
                "table_freed": table_freed,
                "status": order_status_val,
            },
            status="SUCCESS",
        )

        # Notify KDS to drop tickets
        try:
            await publish(
                channel=f"branch_{branch_id}_kitchen",
                event_type="ORDER_CANCELLED",
                data={
                    "order_id": str(order_id_val),
                    "reason": payload.reason,
                },
            )
        except Exception as exc:
            logger.warning("Failed to publish cancellation event: %s", exc)

        return POSCancelOrderResponse(
            order_id=order_id_val,
            status=order_status_val,
            cancellation_reason=payload.reason,
            is_refunded=is_refunded,
            table_freed=table_freed,
            message="Order cancelled and processed successfully.",
        )
