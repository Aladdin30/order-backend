"""SQLAlchemy domain models and metadata for multi-tenant smart restaurant platform."""

from app.models.audit import AuditLog
from app.models.auth import Branch, Table, Tenant, User, UserBranchAccess
from app.models.base import Base, LocalizedText, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.catalog import Category, Item, ModifierGroup, ModifierOption
from app.models.enums import (
    KitchenStation,
    OrderStatus,
    OrderType,
    PaymentMethod,
    PaymentStatus,
    ServiceRequestStatus,
    ServiceRequestType,
    TableStatus,
    UserRole,
)
from app.models.order import Order, OrderItem, Payment
from app.models.service import Review, ServiceRequest

__all__ = [
    # Base & Mixins
    "Base",
    "LocalizedText",
    "TimestampMixin",
    "UUIDPrimaryKeyMixin",
    # Enums
    "UserRole",
    "TableStatus",
    "KitchenStation",
    "OrderStatus",
    "OrderType",
    "ServiceRequestType",
    "ServiceRequestStatus",
    "PaymentMethod",
    "PaymentStatus",
    # Auth & Tenancy
    "Tenant",
    "Branch",
    "User",
    "UserBranchAccess",
    "Table",
    # Catalog & Menu
    "Category",
    "Item",
    "ModifierGroup",
    "ModifierOption",
    # Orders & Financial Ledgers
    "Order",
    "OrderItem",
    "Payment",
    # Service & Feedback
    "ServiceRequest",
    "Review",
    # Audit & Compliance
    "AuditLog",
]
