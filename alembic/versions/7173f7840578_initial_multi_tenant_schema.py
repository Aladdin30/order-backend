"""initial_multi_tenant_schema

Revision ID: 7173f7840578
Revises: 
Create Date: 2026-09-11 00:06:21.423933+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "7173f7840578"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Enum types
user_role_enum = postgresql.ENUM(
    "SUPER_ADMIN",
    "REGIONAL_MANAGER",
    "BRANCH_ADMIN",
    "CASHIER",
    "KITCHEN_STAFF",
    name="user_role",
)
table_status_enum = postgresql.ENUM(
    "AVAILABLE",
    "BROWSING",
    "AWAITING_FOOD",
    "EATING",
    "BILL_REQUESTED",
    "NEEDS_CLEANING",
    name="table_status",
)
kitchen_station_enum = postgresql.ENUM(
    "HOT_KITCHEN",
    "COLD_KITCHEN",
    "BEVERAGE",
    "DESSERT",
    name="kitchen_station",
)
order_status_enum = postgresql.ENUM(
    "DRAFT",
    "PENDING_STAFF_CONFIRMATION",
    "SUBMITTED",
    "PREPARING",
    "READY",
    "DELIVERED",
    "CLOSED",
    "CANCELLED",
    name="order_status",
)
order_type_enum = postgresql.ENUM(
    "DINE_IN",
    "TAKEAWAY",
    name="order_type",
)
service_request_type_enum = postgresql.ENUM(
    "WATER",
    "CUTLERY",
    "NAPKINS",
    "PLATES",
    "WAITER_CALL",
    "PACK_LEFTOVERS",
    "BILL_REQUEST",
    name="service_request_type",
)
service_request_status_enum = postgresql.ENUM(
    "PENDING",
    "ACKNOWLEDGED",
    "COMPLETED",
    "DISMISSED",
    name="service_request_status",
)
payment_method_enum = postgresql.ENUM(
    "CASH",
    "CARD_TERMINAL",
    "STRIPE",
    "APPLE_PAY",
    "LOCAL_WALLET",
    name="payment_method",
)
payment_status_enum = postgresql.ENUM(
    "PENDING",
    "COMPLETED",
    "FAILED",
    "REFUNDED",
    name="payment_status",
)


def upgrade() -> None:
    # 1. Create Enums
    bind = op.get_bind()
    user_role_enum.create(bind, checkfirst=True)
    table_status_enum.create(bind, checkfirst=True)
    kitchen_station_enum.create(bind, checkfirst=True)
    order_status_enum.create(bind, checkfirst=True)
    order_type_enum.create(bind, checkfirst=True)
    service_request_type_enum.create(bind, checkfirst=True)
    service_request_status_enum.create(bind, checkfirst=True)
    payment_method_enum.create(bind, checkfirst=True)
    payment_status_enum.create(bind, checkfirst=True)

    # 2. Tenants Table
    op.create_table(
        "tenants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False, unique=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_tenants_slug", "tenants", ["slug"])

    # 3. Branches Table
    op.create_table(
        "branches",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False),
        sa.Column("latitude", sa.Numeric(10, 7), nullable=False),
        sa.Column("longitude", sa.Numeric(10, 7), nullable=False),
        sa.Column("geofence_radius_meters", sa.SmallInteger(), nullable=False, server_default=sa.text("150")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_branches_tenant_id", "branches", ["tenant_id"])
    op.create_index("ix_branches_tenant_id_slug", "branches", ["tenant_id", "slug"], unique=True)

    # 4. Users Table
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("hashed_password", sa.String(255), nullable=False),
        sa.Column("full_name", sa.String(255), nullable=False),
        sa.Column("role", user_role_enum, nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_users_tenant_id", "users", ["tenant_id"])
    op.create_index("ix_users_email", "users", ["email"])

    # 5. User Branch Access Table
    op.create_table(
        "user_branch_access",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("branch_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("branches.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", "branch_id", name="uq_user_branch_access"),
    )
    op.create_index("ix_user_branch_access_user_id", "user_branch_access", ["user_id"])
    op.create_index("ix_user_branch_access_branch_id", "user_branch_access", ["branch_id"])

    # 6. Tables Table
    op.create_table(
        "tables",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("branch_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("branches.id", ondelete="CASCADE"), nullable=False),
        sa.Column("table_number", sa.String(50), nullable=False),
        sa.Column("capacity", sa.SmallInteger(), nullable=False),
        sa.Column("status", table_status_enum, nullable=False, server_default="AVAILABLE"),
        sa.Column("current_session_token", sa.String(255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("branch_id", "table_number", name="uq_tables_branch_table_number"),
    )
    op.create_index("ix_tables_branch_id", "tables", ["branch_id"])

    # 7. Categories Table
    op.create_table(
        "categories",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("branch_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("branches.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("display_order", sa.SmallInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_categories_branch_id", "categories", ["branch_id"])

    # 8. Items Table
    op.create_table(
        "items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("category_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("categories.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("description", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("base_price", sa.Numeric(10, 2), nullable=False),
        sa.Column("station", kitchen_station_enum, nullable=False),
        sa.Column("image_url", sa.String(1024), nullable=True),
        sa.Column("is_available", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("allergens", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("dietary_badges", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_items_category_id", "items", ["category_id"])

    # 9. Modifier Groups Table
    op.create_table(
        "modifier_groups",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("min_choices", sa.SmallInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("max_choices", sa.SmallInteger(), nullable=False, server_default=sa.text("1")),
        sa.Column("is_required", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_modifier_groups_item_id", "modifier_groups", ["item_id"])

    # 10. Modifier Options Table
    op.create_table(
        "modifier_options",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("modifier_group_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("modifier_groups.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("price_delta", sa.Numeric(10, 2), nullable=False, server_default=sa.text("0.00")),
        sa.Column("is_available", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_modifier_options_modifier_group_id", "modifier_options", ["modifier_group_id"])

    # 11. Orders Table
    op.create_table(
        "orders",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("branch_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("table_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tables.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("status", order_status_enum, nullable=False, server_default="DRAFT"),
        sa.Column("order_type", order_type_enum, nullable=False, server_default="DINE_IN"),
        sa.Column("subtotal", sa.Numeric(10, 2), nullable=False, server_default=sa.text("0.00")),
        sa.Column("tax_total", sa.Numeric(10, 2), nullable=False, server_default=sa.text("0.00")),
        sa.Column("total_amount", sa.Numeric(10, 2), nullable=False, server_default=sa.text("0.00")),
        sa.Column("customer_notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_orders_tenant_id", "orders", ["tenant_id"])
    op.create_index("ix_orders_branch_id", "orders", ["branch_id"])
    op.create_index("ix_orders_table_id", "orders", ["table_id"])
    op.create_index("ix_orders_branch_status", "orders", ["branch_id", "status"])
    op.create_index("ix_orders_branch_created_at", "orders", ["branch_id", "created_at"])

    # 12. Order Items Table
    op.create_table(
        "order_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("items.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("quantity", sa.SmallInteger(), nullable=False, server_default=sa.text("1")),
        sa.Column("unit_price", sa.Numeric(10, 2), nullable=False),
        sa.Column("subtotal", sa.Numeric(10, 2), nullable=False),
        sa.Column("station", kitchen_station_enum, nullable=False),
        sa.Column("is_bumped", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("selected_modifiers", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("special_instructions", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_order_items_order_id", "order_items", ["order_id"])
    op.create_index("ix_order_items_item_id", "order_items", ["item_id"])

    # 13. Payments Table
    op.create_table(
        "payments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("payment_method", payment_method_enum, nullable=False),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="SAR"),
        sa.Column("status", payment_status_enum, nullable=False, server_default="PENDING"),
        sa.Column("transaction_reference", sa.String(255), nullable=True),
        sa.Column("verified_by_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_payments_order_id", "payments", ["order_id"])
    op.create_index("ix_payments_verified_by_user_id", "payments", ["verified_by_user_id"])

    # 14. Service Requests Table
    op.create_table(
        "service_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("branch_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("branches.id", ondelete="CASCADE"), nullable=False),
        sa.Column("table_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tables.id", ondelete="CASCADE"), nullable=False),
        sa.Column("request_type", service_request_type_enum, nullable=False),
        sa.Column("status", service_request_status_enum, nullable=False, server_default="PENDING"),
        sa.Column("escalated", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_service_requests_branch_id", "service_requests", ["branch_id"])
    op.create_index("ix_service_requests_table_id", "service_requests", ["table_id"])
    op.create_index("ix_service_requests_branch_status", "service_requests", ["branch_id", "status"])

    # 15. Reviews Table
    op.create_table(
        "reviews",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("branch_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("branches.id", ondelete="CASCADE"), nullable=False),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("rating", sa.SmallInteger(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("escalated_to_manager", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("manager_resolution_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("rating >= 1 AND rating <= 5", name="ck_reviews_rating_range"),
    )
    op.create_index("ix_reviews_branch_id", "reviews", ["branch_id"])
    op.create_index("ix_reviews_order_id", "reviews", ["order_id"])
    op.create_index("ix_reviews_branch_rating", "reviews", ["branch_id", "rating"])


def downgrade() -> None:
    bind = op.get_bind()

    # Drop tables in reverse order of creation
    op.drop_table("reviews")
    op.drop_table("service_requests")
    op.drop_table("payments")
    op.drop_table("order_items")
    op.drop_table("orders")
    op.drop_table("modifier_options")
    op.drop_table("modifier_groups")
    op.drop_table("items")
    op.drop_table("categories")
    op.drop_table("tables")
    op.drop_table("user_branch_access")
    op.drop_table("users")
    op.drop_table("branches")
    op.drop_table("tenants")

    # Drop enums
    payment_status_enum.drop(bind, checkfirst=True)
    payment_method_enum.drop(bind, checkfirst=True)
    service_request_status_enum.drop(bind, checkfirst=True)
    service_request_type_enum.drop(bind, checkfirst=True)
    order_type_enum.drop(bind, checkfirst=True)
    order_status_enum.drop(bind, checkfirst=True)
    kitchen_station_enum.drop(bind, checkfirst=True)
    table_status_enum.drop(bind, checkfirst=True)
    user_role_enum.drop(bind, checkfirst=True)
