"""Payment API endpoints for guest online checkout, public webhooks, and cashier settlement."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    EnforceBranchAccess,
    RequirePresenceVerified,
    RequireRoles,
    get_async_db,
    get_current_guest_session,
)
from app.core.context import SecurityContext
from app.core.i18n import parse_accept_language
from app.models.enums import UserRole
from app.schemas.payment import (
    InitiateOnlinePaymentRequest,
    InitiateOnlinePaymentResponse,
    OfflinePaymentRequest,
    PaymentResponse,
    PaymentSettlementResponse,
    VerifyOfflinePaymentRequest,
)
from app.schemas.session import GuestSessionContext
from app.services.payment_service import PaymentService

router = APIRouter(prefix="/payments", tags=["Payments & Ledger"])


# -------------------------------------------------------------------------
# Guest Online Payment Routes
# -------------------------------------------------------------------------


@router.post(
    "/online/initiate",
    response_model=InitiateOnlinePaymentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Initiate online payment session",
)
async def initiate_online_payment(
    request: Request,
    payload: InitiateOnlinePaymentRequest,
    session: GuestSessionContext = Depends(get_current_guest_session),
    db: AsyncSession = Depends(get_async_db),
    accept_language: str | None = Header(default=None),
) -> InitiateOnlinePaymentResponse:
    """Guest initializes an online card or digital wallet transaction for their table order."""
    locale = parse_accept_language(accept_language)
    return await PaymentService.initiate_online_payment(
        db=db,
        session=session,
        payload=payload,
        locale=locale,
    )


# -------------------------------------------------------------------------
# Public Webhook Route
# -------------------------------------------------------------------------


@router.post(
    "/webhooks/{provider}",
    summary="Public payment gateway webhook receiver",
)
async def payment_gateway_webhook(
    provider: str,
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    stripe_signature: str | None = Header(default=None, alias="Stripe-Signature"),
    x_signature: str | None = Header(default=None, alias="X-Signature"),
    accept_language: str | None = Header(default=None),
) -> dict[str, Any]:
    """Process cryptographic webhook events emitted by payment gateways (Stripe, Local HMAC)."""
    raw_payload = await request.body()
    signature = stripe_signature or x_signature or request.headers.get("signature")
    locale = parse_accept_language(accept_language)
    return await PaymentService.process_webhook(
        db=db,
        provider=provider,
        raw_payload=raw_payload,
        signature_header=signature,
        locale=locale,
    )


# -------------------------------------------------------------------------
# Guest Offline (Cash / POS Terminal) Payment Routes
# -------------------------------------------------------------------------


@router.post(
    "/offline/request",
    response_model=PaymentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Request table-side cash or POS terminal settlement",
)
async def request_offline_payment(
    request: Request,
    payload: OfflinePaymentRequest,
    session: GuestSessionContext = Depends(RequirePresenceVerified),
    db: AsyncSession = Depends(get_async_db),
    accept_language: str | None = Header(default=None),
) -> PaymentResponse:
    """Verified guest requests physical table-side cash collection or a wireless POS terminal."""
    locale = parse_accept_language(accept_language)
    return await PaymentService.request_offline_payment(
        db=db,
        session=session,
        payload=payload,
        locale=locale,
    )


# -------------------------------------------------------------------------
# Staff Cashier Settlement Routes
# -------------------------------------------------------------------------


@router.post(
    "/offline/{payment_id}/verify",
    response_model=PaymentSettlementResponse,
    summary="Cashier confirms physical cash collection or POS slip",
)
async def verify_offline_payment(
    request: Request,
    payment_id: uuid.UUID,
    payload: VerifyOfflinePaymentRequest | None = None,
    branch_id: uuid.UUID = Depends(EnforceBranchAccess()),
    context: SecurityContext = Depends(
        RequireRoles([UserRole.CASHIER, UserRole.BRANCH_ADMIN])
    ),
    db: AsyncSession = Depends(get_async_db),
    accept_language: str | None = Header(default=None),
) -> PaymentSettlementResponse:
    """Cashier confirms physical payment, settles the ledger, and closes the order."""
    locale = parse_accept_language(accept_language)
    return await PaymentService.verify_offline_payment(
        db=db,
        payment_id=payment_id,
        branch_id=branch_id,
        cashier_id=context.user.id,
        cashier_role=context.role.value,
        payload=payload,
        locale=locale,
    )


@router.get(
    "/branch/pending",
    response_model=list[PaymentResponse],
    summary="List pending offline payments awaiting cashier collection",
)
async def list_pending_branch_payments(
    request: Request,
    branch_id: uuid.UUID = Depends(EnforceBranchAccess()),
    context: SecurityContext = Depends(
        RequireRoles([UserRole.CASHIER, UserRole.BRANCH_ADMIN])
    ),
    db: AsyncSession = Depends(get_async_db),
) -> list[PaymentResponse]:
    """Retrieve all table payments awaiting cashier cash/POS verification for the branch."""
    return await PaymentService.get_pending_cashier_payments_for_branch(
        db=db,
        branch_id=branch_id,
    )
