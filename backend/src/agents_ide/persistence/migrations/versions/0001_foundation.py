"""Authentication and service heartbeat; domain tables arrive in stage 2."""

import sqlalchemy as sa
from alembic import op

revision = "0001_foundation"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pairing",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.Float, nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False),
        sa.Column("next_attempt_at", sa.Float, nullable=False),
        sa.CheckConstraint("id = 1"),
    )
    op.create_table(
        "auth_sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("csrf_token", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.Float, nullable=False),
        sa.Column("revoked", sa.Boolean, nullable=False),
    )
    op.create_table(
        "worker_heartbeat",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("worker_id", sa.String(32), nullable=False),
        sa.Column("pid", sa.Integer, nullable=False),
        sa.Column("started_at", sa.Float, nullable=False),
        sa.Column("last_seen_at", sa.Float, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.CheckConstraint("id = 1"),
    )


def downgrade() -> None:
    op.drop_table("worker_heartbeat")
    op.drop_table("auth_sessions")
    op.drop_table("pairing")
