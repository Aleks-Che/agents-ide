"""Keep one editable definition per template; runs retain their own snapshots."""

import json

import sqlalchemy as sa
from alembic import op

revision = "0025_single_template"
down_revision = "0024_planning_owner"
branch_labels = None
depends_on = None


def _protect_run_snapshot() -> None:
    op.execute("""CREATE TRIGGER run_snapshot_immutable BEFORE UPDATE OF
        request_hash, request_json, snapshot_json, resolved_settings_json, execution_hash,
        policy_hash, schema_version, pipeline_version_id, project_id, chat_id, binding_id,
        idempotency_key ON runs
        BEGIN SELECT RAISE(ABORT, 'run input is immutable'); END""")


def upgrade() -> None:
    from agents_ide.domain.graph_validation import definition_hash, validate_graph
    from agents_ide.domain.schemas import PipelineDraft

    op.execute("DROP TRIGGER pipeline_version_immutable")
    op.execute("DROP TRIGGER run_snapshot_immutable")
    connection = op.get_bind()
    templates = (
        connection.execute(sa.text("SELECT id, draft_json, schema_version FROM pipeline_templates"))
        .mappings()
        .all()
    )
    for template in templates:
        definitions = (
            connection.execute(
                sa.text(
                    "SELECT id, execution_hash FROM pipeline_versions "
                    "WHERE template_id=:id ORDER BY version_number DESC"
                ),
                {"id": template["id"]},
            )
            .mappings()
            .all()
        )
        if not definitions:
            continue
        current = definitions[0]["id"]
        # The saved draft may intentionally have returned to older content.
        draft = json.loads(template["draft_json"])
        report = validate_graph(draft.get("graph", {}), inputs=draft.get("inputs", {}))
        if report.ok:
            digest = definition_hash(
                {
                    "graph": draft["graph"],
                    "required_features": sorted(
                        set(draft.get("required_features", [])) | set(report.features)
                    ),
                    "inputs": draft.get("inputs", {}),
                    "settings": PipelineDraft.model_validate(draft).settings.model_dump(
                        exclude_none=True
                    ),
                    "schema_version": template["schema_version"],
                }
            )
            current = next(
                (row["id"] for row in definitions if row["execution_hash"] == digest), current
            )
        for old in definitions:
            if old["id"] == current:
                continue
            parameters = {"current": current, "old": old["id"]}
            connection.execute(
                sa.text("UPDATE pipeline_bindings SET version_id=:current WHERE version_id=:old"),
                parameters,
            )
            connection.execute(
                sa.text(
                    "UPDATE runs SET pipeline_version_id=:current WHERE pipeline_version_id=:old"
                ),
                parameters,
            )
            connection.execute(sa.text("DELETE FROM pipeline_versions WHERE id=:old"), parameters)
        connection.execute(
            sa.text("UPDATE pipeline_versions SET version_number=1 WHERE id=:id"), {"id": current}
        )
    op.create_index("uq_template_definition", "pipeline_versions", ["template_id"], unique=True)
    _protect_run_snapshot()


def downgrade() -> None:
    op.drop_index("uq_template_definition", table_name="pipeline_versions")
    op.execute("""CREATE TRIGGER pipeline_version_immutable BEFORE UPDATE ON pipeline_versions
        BEGIN SELECT RAISE(ABORT, 'pipeline version is immutable'); END""")
