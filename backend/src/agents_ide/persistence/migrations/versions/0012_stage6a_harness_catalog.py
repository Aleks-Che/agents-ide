"""Stage 6A: catalog and probe fields for HarnessProfile (OpenCode cache)."""

import sqlalchemy as sa
from alembic import op

revision = "0012_stage6a_harness_catalog"
down_revision = "0011_stage8_review"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "harness_profiles",
        sa.Column("catalog_models_json", sa.Text(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "harness_profiles",
        sa.Column("catalog_fetched_at", sa.Float(), nullable=True),
    )
    op.add_column(
        "harness_profiles",
        sa.Column("catalog_ttl_seconds", sa.Integer(), nullable=False, server_default="900"),
    )
    op.add_column(
        "harness_profiles",
        sa.Column("last_test_status", sa.String(16), nullable=True),
    )
    op.add_column(
        "harness_profiles",
        sa.Column("last_test_at", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("harness_profiles", "last_test_at")
    op.drop_column("harness_profiles", "last_test_status")
    op.drop_column("harness_profiles", "catalog_ttl_seconds")
    op.drop_column("harness_profiles", "catalog_fetched_at")
    op.drop_column("harness_profiles", "catalog_models_json")
