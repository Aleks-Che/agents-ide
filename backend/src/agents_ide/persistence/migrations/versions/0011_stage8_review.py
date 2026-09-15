"""Keep confirmed plan identity and acceptance criteria immutable."""

from alembic import op

revision = "0011_stage8_review"
down_revision = "0010_stage8_plan"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TRIGGER plan_items_fixed_definition
        BEFORE UPDATE OF run_id, item_id, order_index, title, acceptance_criteria_json, scope
        ON plan_items
        WHEN NEW.run_id IS NOT OLD.run_id OR NEW.item_id IS NOT OLD.item_id
          OR NEW.order_index IS NOT OLD.order_index OR NEW.title IS NOT OLD.title
          OR NEW.acceptance_criteria_json IS NOT OLD.acceptance_criteria_json
          OR NEW.scope IS NOT OLD.scope
        BEGIN SELECT RAISE(ABORT, 'plan definition is immutable'); END
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER plan_items_fixed_definition")
