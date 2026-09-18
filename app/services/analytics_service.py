"""Analytics Service: High-performance cross-branch KPI aggregation and Menu Engineering engine."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from app.core.redis_pubsub import redis_pubsub
from app.models.auth import Branch
from app.models.catalog import Category, Item
from app.models.enums import (
    OrderSource,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
)
from app.models.order import Order, OrderItem, Payment
from app.schemas.analytics import (
    BranchesMatrixResponse,
    BranchPerformanceRow,
    CategoryPerformanceItem,
    DashboardConsolidatedResponse,
    ExecutiveKPISummary,
    ItemPerformanceItem,
    MenuPerformanceResponse,
    TimePeriod,
)

logger = logging.getLogger("app.services.analytics_service")

CACHE_TTL_SECONDS = 300
CASH_METHODS = {PaymentMethod.CASH}


def _ensure_aware(dt: datetime | None) -> datetime:
    if dt is None:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _to_decimal(val: Any) -> Decimal:
    if val is None:
        return Decimal("0.00")
    return Decimal(str(val)).quantize(Decimal("0.01"))


class AnalyticsService:
    """Enterprise analytical pipeline supporting global and branch-scoped metrics."""

    @staticmethod
    def resolve_time_bounds(
        period: TimePeriod | str,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> tuple[datetime, datetime]:
        """Convert a period token or custom date range into explicit UTC datetime bounds."""
        now = datetime.now(timezone.utc)
        p_str = str(period).lower()

        if p_str == TimePeriod.CUSTOM.value:
            start = _ensure_aware(start_date) if start_date else (now - timedelta(days=30))
            end = _ensure_aware(end_date) if end_date else now
            return start, end

        if p_str == TimePeriod.YESTERDAY.value:
            yest_date = now.date() - timedelta(days=1)
            y_start = datetime.combine(yest_date, time.min, tzinfo=timezone.utc)
            y_end = datetime.combine(yest_date, time.max, tzinfo=timezone.utc)
            return y_start, y_end

        # Quantize sliding now window ceiling to 300-second boundary for deterministic caching
        now_ts = int(now.timestamp())
        next_boundary = ((now_ts // CACHE_TTL_SECONDS) + 1) * CACHE_TTL_SECONDS
        ceil_now = datetime.fromtimestamp(next_boundary, tz=timezone.utc)

        if p_str == TimePeriod.TODAY.value:
            today_start = datetime.combine(now.date(), time.min, tzinfo=timezone.utc)
            return today_start, ceil_now
        elif p_str == TimePeriod.LAST_7_DAYS.value:
            return ceil_now - timedelta(days=7), ceil_now
        elif p_str == TimePeriod.LAST_30_DAYS.value:
            return ceil_now - timedelta(days=30), ceil_now
        else:
            return ceil_now - timedelta(days=30), ceil_now

    @classmethod
    async def get_dashboard(
        cls,
        db: AsyncSession,
        branch_id: uuid.UUID | None = None,
        period: TimePeriod | str = TimePeriod.LAST_30_DAYS,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        limit: int = 10,
        force_refresh: bool = False,
    ) -> DashboardConsolidatedResponse:
        """Fetch unified executive dashboard with Redis caching."""
        p_start, p_end = cls.resolve_time_bounds(period, start_date, end_date)
        period_key = str(period).lower()
        cache_key = f"analytics:{branch_id or 'global'}:{period_key}:{int(p_start.timestamp())}:{int(p_end.timestamp())}"
        if limit != 10:
            cache_key += f":lim{limit}"

        # 1. Attempt Redis Cache Lookup
        if not force_refresh:
            try:
                r_client = await redis_pubsub.get_redis_client()
                if r_client:
                    cached_data = await r_client.get(cache_key)
                    if cached_data:
                        parsed = json.loads(cached_data)
                        return DashboardConsolidatedResponse.model_validate(parsed)
            except Exception as exc:
                logger.warning("Redis cache get error: %s", exc)

        # 2. Compute Dashboard Analytics
        kpis, top_items, bottom_items, categories, rankings = await cls._aggregate_dashboard_metrics(
            db=db,
            branch_id=branch_id,
            period_start=p_start,
            period_end=p_end,
            limit=limit,
        )

        response = DashboardConsolidatedResponse(
            period_start=p_start,
            period_end=p_end,
            kpis=kpis,
            top_selling_items=top_items,
            bottom_selling_items=bottom_items,
            category_breakdown=categories,
            branch_rankings=rankings,
        )

        # 3. Store in Redis Cache
        try:
            r_client = await redis_pubsub.get_redis_client()
            if r_client:
                await r_client.set(
                    cache_key,
                    response.model_dump_json(),
                    ex=CACHE_TTL_SECONDS,
                )
        except Exception as exc:
            logger.warning("Redis cache set error: %s", exc)

        return response

    @classmethod
    async def get_menu_performance(
        cls,
        db: AsyncSession,
        branch_id: uuid.UUID | None = None,
        period: TimePeriod | str = TimePeriod.LAST_30_DAYS,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        limit: int = 10,
    ) -> MenuPerformanceResponse:
        """Fetch dedicated menu engineering metrics (top items, dead-stock, and categories)."""
        p_start, p_end = cls.resolve_time_bounds(period, start_date, end_date)
        _, top_items, bottom_items, categories, _ = await cls._aggregate_dashboard_metrics(
            db=db,
            branch_id=branch_id,
            period_start=p_start,
            period_end=p_end,
            limit=limit,
        )
        return MenuPerformanceResponse(
            period_start=p_start,
            period_end=p_end,
            top_selling_items=top_items,
            bottom_selling_items=bottom_items,
            category_breakdown=categories,
        )

    @classmethod
    async def get_branches_matrix(
        cls,
        db: AsyncSession,
        period: TimePeriod | str = TimePeriod.LAST_30_DAYS,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> BranchesMatrixResponse:
        """Fetch comparative matrix of all branches ranked by gross merchandise value."""
        p_start, p_end = cls.resolve_time_bounds(period, start_date, end_date)
        _, _, _, _, rankings = await cls._aggregate_dashboard_metrics(
            db=db,
            branch_id=None,
            period_start=p_start,
            period_end=p_end,
            limit=10,
        )
        return BranchesMatrixResponse(
            period_start=p_start,
            period_end=p_end,
            branches=rankings,
        )

    # -----------------------------------------------------------------------
    # Internal Zero-N+1 Aggregation Engine
    # -----------------------------------------------------------------------

    @classmethod
    async def _aggregate_dashboard_metrics(
        cls,
        db: AsyncSession,
        branch_id: uuid.UUID | None,
        period_start: datetime,
        period_end: datetime,
        limit: int,
    ) -> tuple[
        ExecutiveKPISummary,
        list[ItemPerformanceItem],
        list[ItemPerformanceItem],
        list[CategoryPerformanceItem],
        list[BranchPerformanceRow],
    ]:
        # 1. Fetch Orders within range with eager loaded order_items
        orders_stmt = (
            select(Order)
            .options(selectinload(Order.order_items))
            .where(
                Order.created_at >= period_start,
                Order.created_at <= period_end,
            )
        )
        if branch_id:
            orders_stmt = orders_stmt.where(Order.branch_id == branch_id)

        orders_res = await db.execute(orders_stmt)
        orders = list(orders_res.scalars().all())

        # 2. Fetch Payments within range
        payments_stmt = (
            select(Payment)
            .options(joinedload(Payment.order))
            .outerjoin(Order, Payment.order_id == Order.id)
            .where(
                Payment.created_at >= period_start,
                Payment.created_at <= period_end,
            )
        )
        if branch_id:
            payments_stmt = payments_stmt.where(
                (Payment.branch_id == branch_id) | (Order.branch_id == branch_id)
            )

        payments_res = await db.execute(payments_stmt)
        payments = list(payments_res.scalars().all())

        # 3. Categorize Orders
        paid_orders: list[Order] = []
        cancelled_orders: list[Order] = []

        for o in orders:
            if o.status == OrderStatus.CANCELLED:
                cancelled_orders.append(o)
            elif o.is_paid or o.status in (
                OrderStatus.PAID,
                OrderStatus.CLOSED,
                OrderStatus.READY,
                OrderStatus.DELIVERED,
                OrderStatus.SERVED,
                OrderStatus.PREPARING,
                OrderStatus.SUBMITTED,
            ):
                paid_orders.append(o)

        # 4. Financial Sums
        gmv = _to_decimal(sum(o.total_amount for o in paid_orders))
        net_revenue = _to_decimal(sum(o.subtotal for o in paid_orders))
        total_tax = _to_decimal(sum(o.tax_total for o in paid_orders))
        total_service_fees = _to_decimal(sum(o.service_fee_total for o in paid_orders))

        total_discounts = Decimal("0.00")
        for o in paid_orders:
            diff = ((o.subtotal or Decimal("0.00")) + (o.tax_total or Decimal("0.00")) + (o.service_fee_total or Decimal("0.00"))) - (o.total_amount or Decimal("0.00"))
            if diff > Decimal("0.00"):
                total_discounts += diff
        total_discounts = _to_decimal(total_discounts)

        total_refunds = _to_decimal(
            sum(p.amount for p in payments if p.status == PaymentStatus.REFUNDED)
        )

        paid_orders_count = len(paid_orders)
        aov = (gmv / Decimal(paid_orders_count)).quantize(Decimal("0.01")) if paid_orders_count > 0 else Decimal("0.00")

        total_paid_items = sum(oi.quantity for o in paid_orders for oi in o.order_items)
        avg_items = (
            (Decimal(total_paid_items) / Decimal(paid_orders_count)).quantize(Decimal("0.01"))
            if paid_orders_count > 0
            else Decimal("0.00")
        )

        kpis = ExecutiveKPISummary(
            gmv=gmv,
            net_revenue=net_revenue,
            total_tax=total_tax,
            total_service_fees=total_service_fees,
            total_discounts=total_discounts,
            total_refunds=total_refunds,
            total_orders=len(orders),
            paid_orders=paid_orders_count,
            cancelled_orders=len(cancelled_orders),
            aov=aov,
            average_items_per_order=avg_items,
        )

        # 5. Fetch Catalog Items & Categories
        items_stmt = (
            select(Item)
            .join(Category, Item.category_id == Category.id)
            .options(joinedload(Item.category))
            .where(Category.is_active.is_(True))
        )
        if branch_id:
            items_stmt = items_stmt.where(Category.branch_id == branch_id)

        items_res = await db.execute(items_stmt)
        all_catalog_items = list(items_res.scalars().all())

        # Map sales from order_items
        item_sales_map: dict[uuid.UUID, dict[str, Any]] = {}
        for item in all_catalog_items:
            item_sales_map[item.id] = {
                "item": item,
                "quantity": 0,
                "revenue": Decimal("0.00"),
                "cancellations": 0,
            }

        # Aggregate paid quantities and revenues
        for o in paid_orders:
            for oi in o.order_items:
                if oi.item_id in item_sales_map:
                    item_sales_map[oi.item_id]["quantity"] += oi.quantity
                    item_sales_map[oi.item_id]["revenue"] += (oi.unit_price * oi.quantity)

        # Aggregate cancellations
        for o in cancelled_orders:
            for oi in o.order_items:
                if oi.item_id in item_sales_map:
                    item_sales_map[oi.item_id]["cancellations"] += oi.quantity

        # Build list of ItemPerformanceItem
        item_metrics_list: list[ItemPerformanceItem] = []
        for it_id, data in item_sales_map.items():
            it = data["item"]
            rev = _to_decimal(data["revenue"])
            qty = data["quantity"]
            canc = data["cancellations"]
            contrib = (
                (rev / gmv * 100).quantize(Decimal("0.01")) if gmv > Decimal("0.00") else Decimal("0.00")
            )
            cat_name = it.category.name if it.category else None

            item_metrics_list.append(
                ItemPerformanceItem(
                    item_id=it.id,
                    item_name=it.name,
                    category_name=cat_name,
                    total_quantity_sold=qty,
                    gross_revenue=rev,
                    unit_price=_to_decimal(it.base_price),
                    cancellations_count=canc,
                    sales_contribution_percentage=contrib,
                )
            )

        # Top-Selling: sorted descending by quantity, then revenue
        top_items = sorted(
            [item for item in item_metrics_list if item.total_quantity_sold > 0],
            key=lambda x: (x.total_quantity_sold, x.gross_revenue),
            reverse=True,
        )[:limit]
        if not top_items and item_metrics_list:
            top_items = sorted(
                item_metrics_list,
                key=lambda x: (x.total_quantity_sold, x.gross_revenue),
                reverse=True,
            )[:limit]

        # Bottom-Selling (Dead-Stock): sorted ascending by quantity, then revenue
        bottom_items = sorted(
            item_metrics_list,
            key=lambda x: (x.total_quantity_sold, x.gross_revenue),
        )[:limit]

        # Category Breakdown
        category_map: dict[uuid.UUID, dict[str, Any]] = {}
        for it in all_catalog_items:
            if it.category_id:
                if it.category_id not in category_map:
                    category_map[it.category_id] = {
                        "id": it.category_id,
                        "name": it.category.name if it.category else "Unknown",
                        "items_sold": 0,
                        "revenue": Decimal("0.00"),
                    }
                data = item_sales_map.get(it.id)
                if data:
                    category_map[it.category_id]["items_sold"] += data["quantity"]
                    category_map[it.category_id]["revenue"] += data["revenue"]

        categories: list[CategoryPerformanceItem] = []
        for c_id, c_data in category_map.items():
            c_rev = _to_decimal(c_data["revenue"])
            c_share = (
                (c_rev / gmv * 100).quantize(Decimal("0.01")) if gmv > Decimal("0.00") else Decimal("0.00")
            )
            categories.append(
                CategoryPerformanceItem(
                    category_id=c_id,
                    category_name=c_data["name"],
                    total_items_sold=c_data["items_sold"],
                    total_revenue=c_rev,
                    gmv_share_percentage=c_share,
                )
            )
        categories = sorted(categories, key=lambda x: x.total_revenue, reverse=True)

        # 6. Cross-Branch Comparative Matrix
        rankings: list[BranchPerformanceRow] = []
        if branch_id is None:
            branches_stmt = select(Branch).where(Branch.is_active.is_(True))
            branches_res = await db.execute(branches_stmt)
            branches = list(branches_res.scalars().all())

            for b in branches:
                b_orders = [o for o in orders if o.branch_id == b.id]
                b_paid_orders = [o for o in paid_orders if o.branch_id == b.id]
                b_canc_orders = [o for o in cancelled_orders if o.branch_id == b.id]

                b_gmv = _to_decimal(sum(o.total_amount for o in b_paid_orders))
                b_paid_count = len(b_paid_orders)
                b_aov = (
                    (b_gmv / Decimal(b_paid_count)).quantize(Decimal("0.01"))
                    if b_paid_count > 0
                    else Decimal("0.00")
                )

                b_canc_rate = (
                    (Decimal(len(b_canc_orders)) / Decimal(len(b_orders)) * 100).quantize(Decimal("0.01"))
                    if b_orders
                    else Decimal("0.00")
                )

                # Payments breakdown for this branch
                b_payments = [
                    p
                    for p in payments
                    if p.status == PaymentStatus.COMPLETED and (p.branch_id == b.id or (p.order and p.order.branch_id == b.id))
                ]
                b_cash = _to_decimal(sum(p.amount for p in b_payments if p.payment_method in CASH_METHODS))
                b_digital = _to_decimal(sum(p.amount for p in b_payments if p.payment_method not in CASH_METHODS))

                # Fallback if simulated orders without payment rows
                if (b_cash + b_digital) == Decimal("0.00") and b_gmv > Decimal("0.00"):
                    for bo in b_paid_orders:
                        if bo.order_source == OrderSource.CASHIER_POS:
                            b_cash += _to_decimal(bo.total_amount)
                        else:
                            b_digital += _to_decimal(bo.total_amount)

                rankings.append(
                    BranchPerformanceRow(
                        branch_id=b.id,
                        branch_name=b.name,
                        gmv=b_gmv,
                        total_paid_orders=b_paid_count,
                        aov=b_aov,
                        cancellation_rate_percentage=b_canc_rate,
                        cash_revenue=b_cash,
                        digital_revenue=b_digital,
                    )
                )

            # Sort branches descending by GMV
            rankings = sorted(rankings, key=lambda x: x.gmv, reverse=True)

        return kpis, top_items, bottom_items, categories, rankings
