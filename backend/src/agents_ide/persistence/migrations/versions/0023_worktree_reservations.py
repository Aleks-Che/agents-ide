"""Allow distinct managed worktrees to reserve the same source repository."""

from alembic import op

revision = "0023_worktree_reservations"
down_revision = "0022_general_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
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


def downgrade() -> None:
    op.execute("DROP TRIGGER reservation_identity_unique")
    op.execute("""CREATE TRIGGER reservation_identity_unique BEFORE INSERT ON workspace_reservations
        WHEN NEW.released_at IS NULL AND EXISTS (
            SELECT 1 FROM workspace_reservations WHERE released_at IS NULL
            AND workspace_identity_dev = NEW.workspace_identity_dev
            AND workspace_identity_ino = NEW.workspace_identity_ino)
        BEGIN SELECT RAISE(ABORT, 'workspace reserved'); END""")
