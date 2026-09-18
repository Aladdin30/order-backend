"""Enumerations for multi-tenant smart restaurant platform."""

from enum import StrEnum


class UserRole(StrEnum):
    """User access hierarchy and role-based permissions."""

    SUPER_ADMIN = "SUPER_ADMIN"
    BRAND_ADMIN = "BRAND_ADMIN"
    REGIONAL_MANAGER = "REGIONAL_MANAGER"
    BRANCH_ADMIN = "BRANCH_ADMIN"
    CASHIER = "CASHIER"
    WAITER = "WAITER"
    KITCHEN_STAFF = "KITCHEN_STAFF"


class MenuItemScope(StrEnum):
    """Catalog item availability scope across brand branches."""

    ALL_BRANCHES = "ALL_BRANCHES"
    SPECIFIC_BRANCHES = "SPECIFIC_BRANCHES"



class TableStatus(StrEnum):
    """Real-time physical table operational lifecycle statuses."""

    AVAILABLE = "AVAILABLE"
    BROWSING = "BROWSING"
    AWAITING_FOOD = "AWAITING_FOOD"
    EATING = "EATING"
    BILL_REQUESTED = "BILL_REQUESTED"
    NEEDS_CLEANING = "NEEDS_CLEANING"


class KitchenStation(StrEnum):
    """Kitchen routing stations for order preparation."""

    HOT_KITCHEN = "HOT_KITCHEN"
    COLD_KITCHEN = "COLD_KITCHEN"
    BEVERAGE = "BEVERAGE"
    DESSERT = "DESSERT"


class OrderStatus(StrEnum):
    """Comprehensive lifecycle statuses of an order."""

    DRAFT = "DRAFT"
    PENDING_STAFF_CONFIRMATION = "PENDING_STAFF_CONFIRMATION"
    SUBMITTED = "SUBMITTED"
    PREPARING = "PREPARING"
    READY = "READY"
    DELIVERED = "DELIVERED"
    SERVED = "SERVED"
    PAID = "PAID"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"


class OrderType(StrEnum):
    """Order fulfillment channels."""

    DINE_IN = "DINE_IN"
    TAKEAWAY = "TAKEAWAY"


class OrderSource(StrEnum):
    """Channel/source origin of an order."""

    QR_CUSTOMER = "QR_CUSTOMER"
    CASHIER_POS = "CASHIER_POS"
    TAKE_A_WAY_APP = "TAKE_A_WAY_APP"


class ServiceRequestType(StrEnum):
    """Guest table-side assistance request categories."""

    WATER = "WATER"
    WATER_REFILL = "WATER_REFILL"
    CUTLERY = "CUTLERY"
    NAPKINS = "NAPKINS"
    PLATES = "PLATES"
    WAITER_CALL = "WAITER_CALL"
    PACK_LEFTOVERS = "PACK_LEFTOVERS"
    BILL_REQUEST = "BILL_REQUEST"
    TAKEAWAY_ORDER = "TAKEAWAY_ORDER"
    OTHER = "OTHER"


class ServiceRequestStatus(StrEnum):
    """Service request dispatch and completion statuses."""

    PENDING = "PENDING"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    COMPLETED = "COMPLETED"
    DISMISSED = "DISMISSED"


class PaymentMethod(StrEnum):
    """Accepted payment tender types."""

    CASH = "CASH"
    CARD_TERMINAL = "CARD_TERMINAL"
    POS_TERMINAL = "POS_TERMINAL"
    STRIPE = "STRIPE"
    ONLINE_CARD = "ONLINE_CARD"
    APPLE_PAY = "APPLE_PAY"
    LOCAL_WALLET = "LOCAL_WALLET"


class PaymentStatus(StrEnum):
    """Settlement status for transactions."""

    PENDING = "PENDING"
    PENDING_CASHIER_VERIFICATION = "PENDING_CASHIER_VERIFICATION"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    REFUNDED = "REFUNDED"


class DrawerStatus(StrEnum):
    """Operational status of a cash drawer shift."""

    OPEN = "OPEN"
    CLOSED = "CLOSED"

