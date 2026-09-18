"""feat(hierarchy): add brand hierarchy and branch menu overrides

Revision ID: c395e71829a2
Revises: b284e5910401
Create Date: 2026-09-18 22:00:00.000000+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c395e71829a2"
down_revision: Union[str, None] = "b284e5910401"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create brands table
    op.create_table(
        "brands",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("slug", sa.String(120), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_brands_slug", "brands", ["slug"], unique=True)

    # 2. Update branches table
    op.add_column("branches", sa.Column("brand_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("brands.id", ondelete="CASCADE"), nullable=True))
    op.add_column("branches", sa.Column("currency", sa.String(3), nullable=False, server_default="EGP"))
    op.add_column("branches", sa.Column("timezone", sa.String(50), nullable=False, server_default="Africa/Cairo"))
    op.create_index("ix_branches_brand_id", "branches", ["brand_id"])

    # 3. Update users table
    op.add_column("users", sa.Column("brand_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("brands.id", ondelete="CASCADE"), nullable=True))
    op.create_index("ix_users_brand_id", "users", ["brand_id"])

    # 4. Update items table
    op.add_column("items", sa.Column("brand_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("brands.id", ondelete="CASCADE"), nullable=True))
    op.add_column("items", sa.Column("scope", sa.String(30), nullable=False, server_default="ALL_BRANCHES"))
    op.add_column("items", sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")))
    op.create_index("ix_items_brand_id", "items", ["brand_id"])
    op.create_index("ix_items_scope", "items", ["scope"])

    # 5. Create branch_menu_overrides table
    op.create_table(
        "branch_menu_overrides",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("branch_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("branches.id", ondelete="CASCADE"), nullable=False),
        sa.Column("menu_item_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("price_override", sa.Numeric(10, 2), nullable=True),
        sa.Column("is_available", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("is_visible", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("branch_id", "menu_item_id", name="uq_branch_menu_overrides"),
    )
    op.create_index("ix_branch_menu_overrides_branch_id", "branch_menu_overrides", ["branch_id"])
    op.create_index("ix_branch_menu_overrides_menu_item_id", "branch_menu_overrides", ["menu_item_id"])

    # 6. Operational hardening (brand_id foreign keys and indexes)
    op.add_column("orders", sa.Column("brand_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("brands.id", ondelete="RESTRICT"), nullable=True))
    op.create_index("ix_orders_brand_id", "orders", ["brand_id"])

    op.add_column("tables", sa.Column("brand_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("brands.id", ondelete="CASCADE"), nullable=True))
    op.create_index("ix_tables_brand_id", "tables", ["brand_id"])

    op.add_column("payments", sa.Column("brand_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("brands.id", ondelete="CASCADE"), nullable=True))
    op.create_index("ix_payments_brand_id", "payments", ["brand_id"])

    op.add_column("z_reports", sa.Column("brand_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_index("ix_z_reports_brand_id", "z_reports", ["brand_id"])


def downgrade() -> None:
    op.drop_index("ix_z_reports_brand_id", table_name="z_reports")
    op.drop_column("z_reports", "brand_id")

    op.drop_index("ix_payments_brand_id", table_name="payments")
    op.drop_column("payments", "brand_id")

    op.drop_index("ix_tables_brand_id", table_name="tables")
    op.drop_column("tables", "brand_id")

    op.drop_index("ix_orders_brand_id", table_name="orders")
    op.drop_column("orders", "brand_id")

    op.drop_table("branch_menu_overrides")

    op.drop_index("ix_items_scope", table_name="items")
    op.drop_index("ix_items_brand_id", table_name="items")
    op.drop_column("items", "is_active")
    op.drop_column("items", "scope")
    op.drop_column("items", "brand_id")

    op.drop_index("ix_users_brand_id", table_name="users")
    op.drop_column("users", "brand_id")

    op.drop_index("ix_branches_brand_id", table_name="branches")
    op.drop_column("branches", "timezone")
    op.drop_column("branches", "currency")
    op.drop_column("branches", "brand_id")

    op.drop_table("brands")
