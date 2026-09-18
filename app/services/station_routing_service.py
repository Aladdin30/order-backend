"""Kitchen Station Routing & Decomposition Engine (Task BE-3.2).

Responsible for:
1. Station fallback inheritance chain:
   OrderItem.station = item.station || item.category.station || KitchenStation.HOT_KITCHEN
2. Decomposing multi-item table orders into station-scoped sub-tickets (KDS).
3. Dispatching sub-tickets via Redis Pub/Sub channels (branch_{id}_kitchen_{station}).
4. Concurrency-safe KDS bump bar lifecycle transitions with pessimistic row locking.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.redis_pubsub import publish
from app.models.catalog import Category, Item
from app.models.enums import KitchenStation, OrderStatus
from app.models.order import Order, OrderItem
from app.schemas.i18n import resolve_localized_string
from app.schemas.kds import (
    KDSBumpResponse,
    KDSSubTicket,
    KDSSubTicketItem,
)

logger = logging.getLogger("app.services.station_routing_service")


def _station_code_str(station: Any) -> str:
    """Safely convert KitchenStation Enum or string code to uppercase string."""
    if hasattr(station, "value"):
        return str(station.value)
    return str(station)


def _safe_get_kitchen_station(entity: Any) -> Any:
    """Safely retrieve kitchen_station relationship without triggering async lazy loading."""
    if entity is None:
        return None
    if hasattr(entity, "__dict__") and "kitchen_station" in entity.__dict__:
        return entity.__dict__["kitchen_station"]
    if not hasattr(entity, "_sa_instance_state"):
        return getattr(entity, "kitchen_station", None)
    return None


class StationRoutingService:
    """Service handling kitchen station resolution, decomposition, and KDS bump bar operations."""

    @staticmethod
    def resolve_item_station(item: Item | Any, category: Category | Any | None) -> KitchenStation | str:
        """Resolve kitchen station strictly following dynamic fallback precedence:

        item.kitchen_station.code || item.station_code || item.station || category.kitchen_station.code || category.station || KitchenStation.HOT_KITCHEN
        Safely serializes both Enum members and dynamic string codes.
        """
        # 1. Item dynamic station entity
        item_ks = _safe_get_kitchen_station(item)
        if item_ks is not None and getattr(item_ks, "code", None):
            code = str(item_ks.code).strip().upper()
            try:
                return KitchenStation(code)
            except ValueError:
                return code

        # 1b. Direct station_code on item if set
        if getattr(item, "station_code", None):
            code = str(item.station_code).strip().upper()
            try:
                return KitchenStation(code)
            except ValueError:
                return code

        # 2. Item legacy station enum or string
        item_stn = getattr(item, "station", None)
        if item_stn is not None:
            if isinstance(item_stn, KitchenStation):
                return item_stn
            code = str(getattr(item_stn, "value", item_stn)).strip().upper()
            try:
                return KitchenStation(code)
            except ValueError:
                return code

        # 3. Category dynamic station entity
        cat_ks = _safe_get_kitchen_station(category)
        if cat_ks is not None and getattr(cat_ks, "code", None):
            code = str(cat_ks.code).strip().upper()
            try:
                return KitchenStation(code)
            except ValueError:
                return code

        # 4. Category legacy station enum or string
        if category is not None:
            cat_stn = getattr(category, "station", None)
            if cat_stn is not None:
                if isinstance(cat_stn, KitchenStation):
                    return cat_stn
                code = str(getattr(cat_stn, "value", cat_stn)).strip().upper()
                try:
                    return KitchenStation(code)
                except ValueError:
                    return code

        # 5. Default fallback
        return KitchenStation.HOT_KITCHEN

    @classmethod
    def decompose_order(
        cls,
        order: Order,
        items: list[OrderItem] | None = None,
        table_number: str | None = None,
    ) -> dict[KitchenStation | str, KDSSubTicket]:
        """Decompose order items into station-scoped sub-tickets.

        Supports routing only newly appended items during reorder checkouts.
        """
        target_items = items if items is not None else order.order_items
        if not target_items:
            return {}

        # Resolve table number safely without triggering async lazy loading
        resolved_table_number = table_number
        if not resolved_table_number:
            table_obj = None
            if hasattr(order, "__dict__") and "table" in order.__dict__:
                table_obj = order.__dict__["table"]
            elif not hasattr(order, "_sa_instance_state"):
                table_obj = getattr(order, "table", None)

            if table_obj is not None and getattr(table_obj, "table_number", None):
                resolved_table_number = str(table_obj.table_number)
            else:
                resolved_table_number = "Unknown"

        # Group line items by resolved operational station
        items_by_station: dict[KitchenStation | str, list[KDSSubTicketItem]] = {}

        for line_item in target_items:
            station_code = getattr(line_item, "station_code", None)
            if station_code:
                try:
                    station: KitchenStation | str = KitchenStation(station_code)
                except ValueError:
                    station = station_code
            else:
                station = line_item.station

            # Resolve item display name safely without triggering async lazy loading
            item_name = "Item"
            item_obj = None
            if hasattr(line_item, "__dict__") and "item" in line_item.__dict__:
                item_obj = line_item.__dict__["item"]
            elif not hasattr(line_item, "_sa_instance_state"):
                item_obj = getattr(line_item, "item", None)

            if item_obj is not None and getattr(item_obj, "name", None):
                item_name = resolve_localized_string(item_obj.name)

            sub_item = KDSSubTicketItem(
                order_item_id=line_item.id,
                item_id=line_item.item_id,
                name=item_name,
                quantity=line_item.quantity,
                station=station,
                selected_modifiers=line_item.selected_modifiers or [],
                special_instructions=line_item.special_instructions,
                is_bumped=line_item.is_bumped,
            )

            if station not in items_by_station:
                items_by_station[station] = []
            items_by_station[station].append(sub_item)

        created_at = getattr(order, "created_at", None) or datetime.now(timezone.utc)
        sub_tickets: dict[KitchenStation | str, KDSSubTicket] = {}

        for stn, stn_items in items_by_station.items():
            total_count = len(stn_items)
            bumped_count = sum(1 for i in stn_items if i.is_bumped)
            code_str = _station_code_str(stn)

            ticket = KDSSubTicket(
                sub_ticket_id=f"{order.id}_{code_str}",
                order_id=order.id,
                branch_id=order.branch_id,
                table_id=order.table_id,
                table_number=resolved_table_number,
                pickup_number=getattr(order, "pickup_number", None),
                station=stn,
                order_status=order.status,
                order_type=order.order_type,
                customer_notes=order.customer_notes,
                created_at=created_at,
                items=stn_items,
                total_items_count=total_count,
                bumped_items_count=bumped_count,
                is_fully_bumped=bumped_count == total_count and total_count > 0,
            )
            sub_tickets[stn] = ticket

        return sub_tickets

    @classmethod
    async def dispatch_order_to_kds(
        cls,
        order: Order,
        items: list[OrderItem] | None = None,
        table_number: str | None = None,
    ) -> list[KDSSubTicket]:
        """Decompose order items and dispatch sub-tickets to scoped KDS Redis channels.

        Publishes to:
        - Dynamic Station Channel: branch_{branch_id}_station_{code.lower()}
        - Targeted Legacy Station Channel: branch_{branch_id}_kitchen_{code.lower()}
        - Consolidated Kitchen Channel: branch_{branch_id}_kitchen
        """
        sub_tickets = cls.decompose_order(order, items=items, table_number=table_number)
        dispatched: list[KDSSubTicket] = []

        for station, ticket in sub_tickets.items():
            payload = ticket.model_dump(mode="json")
            code_str = _station_code_str(station).lower()

            dynamic_channel = f"branch_{order.branch_id}_station_{code_str}"
            legacy_channel = f"branch_{order.branch_id}_kitchen_{code_str}"
            general_channel = f"branch_{order.branch_id}_kitchen"

            # 1. Dispatch dynamic station event
            await publish(
                channel=dynamic_channel,
                event_type="KDS_SUB_TICKET_CREATED",
                data=payload,
            )

            # 2. Dispatch legacy station event
            if legacy_channel != dynamic_channel:
                await publish(
                    channel=legacy_channel,
                    event_type="KDS_SUB_TICKET_CREATED",
                    data=payload,
                )

            # 3. Dispatch to general kitchen channel
            await publish(
                channel=general_channel,
                event_type="KDS_SUB_TICKET_CREATED",
                data=payload,
            )

            dispatched.append(ticket)
            logger.info(
                "Dispatched KDS sub-ticket for order %s to station %s (items: %d)",
                order.id,
                code_str.upper(),
                ticket.total_items_count,
            )

        return dispatched

    @classmethod
    async def bump_order_item(
        cls,
        db: AsyncSession,
        order_item_id: uuid.UUID,
        is_bumped: bool = True,
        actor_role: str | None = None,
        branch_id: uuid.UUID | None = None,
    ) -> KDSBumpResponse:
        """Bump or unbump an order item line on the KDS bump bar.

        Uses SELECT FOR UPDATE row locking on the parent Order to prevent race conditions
        when synchronizing preparation status and transitioning to READY.
        """
        # 1. Fetch the target order item
        item_stmt = select(OrderItem).where(OrderItem.id == order_item_id)
        item_res = await db.execute(item_stmt)
        order_item = item_res.scalar_one_or_none()

        if order_item is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="ORDER_ITEM_NOT_FOUND",
            )

        # 2. Concurrency: Lock parent Order row (SELECT FOR UPDATE)
        order_stmt = (
            select(Order)
            .where(Order.id == order_item.order_id)
            .with_for_update()
        )
        order_res = await db.execute(order_stmt)
        order = order_res.scalar_one()

        # Enforce branch isolation
        if branch_id is not None and order.branch_id != branch_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="ORDER_ITEM_NOT_FOUND",
            )

        # 3. Update bump status
        order_item.is_bumped = is_bumped
        await db.flush()

        # 4. Fetch all sibling order items to evaluate order preparation completeness
        siblings_stmt = select(OrderItem).where(OrderItem.order_id == order.id)
        siblings_res = await db.execute(siblings_stmt)
        all_items = list(siblings_res.scalars().all())

        total_count = len(all_items)
        bumped_count = sum(1 for i in all_items if i.is_bumped)
        all_bumped = (bumped_count == total_count and total_count > 0)

        # 5. Check state transitions
        order_became_ready = False
        if all_bumped and order.status in (OrderStatus.SUBMITTED, OrderStatus.PREPARING):
            order.status = OrderStatus.READY
            order_became_ready = True
            logger.info("Order %s fully bumped across all stations -> transitioned to READY", order.id)
        elif not all_bumped and order.status == OrderStatus.SUBMITTED and bumped_count > 0:
            order.status = OrderStatus.PREPARING
            logger.info("Order %s partially bumped (%d/%d) -> transitioned to PREPARING", order.id, bumped_count, total_count)

        await db.commit()

        # 6. Real-time post-commit notifications
        if order_became_ready:
            ready_payload = {
                "order_id": str(order.id),
                "branch_id": str(order.branch_id),
                "table_id": str(order.table_id),
                "status": OrderStatus.READY.value,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            await publish(
                channel=f"branch_{order.branch_id}_runners",
                event_type="ORDER_READY",
                data=ready_payload,
            )
            await publish(
                channel=f"branch_{order.branch_id}_table_{order.table_id}",
                event_type="ORDER_READY",
                data=ready_payload,
            )

        # 6. Publish KDS bump event
        effective_stn = order_item.station_code or order_item.station
        code_str = _station_code_str(effective_stn).lower()
        bump_payload = {
            "order_id": str(order.id),
            "order_item_id": str(order_item.id),
            "station": _station_code_str(effective_stn),
            "is_bumped": is_bumped,
            "order_status": order.status.value,
            "order_fully_prepared": all_bumped,
            "bumped_items_count": bumped_count,
            "total_items_count": total_count,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await publish(
            channel=f"branch_{order.branch_id}_kitchen",
            event_type="KDS_ITEM_BUMPED",
            data=bump_payload,
        )
        await publish(
            channel=f"branch_{order.branch_id}_kitchen_{code_str}",
            event_type="KDS_ITEM_BUMPED",
            data=bump_payload,
        )
        await publish(
            channel=f"branch_{order.branch_id}_station_{code_str}",
            event_type="KDS_ITEM_BUMPED",
            data=bump_payload,
        )

        return KDSBumpResponse(
            order_id=order.id,
            order_item_id=order_item.id,
            station=effective_stn,
            is_bumped=is_bumped,
            order_status=order.status,
            order_fully_prepared=all_bumped,
            bumped_items_count=bumped_count,
            total_items_count=total_count,
        )

    @classmethod
    async def bump_station_ticket(
        cls,
        db: AsyncSession,
        order_id: uuid.UUID,
        station: KitchenStation | str,
        is_bumped: bool = True,
        actor_role: str | None = None,
        branch_id: uuid.UUID | None = None,
    ) -> KDSBumpResponse:
        """Bulk bump or unbump all items assigned to a specific station for an order."""
        # 1. Concurrency: Lock parent Order row (SELECT FOR UPDATE)
        order_stmt = (
            select(Order)
            .where(Order.id == order_id)
            .with_for_update()
        )
        order_res = await db.execute(order_stmt)
        order = order_res.scalar_one_or_none()

        if order is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="ORDER_NOT_FOUND",
            )

        # Enforce branch isolation
        if branch_id is not None and order.branch_id != branch_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="ORDER_NOT_FOUND",
            )

        # 2. Fetch all items for this order
        siblings_stmt = select(OrderItem).where(OrderItem.order_id == order.id)
        siblings_res = await db.execute(siblings_stmt)
        all_items = list(siblings_res.scalars().all())

        # 3. Mark items of target station
        target_code = _station_code_str(station).upper()
        station_items_found = False
        for item in all_items:
            item_code = _station_code_str(getattr(item, "station_code", None) or item.station).upper()
            if item_code == target_code:
                item.is_bumped = is_bumped
                station_items_found = True

        if not station_items_found:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"NO_ITEMS_FOR_STATION_{target_code}",
            )

        await db.flush()

        total_count = len(all_items)
        bumped_count = sum(1 for i in all_items if i.is_bumped)
        all_bumped = (bumped_count == total_count and total_count > 0)

        # 4. Check state transitions
        order_became_ready = False
        if all_bumped and order.status in (OrderStatus.SUBMITTED, OrderStatus.PREPARING):
            order.status = OrderStatus.READY
            order_became_ready = True
            logger.info("Order %s bulk-bumped across all stations -> transitioned to READY", order.id)
        elif not all_bumped and order.status == OrderStatus.SUBMITTED and bumped_count > 0:
            order.status = OrderStatus.PREPARING

        await db.commit()

        # Post-commit ORDER_READY broadcast
        if order_became_ready:
            ready_payload = {
                "order_id": str(order.id),
                "branch_id": str(order.branch_id),
                "table_id": str(order.table_id),
                "status": OrderStatus.READY.value,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            await publish(
                channel=f"branch_{order.branch_id}_runners",
                event_type="ORDER_READY",
                data=ready_payload,
            )
            await publish(
                channel=f"branch_{order.branch_id}_table_{order.table_id}",
                event_type="ORDER_READY",
                data=ready_payload,
            )

        # 5. Broadcast bulk bump event
        code_str = target_code.lower()
        bulk_payload = {
            "order_id": str(order.id),
            "station": target_code,
            "is_bumped": is_bumped,
            "order_status": order.status.value,
            "order_fully_prepared": all_bumped,
            "bumped_items_count": bumped_count,
            "total_items_count": total_count,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await publish(
            channel=f"branch_{order.branch_id}_kitchen",
            event_type="KDS_STATION_BUMPED",
            data=bulk_payload,
        )
        await publish(
            channel=f"branch_{order.branch_id}_kitchen_{code_str}",
            event_type="KDS_STATION_BUMPED",
            data=bulk_payload,
        )
        await publish(
            channel=f"branch_{order.branch_id}_station_{code_str}",
            event_type="KDS_STATION_BUMPED",
            data=bulk_payload,
        )

        return KDSBumpResponse(
            order_id=order.id,
            station=station,
            is_bumped=is_bumped,
            order_status=order.status,
            order_fully_prepared=all_bumped,
            bumped_items_count=bumped_count,
            total_items_count=total_count,
        )

    @classmethod
    async def get_active_kds_tickets(
        cls,
        db: AsyncSession,
        branch_id: uuid.UUID,
        station: KitchenStation | str | None = None,
    ) -> list[KDSSubTicket]:
        """Fetch active kitchen sub-tickets (SUBMITTED, PREPARING) in FIFO order."""
        stmt = (
            select(Order)
            .options(
                selectinload(Order.table),
                selectinload(Order.order_items).selectinload(OrderItem.item),
            )
            .where(
                Order.branch_id == branch_id,
                Order.status.in_([OrderStatus.SUBMITTED, OrderStatus.PREPARING]),
            )
            .order_by(Order.created_at.asc())
        )
        result = await db.execute(stmt)
        orders = result.scalars().all()

        active_tickets: list[KDSSubTicket] = []
        for order in orders:
            decomposed = cls.decompose_order(order)
            if station is not None:
                filter_code = _station_code_str(station).upper()
                for k, v in decomposed.items():
                    if _station_code_str(k).upper() == filter_code:
                        active_tickets.append(v)
            else:
                active_tickets.extend(decomposed.values())

        # Return sorted by created_at (FIFO)
        active_tickets.sort(key=lambda t: t.created_at)
        return active_tickets
