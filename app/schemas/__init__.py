"""Pydantic request and response schemas."""

from app.schemas.audit import AuditLogListResponse, AuditLogResponse
from app.schemas.auth import LoginRequest, TokenPayload, TokenResponse, UserResponse
from app.schemas.i18n import LocalizedField, LocalizedStr, OptionalLocalizedStr
from app.schemas.qr import (
    QRGenerateTokenRequest,
    QRGenerateTokenResponse,
    QRTokenPayload,
    QRVerificationResponse,
    QRVerifyRequest,
)
from app.schemas.menu import (
    MenuCategoryResponse,
    MenuItemResponse,
    MenuTreeResponse,
    ModifierGroupResponse,
    ModifierOptionResponse,
    SelectedModifierGroupInput,
    SelectedModifierOptionSnapshot,
    ValidateItemSelectionRequest,
    ValidatedItemSelectionResponse,
)
from app.schemas.order import (
    CheckoutItemInput,
    CheckoutRequest,
    OrderItemResponse,
    OrderResponse,
    OrderTransitionRequest,
    OrderTransitionResponse,
)
from app.schemas.payment import (
    InitiateOnlinePaymentRequest,
    InitiateOnlinePaymentResponse,
    OfflinePaymentRequest,
    PaymentResponse,
    PaymentSettlementResponse,
    VerifyOfflinePaymentRequest,
)
from app.schemas.service_request import (
    CreateServiceRequest,
    ServiceRequestResponse,
    UpdateServiceRequestStatus,
)
from app.schemas.session import (
    GuestSessionContext,
    TablePresenceVerifyRequest,
    TableSessionResponse,
)

__all__ = [
    "LoginRequest",
    "TokenResponse",
    "TokenPayload",
    "UserResponse",
    "AuditLogResponse",
    "AuditLogListResponse",
    "QRTokenPayload",
    "QRVerifyRequest",
    "QRVerificationResponse",
    "QRGenerateTokenRequest",
    "QRGenerateTokenResponse",
    "LocalizedField",
    "LocalizedStr",
    "OptionalLocalizedStr",
    "TablePresenceVerifyRequest",
    "TableSessionResponse",
    "GuestSessionContext",
    "ModifierOptionResponse",
    "ModifierGroupResponse",
    "MenuItemResponse",
    "MenuCategoryResponse",
    "MenuTreeResponse",
    "SelectedModifierGroupInput",
    "ValidateItemSelectionRequest",
    "SelectedModifierOptionSnapshot",
    "ValidatedItemSelectionResponse",
    "CheckoutItemInput",
    "CheckoutRequest",
    "OrderItemResponse",
    "OrderResponse",
    "OrderTransitionRequest",
    "OrderTransitionResponse",
    "CreateServiceRequest",
    "UpdateServiceRequestStatus",
    "ServiceRequestResponse",
    "InitiateOnlinePaymentRequest",
    "InitiateOnlinePaymentResponse",
    "OfflinePaymentRequest",
    "VerifyOfflinePaymentRequest",
    "PaymentResponse",
    "PaymentSettlementResponse",
]

