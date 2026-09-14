"""Guard model group mutations without rewriting stored Run history."""

from alembic import op

revision = "0005_model_group_review"
down_revision = "0004_model_groups"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for action in ("INSERT", "UPDATE"):
        op.execute(f"""CREATE TRIGGER model_group_member_guard_{action.lower()}
            BEFORE {action} ON model_group_members
            WHEN NEW.member_index < 0 OR NEW.revision < 1 OR length(trim(NEW.model_id)) = 0
              OR NOT (
                (NEW.harness_profile_id IS NOT NULL AND NEW.provider_connection_id IS NULL
                  AND (SELECT kind FROM model_groups WHERE id=NEW.group_id)='agent')
                OR (NEW.harness_profile_id IS NULL AND NEW.provider_connection_id IS NOT NULL
                  AND (SELECT kind FROM model_groups WHERE id=NEW.group_id)='llm')
              )
              OR EXISTS (SELECT 1 FROM model_group_members m
                WHERE m.group_id=NEW.group_id AND m.id != NEW.id AND m.model_id=NEW.model_id
                  AND m.harness_profile_id IS NEW.harness_profile_id
                  AND m.provider_connection_id IS NEW.provider_connection_id)
            BEGIN SELECT RAISE(ABORT, 'invalid model group member'); END""")
    op.execute("""CREATE TRIGGER model_group_kind_immutable BEFORE UPDATE OF kind ON model_groups
        WHEN NEW.kind != OLD.kind
        BEGIN SELECT RAISE(ABORT, 'model group kind is immutable'); END""")


def downgrade() -> None:
    op.execute("DROP TRIGGER model_group_kind_immutable")
    for action in ("insert", "update"):
        op.execute(f"DROP TRIGGER model_group_member_guard_{action}")
