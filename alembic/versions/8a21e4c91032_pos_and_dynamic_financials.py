"""pos_and_dynamic_financials

Revision ID: 8a21e4c91032
Revises: 7024806250f4
Create Date: 2026-09-18 17:45:00.000000+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "8a21e4c91032"
down_revision: Union[str, None] = "7024806250f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Add dynamic financial configuration to branches
    op.add_column("branches", sa.Column("tax_rate", sa.Numeric(5, 4), nullable=False, server_default=sa.text("'0.0000'")))
    op.add_column("branches", sa.Column("service_fee_rate", sa.Numeric(5, 4), nullable=False, server_default=sa.text("'0.0000'")))
    op.add_column("branches", sa.Column("is_service_taxable", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.add_column("branches", sa.Column("is_tax_inclusive", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.add_column("branches", sa.Column("service_fee_dine_in_only", sa.Boolean(), nullable=False, server_default=sa.text("true")))

    # 2. Create order_source enum
    order_source_enum = postgresql.ENUM("QR_CUSTOMER", "CASHIER_POS", "TAKE_A_WAY_APP", name="order_source")
    order_source_enum.create(op.get_bind(), checkfirst=True)

    # 3. Update orders table
    op.alter_column("orders", "table_id", existing_type=postgresql.UUID(as_uuid=True), nullable=True)
    op.add_column("orders", sa.Column("order_source", sa.Enum("QR_CUSTOMER", "CASHIER_POS", "TAKE_A_WAY_APP", name="order_source", native_enum=True), nullable=False, server_default="QR_CUSTOMER"))
    op.add_column("orders", sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True))
    op.add_column("orders", sa.Column("pickup_number", sa.SmallInteger(), nullable=True))
    op.add_column("orders", sa.Column("service_fee_rate", sa.Numeric(5, 4), nullable=False, server_default=sa.text("'0.0000'")))
    op.add_column("orders", sa.Column("service_fee_total", sa.Numeric(10, 2), nullable=False, server_default=sa.text("'0.00'")))
    op.add_column("orders", sa.Column("applied_tax_rate", sa.Numeric(5, 4), nullable=False, server_default=sa.text("'0.0000'")))
    op.add_column("orders", sa.Column("cancellation_reason", sa.Text(), nullable=True))

    op.create_index("ix_orders_created_by_user_id", "orders", ["created_by_user_id"])
    op.create_index("ix_orders_pickup_number", "orders", ["pickup_number"])


def downgrade() -> None:
    op.drop_index("ix_orders_pickup_number", table_name="orders")
    op.drop_index("ix_orders_created_by_user_id", table_name="orders")

    op.drop_column("orders", "cancellation_reason")
    op.drop_column("orders", "applied_tax_rate")
    op.drop_column("orders", "service_fee_total")
    op.drop_column("orders", "service_fee_rate")
    op.drop_column("orders", "pickup_number")
    op.drop_column("orders", "created_by_user_id")
    op.drop_column("orders", "order_source")
    op.alter_column("orders", "table_id", existing_type=postgresql.UUID(as_uuid=True), nullable=False)

    order_source_enum = postgresql.ENUM("QR_CUSTOMER", "CASHIER_POS", "TAKE_A_WAY_APP", name="order_source")
    order_source_enum.drop(op.get_bind(), checkfirst=True)

    op.drop_column("branches", "service_fee_dine_in_only")
    op.drop_column("branches", "is_tax_inclusive")
    op.drop_column("branches", "is_service_taxable")
    op.drop_column("branches", "service_fee_rate")
    op.drop_column("branches", "tax_rate")
