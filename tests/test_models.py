"""Comprehensive automated test suite for multi-tenant data modeling."""

import uuid
from decimal import Decimal

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import (
    CheckConstraint,
    Numeric,
    SmallInteger,
    UniqueConstraint,
    inspect,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import configure_mappers
from sqlalchemy.schema import CreateTable

from app.models import (
    Base,
    Branch,
    Category,
    Item,
    KitchenStation,
    LocalizedText,
    ModifierGroup,
    ModifierOption,
    Order,
    OrderItem,
    OrderStatus,
    OrderType,
    Payment,
    PaymentMethod,
    PaymentStatus,
    Review,
    ServiceRequest,
    ServiceRequestStatus,
    ServiceRequestType,
    Table,
    TableStatus,
    Tenant,
    User,
    UserBranchAccess,
    UserRole,
)


def test_mapper_compilation():
    """Verify that all SQLAlchemy 2.0 mappers configure without circular import or typing errors."""
    configure_mappers()
    assert len(Base.metadata.tables) == 16


def test_table_registration():
    """Verify all 16 domain and audit tables are properly registered in Base.metadata."""
    expected_tables = {
        "tenants",
        "branches",
        "users",
        "user_branch_access",
        "tables",
        "kitchen_stations",
        "categories",
        "items",
        "modifier_groups",
        "modifier_options",
        "orders",
        "order_items",
        "payments",
        "service_requests",
        "reviews",
        "audit_logs",
    }
    actual_tables = set(Base.metadata.tables.keys())
    assert expected_tables == actual_tables


def test_primary_keys_are_uuid():
    """Verify that all entities use native PostgreSQL UUID as primary key."""
    for table_name, table in Base.metadata.tables.items():
        pk_cols = list(table.primary_key.columns)
        assert len(pk_cols) == 1, f"Table {table_name} must have exactly one PK column"
        pk_col = pk_cols[0]
        assert pk_col.name == "id", f"Table {table_name} PK column must be named 'id'"
        assert isinstance(pk_col.type, UUID), f"Table {table_name} PK must be UUID type"
        assert pk_col.type.as_uuid is True, f"Table {table_name} PK UUID must have as_uuid=True"


def test_timestamp_mixin():
    """Verify all tables inherit created_at and domain entities inherit updated_at with timezone support."""
    for table_name, table in Base.metadata.tables.items():
        assert "created_at" in table.c, f"Table {table_name} missing created_at"
        assert table.c.created_at.type.timezone is True
        assert table.c.created_at.server_default is not None

        # audit_logs is an append-only ledger without mutation/updated_at
        if table_name != "audit_logs":
            assert "updated_at" in table.c, f"Table {table_name} missing updated_at"
            assert table.c.updated_at.type.timezone is True
            assert table.c.updated_at.server_default is not None


def test_geofence_attributes():
    """Verify Branch spatial attributes (latitude, longitude Numeric(10, 7) and geofence radius)."""
    branch_table = Base.metadata.tables["branches"]
    lat_col = branch_table.c.latitude
    lng_col = branch_table.c.longitude
    radius_col = branch_table.c.geofence_radius_meters

    assert isinstance(lat_col.type, Numeric)
    assert lat_col.type.precision == 10
    assert lat_col.type.scale == 7

    assert isinstance(lng_col.type, Numeric)
    assert lng_col.type.precision == 10
    assert lng_col.type.scale == 7

    assert isinstance(radius_col.type, SmallInteger)
    assert radius_col.default.arg == 150


def test_multilingual_jsonb_columns():
    """Verify customer-facing text and badge fields are native PostgreSQL JSONB."""
    jsonb_fields = [
        ("branches", "name"),
        ("categories", "name"),
        ("items", "name"),
        ("items", "description"),
        ("items", "allergens"),
        ("items", "dietary_badges"),
        ("modifier_groups", "name"),
        ("modifier_options", "name"),
        ("order_items", "selected_modifiers"),
    ]

    for table_name, col_name in jsonb_fields:
        table = Base.metadata.tables[table_name]
        col = table.c[col_name]
        assert isinstance(col.type, JSONB), f"{table_name}.{col_name} must be JSONB"


def test_financial_ledger_integrity_rules():
    """Verify immutable audit trail: RESTRICT on delete for operational ledger FKs."""
    orders_table = Base.metadata.tables["orders"]
    order_items_table = Base.metadata.tables["order_items"]
    payments_table = Base.metadata.tables["payments"]

    # Order FKs
    order_tenant_fk = next(fk for fk in orders_table.foreign_keys if fk.column.table.name == "tenants")
    assert order_tenant_fk.ondelete == "RESTRICT"

    order_branch_fk = next(fk for fk in orders_table.foreign_keys if fk.column.table.name == "branches")
    assert order_branch_fk.ondelete == "RESTRICT"

    order_table_fk = next(fk for fk in orders_table.foreign_keys if fk.column.table.name == "tables")
    assert order_table_fk.ondelete == "RESTRICT"

    # OrderItem FKs: item_id is RESTRICT (preserves historical order integrity)
    item_fk = next(fk for fk in order_items_table.foreign_keys if fk.column.table.name == "items")
    assert item_fk.ondelete == "RESTRICT"

    # OrderItem order_id is CASCADE (order owns its items)
    order_fk = next(fk for fk in order_items_table.foreign_keys if fk.column.table.name == "orders")
    assert order_fk.ondelete == "CASCADE"

    # Payment FK: order_id is RESTRICT
    payment_order_fk = next(fk for fk in payments_table.foreign_keys if fk.column.table.name == "orders")
    assert payment_order_fk.ondelete == "RESTRICT"

    # Payment verified_by_user_id is SET NULL
    payment_user_fk = next(fk for fk in payments_table.foreign_keys if fk.column.table.name == "users")
    assert payment_user_fk.ondelete == "SET NULL"


def test_metadata_configuration_cascades():
    """Verify configuration/catalog entities cascade delete cleanly."""
    branches_table = Base.metadata.tables["branches"]
    users_table = Base.metadata.tables["users"]
    user_branch_access_table = Base.metadata.tables["user_branch_access"]
    tables_table = Base.metadata.tables["tables"]
    categories_table = Base.metadata.tables["categories"]
    items_table = Base.metadata.tables["items"]
    mod_groups_table = Base.metadata.tables["modifier_groups"]
    mod_options_table = Base.metadata.tables["modifier_options"]

    assert next(fk for fk in branches_table.foreign_keys if fk.column.table.name == "tenants").ondelete == "CASCADE"
    assert next(fk for fk in users_table.foreign_keys if fk.column.table.name == "tenants").ondelete == "CASCADE"
    assert next(fk for fk in user_branch_access_table.foreign_keys if fk.column.table.name == "users").ondelete == "CASCADE"
    assert next(fk for fk in user_branch_access_table.foreign_keys if fk.column.table.name == "branches").ondelete == "CASCADE"
    assert next(fk for fk in tables_table.foreign_keys if fk.column.table.name == "branches").ondelete == "CASCADE"
    assert next(fk for fk in categories_table.foreign_keys if fk.column.table.name == "branches").ondelete == "CASCADE"
    assert next(fk for fk in items_table.foreign_keys if fk.column.table.name == "categories").ondelete == "CASCADE"
    assert next(fk for fk in mod_groups_table.foreign_keys if fk.column.table.name == "items").ondelete == "CASCADE"
    assert next(fk for fk in mod_options_table.foreign_keys if fk.column.table.name == "modifier_groups").ondelete == "CASCADE"


def test_unique_constraints():
    """Verify unique constraints for multi-branch access, table numbers, tenant slug, review order."""
    # UserBranchAccess unique (user_id, branch_id)
    uba_table = Base.metadata.tables["user_branch_access"]
    uba_uqs = [c for c in uba_table.constraints if isinstance(c, UniqueConstraint)]
    assert any(set(col.name for col in uq.columns) == {"user_id", "branch_id"} for uq in uba_uqs)

    # Table unique (branch_id, table_number)
    tables_table = Base.metadata.tables["tables"]
    tables_uqs = [c for c in tables_table.constraints if isinstance(c, UniqueConstraint)]
    assert any(set(col.name for col in uq.columns) == {"branch_id", "table_number"} for uq in tables_uqs)

    # Review unique order_id
    reviews_table = Base.metadata.tables["reviews"]
    order_id_col = reviews_table.c.order_id
    assert order_id_col.unique is True

    # Tenant slug unique
    tenants_table = Base.metadata.tables["tenants"]
    assert tenants_table.c.slug.unique is True


def test_composite_indexes():
    """Verify required operational and reporting composite indexes."""
    # Branch (tenant_id, slug)
    branch_table = Base.metadata.tables["branches"]
    branch_indices = [idx for idx in branch_table.indexes if set(c.name for c in idx.columns) == {"tenant_id", "slug"}]
    assert len(branch_indices) >= 1

    # Order (branch_id, status) and (branch_id, created_at)
    orders_table = Base.metadata.tables["orders"]
    order_branch_status = [
        idx for idx in orders_table.indexes
        if [c.name for c in idx.columns] == ["branch_id", "status"]
    ]
    assert len(order_branch_status) >= 1

    order_branch_created = [
        idx for idx in orders_table.indexes
        if [c.name for c in idx.columns] == ["branch_id", "created_at"]
    ]
    assert len(order_branch_created) >= 1

    # ServiceRequest (branch_id, status)
    sr_table = Base.metadata.tables["service_requests"]
    sr_indices = [idx for idx in sr_table.indexes if [c.name for c in idx.columns] == ["branch_id", "status"]]
    assert len(sr_indices) >= 1

    # Review (branch_id, rating)
    reviews_table = Base.metadata.tables["reviews"]
    rev_indices = [idx for idx in reviews_table.indexes if [c.name for c in idx.columns] == ["branch_id", "rating"]]
    assert len(rev_indices) >= 1


def test_check_constraints():
    """Verify rating 1..5 check constraint on reviews."""
    reviews_table = Base.metadata.tables["reviews"]
    check_constraints = [c for c in reviews_table.constraints if isinstance(c, CheckConstraint)]
    assert len(check_constraints) >= 1
    assert any("rating >= 1" in str(c.sqltext) and "rating <= 5" in str(c.sqltext) for c in check_constraints)


def test_domain_enums():
    """Verify all required enum types and values."""
    assert UserRole.SUPER_ADMIN == "SUPER_ADMIN"
    assert UserRole.REGIONAL_MANAGER == "REGIONAL_MANAGER"
    assert UserRole.BRANCH_ADMIN == "BRANCH_ADMIN"
    assert UserRole.CASHIER == "CASHIER"
    assert UserRole.WAITER == "WAITER"
    assert UserRole.KITCHEN_STAFF == "KITCHEN_STAFF"

    assert TableStatus.AVAILABLE == "AVAILABLE"
    assert TableStatus.BROWSING == "BROWSING"
    assert TableStatus.AWAITING_FOOD == "AWAITING_FOOD"
    assert TableStatus.EATING == "EATING"
    assert TableStatus.BILL_REQUESTED == "BILL_REQUESTED"
    assert TableStatus.NEEDS_CLEANING == "NEEDS_CLEANING"

    assert KitchenStation.HOT_KITCHEN == "HOT_KITCHEN"
    assert KitchenStation.COLD_KITCHEN == "COLD_KITCHEN"
    assert KitchenStation.BEVERAGE == "BEVERAGE"
    assert KitchenStation.DESSERT == "DESSERT"

    assert OrderStatus.DRAFT == "DRAFT"
    assert OrderStatus.PENDING_STAFF_CONFIRMATION == "PENDING_STAFF_CONFIRMATION"
    assert OrderStatus.SUBMITTED == "SUBMITTED"
    assert OrderStatus.PREPARING == "PREPARING"
    assert OrderStatus.READY == "READY"
    assert OrderStatus.DELIVERED == "DELIVERED"
    assert OrderStatus.CLOSED == "CLOSED"
    assert OrderStatus.CANCELLED == "CANCELLED"

    assert OrderType.DINE_IN == "DINE_IN"
    assert OrderType.TAKEAWAY == "TAKEAWAY"

    assert ServiceRequestType.WATER == "WATER"
    assert ServiceRequestType.CUTLERY == "CUTLERY"
    assert ServiceRequestType.NAPKINS == "NAPKINS"
    assert ServiceRequestType.PLATES == "PLATES"
    assert ServiceRequestType.WAITER_CALL == "WAITER_CALL"
    assert ServiceRequestType.PACK_LEFTOVERS == "PACK_LEFTOVERS"
    assert ServiceRequestType.BILL_REQUEST == "BILL_REQUEST"
    assert ServiceRequestType.TAKEAWAY_ORDER == "TAKEAWAY_ORDER"
    assert ServiceRequestType.OTHER == "OTHER"

    assert ServiceRequestStatus.PENDING == "PENDING"
    assert ServiceRequestStatus.ACKNOWLEDGED == "ACKNOWLEDGED"
    assert ServiceRequestStatus.COMPLETED == "COMPLETED"
    assert ServiceRequestStatus.DISMISSED == "DISMISSED"

    assert PaymentMethod.CASH == "CASH"
    assert PaymentMethod.CARD_TERMINAL == "CARD_TERMINAL"
    assert PaymentMethod.POS_TERMINAL == "POS_TERMINAL"
    assert PaymentMethod.STRIPE == "STRIPE"
    assert PaymentMethod.ONLINE_CARD == "ONLINE_CARD"
    assert PaymentMethod.APPLE_PAY == "APPLE_PAY"
    assert PaymentMethod.LOCAL_WALLET == "LOCAL_WALLET"

    assert PaymentStatus.PENDING == "PENDING"
    assert PaymentStatus.PENDING_CASHIER_VERIFICATION == "PENDING_CASHIER_VERIFICATION"
    assert PaymentStatus.COMPLETED == "COMPLETED"
    assert PaymentStatus.FAILED == "FAILED"
    assert PaymentStatus.REFUNDED == "REFUNDED"


def test_entity_relationships_in_memory():
    """Verify bidirectional relationship mapping, back_populates wiring, and JSONB payloads."""
    tenant = Tenant(
        name="Artisan Burgers Global",
        slug="artisan-burgers",
        is_active=True,
    )

    branch = Branch(
        tenant=tenant,
        name={"en": "Downtown Flagship", "ar": "فرع وسط المدينة"},
        slug="downtown-flagship",
        latitude=Decimal("24.7135517"),
        longitude=Decimal("46.6752957"),
        geofence_radius_meters=150,
        is_active=True,
    )

    regional_manager = User(
        tenant=tenant,
        email="regional@artisan.com",
        hashed_password="argon2_hashed_secret",
        full_name="Fatima Al-Mansoor",
        role=UserRole.REGIONAL_MANAGER,
        is_active=True,
    )

    # Explicit M:N Branch Scoping
    access = UserBranchAccess(
        user=regional_manager,
        branch=branch,
    )

    table = Table(
        branch=branch,
        table_number="T-04",
        capacity=4,
        status=TableStatus.AVAILABLE,
        current_session_token="sess_token_xyz123",
        is_active=True,
    )

    category = Category(
        branch=branch,
        name={"en": "Burgers", "ar": "برجر"},
        display_order=1,
        is_active=True,
    )

    item = Item(
        category=category,
        name={"en": "Smoky Wagyu Burger", "ar": "برجر واغيو مدخن"},
        description={
            "en": "Premium Wagyu beef patty with smoked gouda and truffle aioli",
            "ar": "شريحة لحم واغيو فاخرة مع جبن الغودا المدخن وصلصة الكمأة",
        },
        base_price=Decimal("18.50"),
        station=KitchenStation.HOT_KITCHEN,
        image_url="https://cdn.artisan.com/images/wagyu.jpg",
        is_available=True,
        allergens=["dairy", "gluten"],
        dietary_badges=["halal"],
    )

    mod_group = ModifierGroup(
        item=item,
        name={"en": "Cheese Choice", "ar": "اختيار الجبن"},
        min_choices=1,
        max_choices=2,
        is_required=True,
    )

    mod_option = ModifierOption(
        modifier_group=mod_group,
        name={"en": "Extra Aged Cheddar", "ar": "شيدر معتق إضافي"},
        price_delta=Decimal("2.00"),
        is_available=True,
    )

    order = Order(
        tenant=tenant,
        branch=branch,
        table=table,
        status=OrderStatus.SUBMITTED,
        order_type=OrderType.DINE_IN,
        subtotal=Decimal("20.50"),
        tax_total=Decimal("3.08"),
        total_amount=Decimal("23.58"),
        customer_notes="No pickles please",
    )

    # Modifier snapshot array preserving checkout time state
    selected_modifiers_snapshot = [
        {
            "modifier_group_id": str(mod_group.id),
            "modifier_group_name": mod_group.name,
            "modifier_option_id": str(mod_option.id),
            "modifier_option_name": mod_option.name,
            "price_delta": str(mod_option.price_delta),
        }
    ]

    order_item = OrderItem(
        order=order,
        item=item,
        quantity=1,
        unit_price=Decimal("18.50"),
        subtotal=Decimal("20.50"),
        station=KitchenStation.HOT_KITCHEN,
        is_bumped=False,
        selected_modifiers=selected_modifiers_snapshot,
        special_instructions="Well done",
    )

    payment = Payment(
        order=order,
        payment_method=PaymentMethod.APPLE_PAY,
        amount=Decimal("23.58"),
        currency="SAR",
        status=PaymentStatus.COMPLETED,
        transaction_reference="ch_apple_pay_987654",
        verified_by_user=regional_manager,
    )

    service_request = ServiceRequest(
        branch=branch,
        table=table,
        request_type=ServiceRequestType.WATER,
        status=ServiceRequestStatus.PENDING,
        escalated=False,
    )

    review = Review(
        branch=branch,
        order=order,
        rating=5,
        comment="Exquisite wagyu patty and rapid service!",
        escalated_to_manager=False,
    )

    # Verify bidirectional back_populates wiring
    assert branch in tenant.branches
    assert regional_manager in tenant.users
    assert order in tenant.orders
    assert table in branch.tables
    assert category in branch.categories
    assert order in branch.orders
    assert service_request in branch.service_requests
    assert review in branch.reviews
    assert access in branch.user_access
    assert access in regional_manager.branch_access
    assert item in category.items
    assert mod_group in item.modifier_groups
    assert mod_option in mod_group.options
    assert order_item in order.order_items
    assert payment in order.payments
    assert order.review == review
    assert order_item.item == item
    assert order_item.order == order
    assert review.order == order
    assert payment.verified_by_user == regional_manager
    assert payment in regional_manager.verified_payments
    assert service_request.table == table


def test_alembic_metadata_auto_detection():
    """Verify Alembic configuration and target_metadata detection."""
    config = Config("alembic.ini")
    script = ScriptDirectory.from_config(config)
    assert script is not None

    # Import target_metadata directly as done in env.py
    from app.models import Base as AppBase

    assert len(AppBase.metadata.tables) == 16
    assert "orders" in AppBase.metadata.tables
    assert "branches" in AppBase.metadata.tables
    assert "audit_logs" in AppBase.metadata.tables
    assert "kitchen_stations" in AppBase.metadata.tables


def test_postgresql_ddl_generation():
    """Verify that PostgreSQL DDL compilation succeeds for all 14 tables."""
    dialect = postgresql.dialect()
    for table_name, table in Base.metadata.tables.items():
        ddl = str(CreateTable(table).compile(dialect=dialect))
        assert "CREATE TABLE" in ddl
        assert table_name in ddl
