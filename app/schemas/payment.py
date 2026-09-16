"""Pydantic v2 schemas for payment initiation, webhooks, and cash/terminal ledger settlement."""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.enums import (
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
    TableStatus,
)


class InitiateOnlinePaymentRequest(BaseModel):
    """Customer request to initiate an online checkout transaction."""

    order_id: uuid.UUID | None = Field(
        default=None,
        description="Target order UUID (defaults to active table order if omitted)",
    )
    amount: Decimal | None = Field(
        default=None,
        ge=Decimal("0.01"),
        description="Specific amount to pay (defaults to remaining unpaid balance)",
    )
    method: PaymentMethod = Field(
        default=PaymentMethod.ONLINE_CARD,
        description="Online payment method (ONLINE_CARD, APPLE_PAY, STRIPE)",
    )
    idempotency_key: str | None = Field(
        default=None,
        max_length=255,
        description="Client-provided idempotency key to prevent double checkout",
    )


class InitiateOnlinePaymentResponse(BaseModel):
    """Gateway intent response containing client secrets and reference IDs."""

    payment_id: uuid.UUID
    order_id: uuid.UUID
    amount: Decimal
    currency: str = "SAR"
    status: PaymentStatus
    transaction_reference: str
    client_secret: str
    checkout_url: str | None = None
    idempotency_key: str | None = None


class OfflinePaymentRequest(BaseModel):
    """Customer request for on-site cash collection or mobile POS terminal at the table."""

    order_id: uuid.UUID | None = Field(
        default=None,
        description="Target order UUID (defaults to active table order if omitted)",
    )
    method: PaymentMethod = Field(
        default=PaymentMethod.CASH,
        description="Physical payment method (CASH or POS_TERMINAL)",
    )
    amount: Decimal | None = Field(
        default=None,
        ge=Decimal("0.01"),
        description="Specific amount to pay (defaults to remaining unpaid balance)",
    )

    @model_validator(mode="after")
    def validate_offline_method(self) -> Self:
        allowed = {PaymentMethod.CASH, PaymentMethod.POS_TERMINAL, PaymentMethod.CARD_TERMINAL}
        if self.method not in allowed:
            raise ValueError(f"Offline payment method must be one of: {allowed}")
        return self


class VerifyOfflinePaymentRequest(BaseModel):
    """Cashier payload to confirm cash receipt or physical POS slip."""

    notes: str | None = Field(
        default=None,
        max_length=255,
        description="Optional cashier notes or terminal receipt number",
    )


class PaymentResponse(BaseModel):
    """Immutable ledger representation of a payment transaction."""

    id: uuid.UUID
    order_id: uuid.UUID
    amount: Decimal
    currency: str
    payment_method: PaymentMethod
    status: PaymentStatus
    transaction_reference: str | None = None
    idempotency_key: str | None = None
    verified_by_user_id: uuid.UUID | None = None
    verified_at: datetime.datetime | None = None
    created_at: datetime.datetime

    model_config = ConfigDict(from_attributes=True)


class PaymentSettlementResponse(BaseModel):
    """Consolidated settlement response detailing payment and order closure."""

    payment: PaymentResponse
    order_id: uuid.UUID
    order_status: OrderStatus
    is_paid: bool
    remaining_balance: Decimal
    table_status: TableStatus
