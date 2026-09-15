"""Payment gateway abstraction, intent generation, and cryptographic webhook verification."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import uuid
from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, status

from app.core.config import settings

logger = logging.getLogger(__name__)


class PaymentGateway(ABC):
    """Abstract interface for third-party online payment gateways."""

    @abstractmethod
    def create_payment_intent(
        self,
        amount: Decimal,
        currency: str,
        transaction_reference: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Generate client secret and payment token/checkout URL for online checkout."""
        pass

    @abstractmethod
    def verify_webhook_signature(
        self,
        raw_payload: bytes,
        signature_header: str | None,
    ) -> dict[str, Any]:
        """Cryptographically verify webhook authenticity and extract transaction result."""
        pass


class StripeGateway(PaymentGateway):
    """Stripe payment processing and webhook signature verification."""

    def __init__(
        self,
        secret_key: str | None = None,
        webhook_secret: str | None = None,
    ):
        self.secret_key = secret_key or settings.STRIPE_SECRET_KEY
        self.webhook_secret = webhook_secret or settings.STRIPE_WEBHOOK_SECRET

    def create_payment_intent(
        self,
        amount: Decimal,
        currency: str,
        transaction_reference: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        client_secret = f"pi_mock_{uuid.uuid4().hex[:20]}_secret_{uuid.uuid4().hex[:16]}"
        checkout_url = f"https://checkout.stripe.com/c/pay/{client_secret}"
        return {
            "client_secret": client_secret,
            "checkout_url": checkout_url,
            "transaction_reference": transaction_reference,
            "provider": "stripe",
        }

    def verify_webhook_signature(
        self,
        raw_payload: bytes,
        signature_header: str | None,
    ) -> dict[str, Any]:
        if not signature_header:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Missing Stripe-Signature header",
            )

        # Parse Stripe signature format: t=<timestamp>,v1=<signature>
        parsed_sig: dict[str, str] = {}
        for item in signature_header.split(","):
            if "=" in item:
                k, v = item.strip().split("=", 1)
                parsed_sig[k] = v

        timestamp = parsed_sig.get("t")
        expected_sig = parsed_sig.get("v1")

        if not timestamp or not expected_sig:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid Stripe signature header format",
            )

        try:
            ts_int = int(timestamp)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid Stripe webhook timestamp",
            )

        # Enforce 300-second timestamp freshness tolerance
        if abs(time.time() - ts_int) > 300:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Webhook signature timestamp expired",
            )

        signed_payload = f"{timestamp}.".encode("utf-8") + raw_payload
        computed_sig = hmac.new(
            self.webhook_secret.encode("utf-8"),
            signed_payload,
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(computed_sig, expected_sig):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid Stripe webhook cryptographic signature",
            )

        try:
            event = json.loads(raw_payload.decode("utf-8"))
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Malformed JSON in webhook payload",
            ) from exc

        return event


class LocalHMACGateway(PaymentGateway):
    """Local / Mock payment gateway utilizing standard HMAC-SHA256 signatures."""

    def __init__(self, webhook_secret: str | None = None):
        self.webhook_secret = webhook_secret or settings.LOCAL_PAYMENT_WEBHOOK_SECRET

    def create_payment_intent(
        self,
        amount: Decimal,
        currency: str,
        transaction_reference: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        client_secret = f"mock_sec_{uuid.uuid4().hex}"
        checkout_url = f"https://pay.local.test/checkout/{transaction_reference}"
        return {
            "client_secret": client_secret,
            "checkout_url": checkout_url,
            "transaction_reference": transaction_reference,
            "provider": "local",
        }

    def verify_webhook_signature(
        self,
        raw_payload: bytes,
        signature_header: str | None,
    ) -> dict[str, Any]:
        if not signature_header:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Missing webhook signature header",
            )

        computed = hmac.new(
            self.webhook_secret.encode("utf-8"),
            raw_payload,
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(computed, signature_header.strip()):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid webhook cryptographic signature",
            )

        try:
            event = json.loads(raw_payload.decode("utf-8"))
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Malformed JSON in webhook payload",
            ) from exc

        return event


def get_payment_gateway(provider: str) -> PaymentGateway:
    """Resolve payment gateway adapter instance by provider name."""
    clean_provider = provider.lower().strip()
    if clean_provider in ("stripe", "stripe_mock"):
        return StripeGateway()
    elif clean_provider in ("local", "mock", "local_wallet"):
        return LocalHMACGateway()
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported payment gateway provider: '{provider}'",
        )
