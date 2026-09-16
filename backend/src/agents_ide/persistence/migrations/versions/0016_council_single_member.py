"""Council single-member fallback: allow explicit degraded plan promotion."""

from alembic import op

revision = "0016_council_single_member"
down_revision = "0015_council_retry"
branch_labels = None
depends_on = None


def _replace_author_constraint(expression: str) -> None:
    bind = op.get_bind()
    # sqlite3 legacy transaction mode does not start a transaction for DDL.
    # Keep the table rebuild, answers, triggers and Alembic version atomic.
    driver = bind.connection.driver_connection
    assert driver is not None
    if not driver.in_transaction:
        bind.exec_driver_sql("BEGIN IMMEDIATE")
    with bind.begin_nested():
        # Revisions are referenced by answers. Temporarily empty that child
        # table inside this transaction; keep FK enforcement enabled throughout.
        op.execute("CREATE TEMP TABLE council_0016_answers AS SELECT * FROM planning_answers")
        op.execute("DELETE FROM planning_answers")
        op.execute("DROP TRIGGER planning_confirmed_no_update")
        op.execute("DROP TRIGGER planning_confirmed_no_delete")
        with op.batch_alter_table("planning_revisions") as batch:
            batch.drop_constraint("ck_planning_revisions_author", type_="check")
            batch.create_check_constraint("ck_planning_revisions_author", expression)
        op.execute("INSERT INTO planning_answers SELECT * FROM council_0016_answers")
        op.execute("DROP TABLE council_0016_answers")
        for action in ("UPDATE", "DELETE"):
            op.execute(f"""
                CREATE TRIGGER planning_confirmed_no_{action.lower()}
                BEFORE {action} ON planning_revisions WHEN OLD.confirmed_at IS NOT NULL
                BEGIN SELECT RAISE(ABORT, 'confirmed_planning_revision_immutable'); END
            """)
        if bind.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
            raise RuntimeError("Council migration violated foreign keys")


def upgrade() -> None:
    _replace_author_constraint("author IN ('merger','user','single_member')")


def downgrade() -> None:
    if (
        op.get_bind()
        .exec_driver_sql("SELECT 1 FROM planning_revisions WHERE author = 'single_member' LIMIT 1")
        .first()
    ):
        raise RuntimeError("Cannot downgrade: single_member revisions must retain their author")
    _replace_author_constraint("author IN ('merger','user')")
