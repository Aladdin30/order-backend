"""financials_and_z_reports

Revision ID: b284e5910401
Revises: 8a21e4c91032
Create Date: 2026-09-18 20:00:00.000000+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b284e5910401"
down_revision: Union[str, None] = "8a21e4c91032"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create cash_drawer_sessions table
    op.create_table(
        "cash_drawer_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("branch_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("branches.id", ondelete="CASCADE"), nullable=False),
        sa.Column("opened_by_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("closed_by_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="OPEN"),
        sa.Column("opening_balance", sa.Numeric(10, 2), nullable=False, server_default="0.00"),
        sa.Column("declared_cash_amount", sa.Numeric(10, 2), nullable=True),
        sa.Column("calculated_cash_amount", sa.Numeric(10, 2), nullable=True),
        sa.Column("cash_variance", sa.Numeric(10, 2), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closing_notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_cash_drawer_sessions_branch_id", "cash_drawer_sessions", ["branch_id"])
    op.create_index("ix_cash_drawer_sessions_opened_by_user_id", "cash_drawer_sessions", ["opened_by_user_id"])
    op.create_index("ix_cash_drawer_sessions_closed_by_user_id", "cash_drawer_sessions", ["closed_by_user_id"])
    op.create_index("ix_cash_drawer_sessions_status", "cash_drawer_sessions", ["status"])

    # 2. Create z_reports table
    op.create_table(
        "z_reports",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("branch_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("branches.id", ondelete="CASCADE"), nullable=False),
        sa.Column("generated_by_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("drawer_session_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("cash_drawer_sessions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("report_number", sa.String(50), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("gross_sales", sa.Numeric(10, 2), nullable=False, server_default="0.00"),
        sa.Column("net_sales", sa.Numeric(10, 2), nullable=False, server_default="0.00"),
        sa.Column("total_tax", sa.Numeric(10, 2), nullable=False, server_default="0.00"),
        sa.Column("total_service_fees", sa.Numeric(10, 2), nullable=False, server_default="0.00"),
        sa.Column("total_discounts", sa.Numeric(10, 2), nullable=False, server_default="0.00"),
        sa.Column("total_refunds", sa.Numeric(10, 2), nullable=False, server_default="0.00"),
        sa.Column("cash_sales", sa.Numeric(10, 2), nullable=False, server_default="0.00"),
        sa.Column("card_pos_sales", sa.Numeric(10, 2), nullable=False, server_default="0.00"),
        sa.Column("online_sales", sa.Numeric(10, 2), nullable=False, server_default="0.00"),
        sa.Column("qr_customer_sales", sa.Numeric(10, 2), nullable=False, server_default="0.00"),
        sa.Column("cashier_pos_sales", sa.Numeric(10, 2), nullable=False, server_default="0.00"),
        sa.Column("takeaway_app_sales", sa.Numeric(10, 2), nullable=False, server_default="0.00"),
        sa.Column("total_orders", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("paid_orders", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("refunded_orders", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cancelled_orders", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("opening_balance", sa.Numeric(10, 2), nullable=True),
        sa.Column("declared_cash", sa.Numeric(10, 2), nullable=True),
        sa.Column("cash_variance", sa.Numeric(10, 2), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_z_reports_branch_id", "z_reports", ["branch_id"])
    op.create_index("ix_z_reports_generated_by_user_id", "z_reports", ["generated_by_user_id"])
    op.create_index("ix_z_reports_drawer_session_id", "z_reports", ["drawer_session_id"])
    op.create_index("ix_z_reports_report_number", "z_reports", ["report_number"], unique=True)
    op.create_index("ix_z_reports_business_date", "z_reports", ["business_date"])


def downgrade() -> None:
    op.drop_table("z_reports")
    op.drop_table("cash_drawer_sessions")
