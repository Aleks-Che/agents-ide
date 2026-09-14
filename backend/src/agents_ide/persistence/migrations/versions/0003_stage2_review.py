"""Preserve stage 2 requests, drafts and reservation scope; enforce invariants."""

import sqlalchemy as sa
from alembic import op

revision = "0003_stage2_review"
down_revision = "0002_domain"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runs", sa.Column("active_intervals_json", sa.Text, nullable=False, server_default="[]")
    )
    op.add_column(
        "agent_sessions",
        sa.Column("capabilities_json", sa.Text, nullable=False, server_default="{}"),
    )
    op.add_column(
        "pipeline_templates", sa.Column("draft_json", sa.Text, nullable=False, server_default="{}")
    )
    for table in ("pipeline_versions", "pipeline_bindings"):
        op.add_column(
            table, sa.Column("settings_json", sa.Text, nullable=False, server_default="{}")
        )
    op.add_column("messages", sa.Column("archived_at", sa.Float))
    op.add_column("messages", sa.Column("version", sa.Integer, nullable=False, server_default="1"))
    op.add_column("runs", sa.Column("request_hash", sa.String(64)))
    op.add_column("runs", sa.Column("request_json", sa.Text))
    # NULL records pre-review commands whose original payload was not persisted.
    # It must not be invented or replayed by a future worker.
    op.add_column("command_journal", sa.Column("payload_json", sa.Text))
    op.add_column("workspace_reservations", sa.Column("workspace_json", sa.Text))
    # Retain old rows (including conflicting legacy reservations) for recovery.
    # Triggers constrain new writes without deleting or rewriting history.
    op.execute("""CREATE TRIGGER journal_sequence_unique BEFORE INSERT ON command_journal
        WHEN EXISTS (SELECT 1 FROM command_journal
                     WHERE run_id = NEW.run_id AND sequence = NEW.sequence)
        BEGIN SELECT RAISE(ABORT, 'command sequence conflict'); END""")
    op.execute("""CREATE TRIGGER reservation_identity_unique BEFORE INSERT ON workspace_reservations
        WHEN NEW.released_at IS NULL AND EXISTS (
            SELECT 1 FROM workspace_reservations WHERE released_at IS NULL
            AND workspace_identity_dev = NEW.workspace_identity_dev
            AND workspace_identity_ino = NEW.workspace_identity_ino)
        BEGIN SELECT RAISE(ABORT, 'workspace reserved'); END""")
    for action in ("INSERT", "UPDATE"):
        op.execute(f"""CREATE TRIGGER run_chat_{action.lower()} BEFORE {action} ON runs
            WHEN NEW.chat_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM chats WHERE id = NEW.chat_id AND project_id = NEW.project_id)
            BEGIN SELECT RAISE(ABORT, 'run chat project mismatch'); END""")
    op.execute("""CREATE TRIGGER pipeline_version_immutable BEFORE UPDATE ON pipeline_versions
        BEGIN SELECT RAISE(ABORT, 'pipeline version is immutable'); END""")
    op.execute("""CREATE TRIGGER run_snapshot_immutable BEFORE UPDATE OF
        request_hash, request_json, snapshot_json, resolved_settings_json, execution_hash,
        policy_hash, schema_version, pipeline_version_id, project_id, chat_id, binding_id,
        idempotency_key ON runs
        BEGIN SELECT RAISE(ABORT, 'run input is immutable'); END""")


def downgrade() -> None:
    op.drop_column("runs", "active_intervals_json")
    op.drop_column("agent_sessions", "capabilities_json")
    for name in (
        "run_snapshot_immutable",
        "pipeline_version_immutable",
        "run_chat_insert",
        "run_chat_update",
        "reservation_identity_unique",
        "journal_sequence_unique",
    ):
        op.execute(f"DROP TRIGGER {name}")
    for table, column in (
        ("workspace_reservations", "workspace_json"),
        ("command_journal", "payload_json"),
        ("runs", "request_json"),
        ("runs", "request_hash"),
        ("messages", "version"),
        ("messages", "archived_at"),
        ("pipeline_bindings", "settings_json"),
        ("pipeline_versions", "settings_json"),
        ("pipeline_templates", "draft_json"),
    ):
        op.drop_column(table, column)
