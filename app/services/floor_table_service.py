"""Floor Table Service: Real-time table state, session aggregation engine, and floor WebSocket broadcasting."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import redis.asyncio as aioredis
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis_pubsub import publish
from app.models.auth import Table
from app.models.enums import (
    OrderStatus,
    ServiceRequestStatus,
    ServiceRequestType,
    TableStatus,
)
from app.models.order import Order
from app.models.service import ServiceRequest
from app.models.table import TableSession
from app.schemas.floor import (
    FloorSummaryResponse,
    FloorTableLiveResponse,
    TableOccupancyState,
)

logger = logging.getLogger("app.services.floor_table_service")

# Statuses indicating an order has ended its active dining presence
TERMINAL_ORDER_STATUSES = {
    OrderStatus.CLOSED,
    OrderStatus.CANCELLED,
    OrderStatus.PAID,
}


def _compute_duration_minutes(dt: datetime | None, now: datetime) -> int:
    """Compute elapsed duration in whole minutes, safely handling naive vs aware datetimes."""
    if not dt:
        return 0
    if dt.tzinfo is None and now.tzinfo is not None:
        dt = dt.replace(tzinfo=timezone.utc)
    elif dt.tzinfo is not None and now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    delta_seconds = (now - dt).total_seconds()
    return max(0, int(delta_seconds // 60))


class FloorTableService:
    """Aggregates physical table states, active dining sessions, orders, and service calls."""

    @staticmethod
    async def get_branch_live_tables(
        branch_id: uuid.UUID,
        db: AsyncSession,
    ) -> FloorSummaryResponse:
        """Aggregate all tables, active sessions, orders, and service requests in zero N+1 queries."""
        now = datetime.now(timezone.utc)

        # 1. Fetch all active dining tables belonging strictly to this branch
        table_stmt = (
            select(Table)
            .where(
                Table.branch_id == branch_id,
                Table.is_active.is_(True),
            )
            .order_by(Table.table_number)
        )
        table_res = await db.execute(table_stmt)
        tables = list(table_res.scalars().all())

        if not tables:
            return FloorSummaryResponse(
                total_tables=0,
                available_tables=0,
                occupied_tables=0,
                tables_awaiting_food=0,
                tables_with_pending_requests=0,
                tables=[],
            )

        table_ids = [t.id for t in tables]

        # 2. Batch query active table sessions (zero N+1)
        session_stmt = (
            select(TableSession)
            .where(
                TableSession.table_id.in_(table_ids),
                TableSession.is_active.is_(True),
            )
            .order_by(TableSession.created_at.desc())
        )
        session_res = await db.execute(session_stmt)
        active_sessions: dict[uuid.UUID, TableSession] = {}
        for s in session_res.scalars().all():
            if s.table_id not in active_sessions:
                active_sessions[s.table_id] = s

        # 3. Batch query active open orders (zero N+1)
        order_stmt = (
            select(Order)
            .where(
                Order.table_id.in_(table_ids),
                Order.status.notin_(TERMINAL_ORDER_STATUSES),
            )
            .order_by(Order.created_at.desc())
        )
        order_res = await db.execute(order_stmt)
        active_orders: dict[uuid.UUID, Order] = {}
        for o in order_res.scalars().all():
            if o.table_id not in active_orders:
                active_orders[o.table_id] = o

        # 4. Grouped query for pending service requests and bill requests (zero N+1)
        sr_stmt = (
            select(
                ServiceRequest.table_id,
                func.count(ServiceRequest.id).label("total_pending"),
                func.sum(
                    case(
                        (ServiceRequest.request_type == ServiceRequestType.BILL_REQUEST, 1),
                        else_=0,
                    )
                ).label("bill_requests_count"),
            )
            .where(
                ServiceRequest.table_id.in_(table_ids),
                ServiceRequest.status == ServiceRequestStatus.PENDING,
            )
            .group_by(ServiceRequest.table_id)
        )
        sr_res = await db.execute(sr_stmt)
        sr_data: dict[uuid.UUID, dict[str, int]] = {
            row.table_id: {
                "total_pending": int(row.total_pending or 0),
                "bill_requests_count": int(row.bill_requests_count or 0),
            }
            for row in sr_res.all()
        }

        # 5. Transform and compute live table occupancy and floor summary
        table_responses: list[FloorTableLiveResponse] = []
        available_tables = 0
        occupied_tables = 0
        tables_awaiting_food = 0
        tables_with_pending_requests = 0

        for table in tables:
            session = active_sessions.get(table.id)
            order = active_orders.get(table.id)
            sr_metrics = sr_data.get(table.id, {"total_pending": 0, "bill_requests_count": 0})
            pending_sr_count = sr_metrics["total_pending"]
            has_bill_request = sr_metrics["bill_requests_count"] > 0

            # Determine dynamic occupancy state
            if table.status == TableStatus.BILL_REQUESTED or has_bill_request:
                current_state = TableOccupancyState.BILL_REQUESTED
            elif order is not None:
                if order.status in (OrderStatus.READY, OrderStatus.DELIVERED, OrderStatus.SERVED):
                    current_state = TableOccupancyState.FOOD_SERVED
                elif order.status in (
                    OrderStatus.SUBMITTED,
                    OrderStatus.PREPARING,
                    OrderStatus.DRAFT,
                    OrderStatus.PENDING_STAFF_CONFIRMATION,
                ):
                    current_state = TableOccupancyState.AWAITING_FOOD
                else:
                    current_state = TableOccupancyState.AWAITING_FOOD
            elif (
                session is not None
                or (table.current_session_token is not None and table.status != TableStatus.AVAILABLE)
                or table.status in (TableStatus.BROWSING, TableStatus.AWAITING_FOOD, TableStatus.EATING)
            ):
                current_state = TableOccupancyState.SEATED
            else:
                current_state = TableOccupancyState.AVAILABLE

            # Calculate occupancy duration in minutes
            if current_state == TableOccupancyState.AVAILABLE:
                occupancy_minutes = 0
            elif session is not None and session.created_at is not None:
                occupancy_minutes = _compute_duration_minutes(session.created_at, now)
            elif order is not None and order.created_at is not None:
                occupancy_minutes = _compute_duration_minutes(order.created_at, now)
            elif table.status != TableStatus.AVAILABLE and table.updated_at is not None:
                occupancy_minutes = _compute_duration_minutes(table.updated_at, now)
            elif table.status != TableStatus.AVAILABLE and table.created_at is not None:
                occupancy_minutes = _compute_duration_minutes(table.created_at, now)
            else:
                occupancy_minutes = 0

            # Resolve active session identifier
            resolved_session_id: uuid.UUID | None = None
            if session is not None:
                resolved_session_id = session.id
            elif table.current_session_token:
                try:
                    resolved_session_id = uuid.UUID(table.current_session_token)
                except ValueError:
                    resolved_session_id = None

            # Summary tallying
            if current_state == TableOccupancyState.AVAILABLE:
                available_tables += 1
            else:
                occupied_tables += 1

            if current_state == TableOccupancyState.AWAITING_FOOD:
                tables_awaiting_food += 1

            if pending_sr_count > 0:
                tables_with_pending_requests += 1

            table_responses.append(
                FloorTableLiveResponse(
                    table_id=table.id,
                    table_number=table.table_number,
                    capacity=table.capacity,
                    current_state=current_state,
                    occupancy_duration_minutes=occupancy_minutes,
                    active_session_id=resolved_session_id,
                    active_order_id=order.id if order else None,
                    order_status=order.status if order else None,
                    order_total=Decimal(str(order.total_amount)) if (order and order.total_amount is not None) else None,
                    pending_service_requests_count=pending_sr_count,
                )
            )

        return FloorSummaryResponse(
            total_tables=len(tables),
            available_tables=available_tables,
            occupied_tables=occupied_tables,
            tables_awaiting_food=tables_awaiting_food,
            tables_with_pending_requests=tables_with_pending_requests,
            tables=table_responses,
        )

    @staticmethod
    async def broadcast_floor_event(
        branch_id: uuid.UUID,
        event_type: str,
        table_id: uuid.UUID,
        payload: dict[str, Any] | None = None,
        redis: aioredis.Redis | None = None,
    ) -> None:
        """Publishes a real-time event frame to the branch floor channel."""
        channel = f"branch_{branch_id}_floor"
        message_data = {
            "event": event_type,
            "branch_id": str(branch_id),
            "table_id": str(table_id),
            **(payload or {}),
        }
        try:
            if redis is not None:
                await redis.publish(channel, json.dumps(message_data))
            else:
                await publish(channel=channel, event_type=event_type, data=message_data)
        except Exception as exc:
            # Non-blocking: fail gracefully if Redis publish experiences transient errors
            logger.warning("Failed to broadcast floor event on channel %s: %s", channel, exc)
