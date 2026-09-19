"""A managed worktree does not reserve the source checkout's working files."""

from alembic import op

revision = "0026_independent_workspaces"
down_revision = "0025_single_template"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP TRIGGER reservation_identity_unique")
    op.execute("""CREATE TRIGGER reservation_identity_unique BEFORE INSERT ON workspace_reservations
        WHEN NEW.released_at IS NULL AND EXISTS (
            SELECT 1 FROM workspace_reservations WHERE released_at IS NULL
            AND workspace_identity_dev = NEW.workspace_identity_dev
            AND workspace_identity_ino = NEW.workspace_identity_ino
            AND (workspace_json IS NULL OR NEW.workspace_json IS NULL
                 OR (json_extract(workspace_json, '$.worktree_path') IS NULL
                     AND json_extract(NEW.workspace_json, '$.worktree_path') IS NULL)
                 OR coalesce(json_extract(workspace_json, '$.worktree_path'),
                             json_extract(workspace_json, '$.normalized_path')) =
                    coalesce(json_extract(NEW.workspace_json, '$.worktree_path'),
                             json_extract(NEW.workspace_json, '$.normalized_path'))))
        BEGIN SELECT RAISE(ABORT, 'workspace reserved'); END""")


def downgrade() -> None:
    op.execute("DROP TRIGGER reservation_identity_unique")
    op.execute("""CREATE TRIGGER reservation_identity_unique BEFORE INSERT ON workspace_reservations
        WHEN NEW.released_at IS NULL AND EXISTS (
            SELECT 1 FROM workspace_reservations WHERE released_at IS NULL
            AND workspace_identity_dev = NEW.workspace_identity_dev
            AND workspace_identity_ino = NEW.workspace_identity_ino
            AND (json_extract(workspace_json, '$.worktree_path') IS NULL
                 OR json_extract(NEW.workspace_json, '$.worktree_path') IS NULL
                 OR json_extract(workspace_json, '$.worktree_path') =
                    json_extract(NEW.workspace_json, '$.worktree_path')))
        BEGIN SELECT RAISE(ABORT, 'workspace reserved'); END""")
