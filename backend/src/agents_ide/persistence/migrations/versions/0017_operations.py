"""Retention metadata and bounded storage accounting; executable snapshots stay immutable."""

import sqlalchemy as sa
from alembic import op

revision = "0017_operations"
down_revision = "0016_council_single_member"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for column in (
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("retention_sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("detailed_event_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("artifact_bytes", sa.BigInteger(), nullable=False, server_default="0"),
    ):
        op.add_column("runs", column)
    op.add_column("artifact_manifests", sa.Column("tombstoned_at", sa.Float()))
    op.add_column("artifact_manifests", sa.Column("purged_at", sa.Float()))
    op.create_index("ix_artifacts_run", "artifact_manifests", ["run_id"])
    op.create_index("ix_events_run_type_sequence", "run_events", ["run_id", "type", "sequence"])
    op.execute(
        "UPDATE runs SET artifact_bytes=COALESCE((SELECT SUM(byte_length) "
        "FROM artifact_manifests WHERE run_id=runs.id AND body_json IS NOT NULL),0), "
        "detailed_event_count=(SELECT COUNT(*) FROM run_events WHERE run_id=runs.id "
        "AND type IN ('agent.message_delta','attempt.text_delta','attempt.progress'))"
    )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.exec_driver_sql(
        "SELECT 1 FROM runs WHERE pinned=1 OR retention_sequence>0 UNION ALL "
        "SELECT 1 FROM artifact_manifests WHERE tombstoned_at IS NOT NULL "
        "OR purged_at IS NOT NULL LIMIT 1"
    ).first():
        raise RuntimeError("Restore a compatible backup instead of dropping retention metadata")
    op.drop_index("ix_events_run_type_sequence", table_name="run_events")
    op.drop_index("ix_artifacts_run", table_name="artifact_manifests")
    for name in ("tombstoned_at", "purged_at"):
        op.drop_column("artifact_manifests", name)
    for name in ("pinned", "retention_sequence", "detailed_event_count", "artifact_bytes"):
        op.drop_column("runs", name)
