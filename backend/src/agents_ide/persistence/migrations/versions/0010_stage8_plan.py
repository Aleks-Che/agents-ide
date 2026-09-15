"""Stage 8: PlanItem scope and idempotent plan imports."""

import sqlalchemy as sa
from alembic import op

revision = "0010_stage8_plan"
down_revision = "0009_stage5_review"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "plan_items",
        sa.Column("scope", sa.String(64), nullable=False, server_default="__default__"),
    )
    op.add_column(
        "plan_items",
        sa.Column("created_at", sa.Float(), nullable=False, server_default="0"),
    )
    op.add_column(
        "plan_items",
        sa.Column("updated_at", sa.Float(), nullable=False, server_default="0"),
    )
    op.create_index("ix_plan_items_scope", "plan_items", ["run_id", "scope", "order_index"])


def downgrade() -> None:
    op.drop_index("ix_plan_items_scope", table_name="plan_items")
    op.drop_column("plan_items", "updated_at")
    op.drop_column("plan_items", "created_at")
    op.drop_column("plan_items", "scope")
