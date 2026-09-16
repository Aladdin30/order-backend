"""Payment settlement engine, gateway intent generation, webhooks, and cash/POS ledger."""

from __future__ import annotations

import datetime
import logging
import uuid
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.i18n import SupportedLocale, get_localized_message
from app.core.payment_gateways import get_payment_gateway
from app.models.auth import Table
from app.models.enums import (
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
    ServiceRequestStatus,
    TableStatus,
)
from app.models.order import Order, Payment
from app.models.service import ServiceRequest
from app.schemas.payment import (
    InitiateOnlinePaymentRequest,
    InitiateOnlinePaymentResponse,
    OfflinePaymentRequest,
    PaymentResponse,
    PaymentSettlementResponse,
    VerifyOfflinePaymentRequest,
)
from app.schemas.session import GuestSessionContext
from app.services.audit_service import AuditLogger

logger = logging.getLogger(__name__)

CURRENCY_ROUNDING = Decimal("0.01")


def round_currency(value: Decimal | float | int | str) -> Decimal:
    """Consistently round monetary amounts to 2 decimal places using ROUND_HALF_UP."""
    return Decimal(str(value)).quantize(CURRENCY_ROUNDING, rounding=ROUND_HALF_UP)


def build_payment_response(payment: Payment) -> PaymentResponse:
    """Map Payment ORM model to immutable response schema."""
    return PaymentResponse(
        id=payment.id,
        order_id=payment.order_id,
        amount=round_currency(payment.amount),
        currency=payment.currency,
        payment_method=payment.payment_method,
        status=payment.status,
        transaction_reference=payment.transaction_reference,
        idempotency_key=payment.idempotency_key,
        verified_by_user_id=payment.verified_by_user_id,
        verified_at=payment.verified_at,
        created_at=payment.created_at,
    )


class PaymentService:
    """Payment processing engine for online checkouts, webhooks, and cashier ledger settlement."""

    @classmethod
    async def initiate_online_payment(
        cls,
        db: AsyncSession,
        session: GuestSessionContext,
        payload: InitiateOnlinePaymentRequest,
        locale: SupportedLocale | None = None,
    ) -> InitiateOnlinePaymentResponse:
        """Initiate an online payment session, generate gateway intent, and register PENDING payment."""
        # 1. Resolve Target Order
        if payload.order_id:
            stmt = (
                select(Order)
                .options(selectinload(Order.payments))
                .where(
                    Order.id == payload.order_id,
                    Order.branch_id == session.branch_id,
                )
            )
        else:
            stmt = (
                select(Order)
                .options(selectinload(Order.payments))
                .where(
                    Order.table_id == session.table_id,
                    Order.status.notin_([OrderStatus.CLOSED, OrderStatus.CANCELLED]),
                )
                .order_by(Order.created_at.desc())
                .limit(1)
            )
        order = (await db.execute(stmt)).scalar_one_or_none()

        if order is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=get_localized_message("NO_ACTIVE_ORDER", locale),
            )

        if order.status == OrderStatus.CANCELLED:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=get_localized_message("ORDER_CANCELLED_CANNOT_PAY", locale),
            )

        # 2. Idempotency Check on client key
        if payload.idempotency_key:
            stmt_idem = select(Payment).where(Payment.idempotency_key == payload.idempotency_key)
            existing_payment = (await db.execute(stmt_idem)).scalar_one_or_none()
            if existing_payment is not None:
                gateway = get_payment_gateway("stripe" if payload.method in (PaymentMethod.ONLINE_CARD, PaymentMethod.STRIPE, PaymentMethod.APPLE_PAY) else "local")
                intent = gateway.create_payment_intent(
                    amount=existing_payment.amount,
                    currency=existing_payment.currency,
                    transaction_reference=existing_payment.transaction_reference or f"txn_{existing_payment.id.hex[:12]}",
                )
                return InitiateOnlinePaymentResponse(
                    payment_id=existing_payment.id,
                    order_id=existing_payment.order_id,
                    amount=round_currency(existing_payment.amount),
                    currency=existing_payment.currency,
                    status=existing_payment.status,
                    transaction_reference=existing_payment.transaction_reference or intent["transaction_reference"],
                    client_secret=intent["client_secret"],
                    checkout_url=intent.get("checkout_url"),
                    idempotency_key=existing_payment.idempotency_key,
                )

        # 3. Calculate Remaining Unpaid Balance
        completed_amount = sum(
            (p.amount for p in order.payments if p.status == PaymentStatus.COMPLETED),
            Decimal("0.00"),
        )
        remaining_balance = round_currency(order.total_amount - completed_amount)

        if remaining_balance <= Decimal("0.00") or order.is_paid:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=get_localized_message("ORDER_ALREADY_PAID", locale),
            )

        # Determine payment amount
        if payload.amount is not None:
            payment_amount = round_currency(payload.amount)
            if payment_amount <= Decimal("0.00") or payment_amount > remaining_balance:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=get_localized_message("PAYMENT_AMOUNT_INVALID", locale),
                )
        else:
            payment_amount = remaining_balance

        # 4. Generate Reference & Intent
        txn_ref = f"txn_online_{uuid.uuid4().hex[:16]}"
        gateway_name = "stripe" if payload.method in (PaymentMethod.ONLINE_CARD, PaymentMethod.STRIPE, PaymentMethod.APPLE_PAY) else "local"
        gateway = get_payment_gateway(gateway_name)
        intent = gateway.create_payment_intent(
            amount=payment_amount,
            currency="SAR",
            transaction_reference=txn_ref,
            metadata={"order_id": str(order.id), "table_id": str(session.table_id)},
        )

        now = datetime.datetime.now(datetime.timezone.utc)
        payment = Payment(
            id=uuid.uuid4(),
            tenant_id=session.tenant_id,
            branch_id=session.branch_id,
            order_id=order.id,
            payment_method=payload.method,
            amount=payment_amount,
            currency="SAR",
            status=PaymentStatus.PENDING,
            transaction_reference=txn_ref,
            idempotency_key=payload.idempotency_key or f"idem_{uuid.uuid4().hex[:16]}",
            created_at=now,
            updated_at=now,
        )
        db.add(payment)
        await db.commit()
        await db.refresh(payment)

        # 5. Audit Log
        await AuditLogger.log(
            tenant_id=session.tenant_id,
            action="PAYMENT_INITIATED",
            resource_type="payment",
            resource_id=str(payment.id),
            branch_id=session.branch_id,
            actor_role="GUEST",
            changes={
                "order_id": str(order.id),
                "amount": str(payment_amount),
                "method": payload.method.value,
                "transaction_reference": txn_ref,
            },
        )

        return InitiateOnlinePaymentResponse(
            payment_id=payment.id,
            order_id=payment.order_id,
            amount=round_currency(payment.amount),
            currency=payment.currency,
            status=payment.status,
            transaction_reference=txn_ref,
            client_secret=intent["client_secret"],
            checkout_url=intent.get("checkout_url"),
            idempotency_key=payment.idempotency_key,
        )

    @classmethod
    async def process_webhook(
        cls,
        db: AsyncSession,
        provider: str,
        raw_payload: bytes,
        signature_header: str | None,
        locale: SupportedLocale | None = None,
    ) -> dict[str, Any]:
        """Verify webhook signature, guard against replay, and finalize order settlement."""
        gateway = get_payment_gateway(provider)
        event = gateway.verify_webhook_signature(raw_payload, signature_header)

        # Extract transaction reference and status from event payload
        txn_ref = (
            event.get("transaction_reference")
            or event.get("data", {}).get("object", {}).get("metadata", {}).get("transaction_reference")
            or event.get("data", {}).get("object", {}).get("id")
            or event.get("id")
        )

        if not txn_ref:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Transaction reference missing in webhook event",
            )

        # 1. Early Webhook Replay & Idempotency Guard (Before locking or transactions)
        stmt_find = select(Payment).where(Payment.transaction_reference == txn_ref)
        payment = (await db.execute(stmt_find)).scalar_one_or_none()

        if payment is None:
            # Check by id if reference was a uuid
            try:
                p_uuid = uuid.UUID(txn_ref)
                stmt_uuid = select(Payment).where(Payment.id == p_uuid)
                payment = (await db.execute(stmt_uuid)).scalar_one_or_none()
            except ValueError:
                pass

        if payment is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=get_localized_message("PAYMENT_NOT_FOUND", locale),
            )

        if payment.status == PaymentStatus.COMPLETED:
            logger.info("Webhook replay ignored for already COMPLETED payment %s", payment.id)
            return {"status": "already_processed", "payment_id": str(payment.id)}

        # 2. Acquire Pessimistic Row Lock on Table first (Parent) then Order (Child)
        now = datetime.datetime.now(datetime.timezone.utc)
        order_ref_stmt = select(Order.table_id).where(Order.id == payment.order_id)
        order_table_id = (await db.execute(order_ref_stmt)).scalar_one_or_none()

        table = None
        if order_table_id is not None:
            stmt_table = select(Table).where(Table.id == order_table_id).with_for_update()
            table = (await db.execute(stmt_table)).scalar_one_or_none()

        stmt_order = (
            select(Order)
            .where(Order.id == payment.order_id)
            .with_for_update()
        )
        order = (await db.execute(stmt_order)).scalar_one()

        # Update payment to COMPLETED
        payment.status = PaymentStatus.COMPLETED
        payment.verified_at = now
        payment.updated_at = now

        # 3. Calculate Total Completed Settlement
        stmt_all_payments = select(Payment).where(
            Payment.order_id == order.id,
            Payment.status == PaymentStatus.COMPLETED,
        )
        completed_payments = (await db.execute(stmt_all_payments)).scalars().all()
        # Include current payment if not yet in DB query
        completed_ids = {p.id for p in completed_payments}
        total_completed = sum((p.amount for p in completed_payments), Decimal("0.00"))
        if payment.id not in completed_ids:
            total_completed += payment.amount

        total_completed = round_currency(total_completed)

        # 4. Finalize Order and Table Teardown if Fully Paid
        order_closed = False
        if total_completed >= order.total_amount:
            order.is_paid = True
            order.status = OrderStatus.CLOSED
            order.updated_at = now
            order_closed = True

            # Table teardown: clear session token and reset to AVAILABLE
            if table:
                table.status = TableStatus.AVAILABLE
                table.current_session_token = None
                table.updated_at = now

                # Teardown active service requests on settled table
                await db.execute(
                    update(ServiceRequest)
                    .where(
                        ServiceRequest.table_id == table.id,
                        ServiceRequest.status.in_([
                            ServiceRequestStatus.PENDING,
                            ServiceRequestStatus.ACKNOWLEDGED,
                        ]),
                    )
                    .values(
                        status=ServiceRequestStatus.DISMISSED,
                        dismissed_at=now,
                    )
                )

        tenant_id = payment.tenant_id or order.tenant_id
        branch_id = payment.branch_id or order.branch_id
        payment_id_str = str(payment.id)
        order_id_str = str(order.id)

        await db.commit()

        # 5. Audit Log
        await AuditLogger.log(
            tenant_id=tenant_id,
            action="PAYMENT_PROCESSED_ONLINE",
            resource_type="payment",
            resource_id=payment_id_str,
            branch_id=branch_id,
            actor_role="WEBHOOK",
            changes={
                "order_id": order_id_str,
                "amount": str(payment.amount),
                "status": PaymentStatus.COMPLETED.value,
                "order_closed": order_closed,
            },
        )

        return {
            "status": "processed",
            "payment_id": payment_id_str,
            "order_id": order_id_str,
            "order_closed": order_closed,
        }

    @classmethod
    async def request_offline_payment(
        cls,
        db: AsyncSession,
        session: GuestSessionContext,
        payload: OfflinePaymentRequest,
        locale: SupportedLocale | None = None,
    ) -> PaymentResponse:
        """Guest requests cash collection or mobile POS terminal at the table."""
        # 1. Resolve Target Order
        if payload.order_id:
            stmt = (
                select(Order)
                .options(selectinload(Order.payments))
                .where(
                    Order.id == payload.order_id,
                    Order.branch_id == session.branch_id,
                )
            )
        else:
            stmt = (
                select(Order)
                .options(selectinload(Order.payments))
                .where(
                    Order.table_id == session.table_id,
                    Order.status.notin_([OrderStatus.CLOSED, OrderStatus.CANCELLED]),
                )
                .order_by(Order.created_at.desc())
                .limit(1)
            )
        order = (await db.execute(stmt)).scalar_one_or_none()

        if order is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=get_localized_message("NO_ACTIVE_ORDER", locale),
            )

        if order.status == OrderStatus.CANCELLED:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=get_localized_message("ORDER_CANCELLED_CANNOT_PAY", locale),
            )

        # 2. Prevent active duplicate offline request
        stmt_dup = select(Payment).where(
            Payment.order_id == order.id,
            Payment.status == PaymentStatus.PENDING_CASHIER_VERIFICATION,
        )
        dup = (await db.execute(stmt_dup)).scalar_one_or_none()
        if dup is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=get_localized_message("ACTIVE_OFFLINE_PAYMENT_EXISTS", locale),
            )

        # 3. Calculate Balance
        completed_amount = sum(
            (p.amount for p in order.payments if p.status == PaymentStatus.COMPLETED),
            Decimal("0.00"),
        )
        remaining_balance = round_currency(order.total_amount - completed_amount)

        if remaining_balance <= Decimal("0.00") or order.is_paid:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=get_localized_message("ORDER_ALREADY_PAID", locale),
            )

        if payload.amount is not None:
            payment_amount = round_currency(payload.amount)
            if payment_amount <= Decimal("0.00") or payment_amount > remaining_balance:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=get_localized_message("PAYMENT_AMOUNT_INVALID", locale),
                )
        else:
            payment_amount = remaining_balance

        # 4. Create Payment and Update Table Status to BILL_REQUESTED
        now = datetime.datetime.now(datetime.timezone.utc)
        txn_ref = f"txn_offline_{uuid.uuid4().hex[:12]}"
        payment = Payment(
            id=uuid.uuid4(),
            tenant_id=session.tenant_id,
            branch_id=session.branch_id,
            order_id=order.id,
            payment_method=payload.method,
            amount=payment_amount,
            currency="SAR",
            status=PaymentStatus.PENDING_CASHIER_VERIFICATION,
            transaction_reference=txn_ref,
            idempotency_key=f"idem_offline_{uuid.uuid4().hex[:16]}",
            created_at=now,
            updated_at=now,
        )
        db.add(payment)
        await db.flush()

        # Update Table Status to BILL_REQUESTED
        stmt_table = select(Table).where(Table.id == session.table_id)
        table = (await db.execute(stmt_table)).scalar_one_or_none()
        if table:
            table.status = TableStatus.BILL_REQUESTED
            table.updated_at = now

        response = build_payment_response(payment)
        await db.commit()

        # 5. Audit Log
        await AuditLogger.log(
            tenant_id=session.tenant_id,
            action="OFFLINE_PAYMENT_REQUESTED",
            resource_type="payment",
            resource_id=str(payment.id),
            branch_id=session.branch_id,
            actor_role="GUEST",
            changes={
                "order_id": str(order.id),
                "amount": str(payment_amount),
                "method": payload.method.value,
                "table_status": TableStatus.BILL_REQUESTED.value,
            },
        )

        return response

    @classmethod
    async def verify_offline_payment(
        cls,
        db: AsyncSession,
        payment_id: uuid.UUID,
        branch_id: uuid.UUID,
        cashier_id: uuid.UUID,
        cashier_role: str,
        payload: VerifyOfflinePaymentRequest | None = None,
        locale: SupportedLocale | None = None,
    ) -> PaymentSettlementResponse:
        """Cashier confirms cash collection or external POS slip, settling the payment and order."""
        # 1. Fetch Payment Scoped to Branch
        stmt_payment = select(Payment).where(
            Payment.id == payment_id,
            Payment.branch_id == branch_id,
        )
        payment = (await db.execute(stmt_payment)).scalar_one_or_none()

        if payment is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=get_localized_message("PAYMENT_NOT_FOUND", locale),
            )

        if payment.status == PaymentStatus.COMPLETED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=get_localized_message("PAYMENT_ALREADY_SETTLED", locale),
            )

        # 2. Acquire Pessimistic Row Lock on Table first (Parent) then Order (Child)
        order_ref_stmt = select(Order.table_id).where(Order.id == payment.order_id)
        order_table_id = (await db.execute(order_ref_stmt)).scalar_one_or_none()

        table = None
        if order_table_id is not None:
            stmt_table = select(Table).where(Table.id == order_table_id).with_for_update()
            table = (await db.execute(stmt_table)).scalar_one_or_none()

        stmt_order = (
            select(Order)
            .where(Order.id == payment.order_id)
            .with_for_update()
        )
        order = (await db.execute(stmt_order)).scalar_one()

        now = datetime.datetime.now(datetime.timezone.utc)
        payment.status = PaymentStatus.COMPLETED
        payment.verified_by_user_id = cashier_id
        payment.verified_at = now
        payment.updated_at = now

        # 3. Recompute Total Completed Payments
        stmt_all_payments = select(Payment).where(
            Payment.order_id == order.id,
            Payment.status == PaymentStatus.COMPLETED,
        )
        completed_payments = (await db.execute(stmt_all_payments)).scalars().all()
        completed_ids = {p.id for p in completed_payments}
        total_completed = sum((p.amount for p in completed_payments), Decimal("0.00"))
        if payment.id not in completed_ids:
            total_completed += payment.amount

        total_completed = round_currency(total_completed)
        remaining_balance = round_currency(max(Decimal("0.00"), order.total_amount - total_completed))

        # 4. Finalize Order & Table Teardown
        table_status = TableStatus.BILL_REQUESTED
        if total_completed >= order.total_amount:
            order.is_paid = True
            order.status = OrderStatus.CLOSED
            order.updated_at = now

            # Teardown table: clear current_session_token and set AVAILABLE
            if table:
                table.status = TableStatus.AVAILABLE
                table.current_session_token = None
                table.updated_at = now
                table_status = TableStatus.AVAILABLE

                # Teardown active service requests on settled table
                await db.execute(
                    update(ServiceRequest)
                    .where(
                        ServiceRequest.table_id == table.id,
                        ServiceRequest.status.in_([
                            ServiceRequestStatus.PENDING,
                            ServiceRequestStatus.ACKNOWLEDGED,
                        ]),
                    )
                    .values(
                        status=ServiceRequestStatus.DISMISSED,
                        dismissed_at=now,
                    )
                )
        else:
            if table:
                table_status = table.status

        payment_resp = build_payment_response(payment)
        order_id = order.id
        order_status = order.status
        is_paid = order.is_paid

        await db.commit()

        # 5. Audit Log
        tenant_id = payment.tenant_id or order.tenant_id
        await AuditLogger.log(
            tenant_id=tenant_id,
            action="CASH_PAYMENT_VERIFIED",
            resource_type="payment",
            resource_id=str(payment.id),
            branch_id=branch_id,
            user_id=cashier_id,
            actor_role=cashier_role,
            changes={
                "order_id": str(order_id),
                "amount": str(payment.amount),
                "remaining_balance": str(remaining_balance),
                "order_status": order_status.value,
                "table_status": table_status.value,
                "notes": payload.notes if payload else None,
            },
        )

        return PaymentSettlementResponse(
            payment=payment_resp,
            order_id=order_id,
            order_status=order_status,
            is_paid=is_paid,
            remaining_balance=remaining_balance,
            table_status=table_status,
        )

    @classmethod
    async def get_pending_cashier_payments_for_branch(
        cls,
        db: AsyncSession,
        branch_id: uuid.UUID,
    ) -> list[PaymentResponse]:
        """Query list of offline payments awaiting cashier settlement for the branch."""
        stmt = (
            select(Payment)
            .where(
                Payment.branch_id == branch_id,
                Payment.status == PaymentStatus.PENDING_CASHIER_VERIFICATION,
            )
            .order_by(Payment.created_at.asc())
        )
        result = await db.execute(stmt)
        payments = result.scalars().all()
        return [build_payment_response(p) for p in payments]
