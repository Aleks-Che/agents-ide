"""Persist sanitized native model metadata and auth/config change fingerprint."""

import sqlalchemy as sa
from alembic import op

revision = "0018_native_catalog"
down_revision = "0017_operations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("harness_profiles", sa.Column("catalog_fingerprint", sa.String(64)))
    op.add_column(
        "harness_profiles",
        sa.Column("catalog_metadata_json", sa.Text(), nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    op.drop_column("harness_profiles", "catalog_metadata_json")
    op.drop_column("harness_profiles", "catalog_fingerprint")
