"""Current template settings, migrated history and saved JSON recovery."""

import json

import pytest
from alembic import command as migrations
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from test_stage4_review import make_run
from test_stage5_review import command, run_now
from test_stage_restart import stage_payload

from agents_ide.adapters.base import ExternalOutcome, LLMResult
from agents_ide.domain.common import to_json
from agents_ide.engine.runner import Runner
from agents_ide.persistence.database import MIGRATIONS_DIRECTORY
from agents_ide.persistence.models import ArtifactManifest, PipelineVersion, Run, StepAttempt

RAW = '<think>Example: {"answer":"YES"}</think>\n```json\n{"answer":"NO"}\n```'
GRAPH = {
    "nodes": [
        {"id": "s", "type": "Start"},
        {
            "id": "check",
            "type": "LLMRequest",
            "config": {
                "prompt": "Return JSON",
                "response_format": "json",
                "output_schema": {
                    "type": "object",
                    "properties": {"answer": {"type": "string"}},
                    "required": ["answer"],
                    "additionalProperties": False,
                },
                "model_selection": {
                    "kind": "direct",
                    "provider_connection_id": "CONNECTION",
                    "model_id": "m",
                },
            },
        },
        {"id": "e", "type": "End"},
    ],
    "edges": [{"from": "s", "to": "check"}, {"from": "check", "to": "e"}],
}


def fail_json(authenticated, tmp_path, settings, monkeypatch, raw=RAW):
    run, factory = make_run(authenticated, tmp_path, graph=GRAPH)
    calls = []

    class Adapter:
        def run(self, request):
            calls.append(request.prompt)
            return LLMResult(ExternalOutcome.SUCCEEDED, raw, None, None)

    monkeypatch.setattr(Runner, "_build_adapters", lambda *_: (None, Adapter()))
    assert run_now(run, factory, settings).waiting_reason.code == "invalid_response_format"
    return run, factory, calls


def test_restart_uses_current_saved_json_settings(authenticated, tmp_path, settings, monkeypatch):
    run, factory, calls = fail_json(authenticated, tmp_path, settings, monkeypatch)
    client, headers = authenticated
    with factory() as session:
        row = session.get(Run, run["id"])
        original = row.snapshot_json
        definition = session.get(PipelineVersion, row.pipeline_version_id)
        template_id, definition_id = definition.template_id, definition.id
        graph = json.loads(definition.graph_json)
    graph["nodes"][1]["config"]["json_processing"] = {"extract_json": True}
    url = f"/api/templates/{template_id}"
    template = client.get(url).json()
    response = client.put(
        f"{url}/save",
        json={"expected_version": template["version"], "graph": graph},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert client.get(f"{url}/saved").json()["id"] == definition_id
    assert len(client.get(f"{url}/versions").json()) == 1
    response = command(authenticated, run, "restart_stage", stage_payload(factory, run))
    assert response.status_code == 200, response.text
    assert run_now(run, factory, settings).final_state == "completed"
    assert len(calls) == 2
    with factory() as session:
        row = session.get(Run, run["id"])
        assert row.snapshot_json == original
        attempts = list(session.scalars(select(StepAttempt).order_by(StepAttempt.started_at)))
        assert [a.status for a in attempts] == ["failed", "succeeded"]
        assert attempts[0].execution_id != attempts[1].execution_id
        body = json.loads(session.get(ArtifactManifest, attempts[-1].result_artifact_id).body_json)
        assert body["validated"] == {"answer": "NO"}


def test_saved_json_reprocessing_keeps_raw_and_does_not_call_model(
    authenticated, tmp_path, settings, monkeypatch
):
    run, factory, calls = fail_json(authenticated, tmp_path, settings, monkeypatch)
    with factory() as session:
        row = session.get(Run, run["id"])
        original = row.snapshot_json
        old_artifact = session.get(StepAttempt, row.current_attempt_id).result_artifact_id
    response = command(authenticated, run, "resolve", {"json_processing": {"extract_json": True}})
    assert response.status_code == 200, response.text
    assert response.json()["response"]["reprocessing_pending"]
    response = command(authenticated, run, "resume")
    assert response.status_code == 200, response.text
    assert run_now(run, factory, settings).final_state == "completed"
    assert len(calls) == 1
    with factory() as session:
        row = session.get(Run, run["id"])
        assert row.snapshot_json == original
        assert json.loads(row.runtime_json)["external_calls"] == 1
        attempts = list(session.scalars(select(StepAttempt)))
        assert len(attempts) == 1 and attempts[0].status == "succeeded"
        assert json.loads(session.get(ArtifactManifest, old_artifact).body_json)["raw_text"] == RAW
        body = json.loads(session.get(ArtifactManifest, attempts[0].result_artifact_id).body_json)
        assert body["reprocessed_from"] == old_artifact
        assert body["validated"] == {"answer": "NO"}


@pytest.mark.parametrize("raw", ['<think>x</think>{"answer":', '<think>x</think>{"answer":1}'])
def test_reprocessing_rejects_invalid_saved_json(
    authenticated, tmp_path, settings, monkeypatch, raw
):
    run, factory, calls = fail_json(authenticated, tmp_path, settings, monkeypatch, raw)
    response = command(authenticated, run, "resolve", {"json_processing": {"extract_json": True}})
    assert response.status_code == 422, response.text
    assert len(calls) == 1
    with factory() as session:
        assert "json_reprocessing" not in json.loads(session.get(Run, run["id"]).runtime_json)


@pytest.mark.parametrize("saved_draft_matches_old", [False, True])
def test_migration_keeps_current_definition_and_run_history(
    authenticated, tmp_path, settings, saved_draft_matches_old
):
    run, factory = make_run(authenticated, tmp_path)
    assert run_now(run, factory, settings).final_state == "completed"
    client, _ = authenticated
    with client.app.state.engine.connect() as connection:
        config = Config()
        config.set_main_option("script_location", str(MIGRATIONS_DIRECTORY))
        config.attributes["connection"] = connection
        migrations.downgrade(config, "0024_planning_owner")
        connection.commit()
        old = dict(
            connection.exec_driver_sql(
                "SELECT * FROM pipeline_versions WHERE id="
                "(SELECT pipeline_version_id FROM runs WHERE id=?)",
                (run["id"],),
            )
            .mappings()
            .one()
        )
        snapshots = connection.exec_driver_sql(
            "SELECT id, snapshot_json, execution_hash FROM runs"
        ).all()
        artifacts = connection.exec_driver_sql("SELECT * FROM artifact_manifests").all()
        attempts = connection.exec_driver_sql("SELECT * FROM step_attempts").all()
        latest = {
            **old,
            "id": "new-definition",
            "version_number": 2,
            "inputs_json": '{"task":"new"}',
            "execution_hash": "later",
        }
        connection.exec_driver_sql(
            f"INSERT INTO pipeline_versions ({','.join(latest)}) "
            f"VALUES ({','.join('?' for _ in latest)})",
            tuple(latest.values()),
        )
        if saved_draft_matches_old:
            draft = {
                "graph": json.loads(old["graph_json"]),
                "inputs": json.loads(old["inputs_json"]),
                "settings": json.loads(old["settings_json"]),
                "required_features": json.loads(old["required_features_json"]),
            }
            connection.exec_driver_sql(
                "UPDATE pipeline_templates SET draft_json=? WHERE id=?",
                (to_json(draft), old["template_id"]),
            )
        connection.commit()
        migrations.upgrade(config, "head")
        connection.commit()
        expected = old["id"] if saved_draft_matches_old else latest["id"]
        assert connection.exec_driver_sql(
            "SELECT id, version_number FROM pipeline_versions WHERE template_id=?",
            (old["template_id"],),
        ).all() == [(expected, 1)]
        assert (
            connection.exec_driver_sql(
                "SELECT pipeline_version_id FROM runs WHERE id=?", (run["id"],)
            ).scalar()
            == expected
        )
        assert (
            connection.exec_driver_sql(
                "SELECT version_id FROM pipeline_bindings WHERE id=?", (run["binding_id"],)
            ).scalar()
            == expected
        )
        assert (
            connection.exec_driver_sql("SELECT id, snapshot_json, execution_hash FROM runs").all()
            == snapshots
        )
        assert connection.exec_driver_sql("SELECT * FROM artifact_manifests").all() == artifacts
        assert connection.exec_driver_sql("SELECT * FROM step_attempts").all() == attempts
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        with pytest.raises(IntegrityError, match="run input is immutable"):
            connection.exec_driver_sql("UPDATE runs SET snapshot_json='{}'")
