"""Model groups and priority lists for stage 2A."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_model_groups"
down_revision = "0003_stage2_review"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Bindings gain an explicit model_selections_json column to record per-role
    # direct/group choices. Existing rows keep an empty dict; old model_overrides
    # remain untouched so pre-2A Runs still load.
    op.add_column(
        "pipeline_bindings",
        sa.Column(
            "model_selections_json",
            sa.Text,
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )

    op.create_table(
        "model_groups",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text, nullable=False, server_default=""),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("revision", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.Column("archived_at", sa.Float),
        sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
        sa.CheckConstraint("kind IN ('agent', 'llm')", name="ck_model_groups_kind"),
        sa.UniqueConstraint("kind", "name", name="uq_model_groups_kind_name"),
    )
    op.create_index("ix_model_groups_archived", "model_groups", ["archived_at"])

    op.create_table(
        "model_group_members",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("group_id", sa.String(32), nullable=False),
        sa.Column("member_index", sa.Integer, nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("1")),
        sa.Column("harness_profile_id", sa.String(32)),
        sa.Column("provider_connection_id", sa.String(32)),
        sa.Column("model_id", sa.String(256), nullable=False),
        sa.Column("params_json", sa.Text, nullable=False, server_default="{}"),
        sa.Column("revision", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["model_groups.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["harness_profile_id"], ["harness_profiles.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["provider_connection_id"],
            ["provider_connections.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("group_id", "member_index", name="uq_members_group_index"),
    )
    op.create_index("ix_members_group", "model_group_members", ["group_id"])
    op.execute(
        """CREATE TRIGGER model_group_member_kind_check BEFORE INSERT ON model_group_members
        WHEN NOT (
            (NEW.harness_profile_id IS NOT NULL AND NEW.provider_connection_id IS NULL)
            OR (NEW.harness_profile_id IS NULL AND NEW.provider_connection_id IS NOT NULL)
        ) BEGIN
            SELECT RAISE(ABORT, 'member must reference one profile or connection');
        END"""
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER model_group_member_kind_check")
    op.drop_index("ix_members_group", table_name="model_group_members")
    op.drop_table("model_group_members")
    op.drop_index("ix_model_groups_archived", table_name="model_groups")
    op.drop_table("model_groups")
    op.drop_column("pipeline_bindings", "model_selections_json")
