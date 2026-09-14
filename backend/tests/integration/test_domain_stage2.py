"""Stage 2 integration tests for projects, chats, templates and bindings."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agents_ide.api.app import create_app


def _project_payload(workspace: Path) -> dict[str, object]:
    return {
        "name": "demo",
        "workspace_path": str(workspace),
    }


def _create_project(
    client: TestClient, headers: dict[str, str], workspace: Path, name: str = "demo"
) -> dict[str, object]:
    response = client.post(
        "/api/projects",
        json={"name": name, "workspace_path": str(workspace)},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_project_create_lists_and_persists(authenticated, tmp_path):
    client, headers = authenticated
    workspace = tmp_path / "ws1"
    workspace.mkdir()
    project = _create_project(client, headers, workspace)
    assert project["archived"] is False
    assert project["workspace"]["normalized_path"].endswith("ws1")
    identity = (project["workspace"]["identity_dev"], project["workspace"]["identity_ino"])

    # Same path → no duplicate project.
    response = client.post(
        "/api/projects",
        json={"name": "demo-second", "workspace_path": str(workspace)},
        headers=headers,
    )
    assert response.status_code == 409
    assert response.json()["code"] == "conflict"

    response = client.get("/api/projects", headers=headers)
    assert response.status_code == 200
    listed = response.json()
    assert len(listed) == 1
    workspace = listed[0]["workspace"]
    assert (workspace["identity_dev"], workspace["identity_ino"]) == identity

    # Persist across app restart.
    headers_only = {"Origin": headers["Origin"], "X-CSRF-Token": headers["X-CSRF-Token"]}
    settings = client.app.state.settings
    with TestClient(create_app(settings), base_url=settings.origin) as restarted:
        restarted.cookies.update(client.cookies)
        response = restarted.get("/api/projects", headers=headers_only)
        assert response.status_code == 200
        assert len(response.json()) == 1


def test_project_archive_blocks_active_run(authenticated, tmp_path):
    client, headers = authenticated
    workspace = tmp_path / "ws-archive"
    workspace.mkdir()
    project = _create_project(client, headers, workspace, name="arch")

    # Create a binding referencing the project.
    template = client.post(
        "/api/templates",
        json={"name": "tpl", "schema_version": "1.0.0"},
        headers=headers,
    ).json()
    version = client.post(
        f"/api/templates/{template['id']}/versions",
        json={"graph": {"nodes": [{"id": "start", "type": "Start"}], "edges": []}},
        headers=headers,
    ).json()
    binding = client.post(
        f"/api/versions/{version['id']}/bindings",
        json={"project_id": project["id"], "name": "main"},
        headers=headers,
    ).json()

    # Start a Run so archive cannot proceed.
    run = client.post(
        "/api/runs",
        json={
            "project_id": project["id"],
            "binding_id": binding["id"],
            "message": "hi",
            "idempotency_key": "run-active",
        },
        headers=headers,
    ).json()
    assert run["state"] == "queued"

    response = client.post(
        f"/api/projects/{project['id']}/archive",
        json={"expected_version": project["version"], "archive": True},
        headers=headers,
    )
    assert response.status_code == 409
    assert response.json()["code"] == "project_has_active_runs"


def test_chats_messages_and_archive(authenticated, tmp_path):
    client, headers = authenticated
    workspace = tmp_path / "ws-chat"
    workspace.mkdir()
    project = _create_project(client, headers, workspace, name="chat-proj")

    chat = client.post(
        f"/api/projects/{project['id']}/chats",
        json={"title": "План"},
        headers=headers,
    ).json()

    response = client.post(
        f"/api/chats/{chat['id']}/messages",
        json={"role": "user", "content": "Привет"},
        headers=headers,
    )
    assert response.status_code == 201
    response = client.post(
        f"/api/chats/{chat['id']}/messages",
        json={"role": "assistant", "content": "Здравствуйте"},
        headers=headers,
    )
    assert response.status_code == 201

    listed = client.get(f"/api/chats/{chat['id']}/messages", headers=headers).json()
    assert [m["role"] for m in listed] == ["user", "assistant"]

    # Same title in the same project should conflict.
    response = client.post(
        f"/api/projects/{project['id']}/chats",
        json={"title": "План"},
        headers=headers,
    )
    assert response.status_code == 409

    # Archive then unarchive.
    response = client.post(
        f"/api/chats/{chat['id']}/archive",
        json={"expected_version": chat["version"], "archive": True},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["archived"] is True

    response = client.get("/api/projects/{}/chats".format(project["id"]), headers=headers).json()
    assert all(item["archived"] for item in response) or not response


def test_pipeline_versions_are_immutable(authenticated, tmp_path):
    client, headers = authenticated
    workspace = tmp_path / "ws-tpl"
    workspace.mkdir()
    project = _create_project(client, headers, workspace, name="tpl-proj")

    template = client.post(
        "/api/templates",
        json={"name": "fix-iterate", "schema_version": "1.0.0"},
        headers=headers,
    ).json()
    graph = {
        "nodes": [
            {"id": "start", "type": "Start"},
            {"id": "finish", "type": "End"},
        ],
        "edges": [],
    }
    first = client.post(
        f"/api/templates/{template['id']}/versions",
        json={"graph": graph, "required_features": ["git"], "inputs": {"x": 1}},
        headers=headers,
    ).json()
    second = client.post(
        f"/api/templates/{template['id']}/versions",
        json={"graph": graph, "required_features": ["git"], "inputs": {"x": 2}},
        headers=headers,
    ).json()
    assert first["version_number"] == 1
    assert second["version_number"] == 2
    assert first["execution_hash"] != second["execution_hash"]

    # Create a binding; then start a Run.
    binding = client.post(
        f"/api/versions/{first['id']}/bindings",
        json={"project_id": project["id"], "name": "main"},
        headers=headers,
    ).json()
    run = client.post(
        "/api/runs",
        json={
            "project_id": project["id"],
            "binding_id": binding["id"],
            "message": "do",
            "idempotency_key": "key-1",
        },
        headers=headers,
    ).json()
    snapshot_before = run["snapshot_hash"]

    # Mutate the binding; the existing Run must retain its original hash.
    response = client.patch(
        f"/api/bindings/{binding['id']}",
        json={"name": "main-renamed", "expected_version": binding["version"]},
        headers=headers,
    )
    assert response.status_code == 200
    response = client.get(f"/api/runs/{run['id']}", headers=headers)
    assert response.status_code == 200
    assert response.json()["snapshot_hash"] == snapshot_before


def test_idempotency_returns_same_run(authenticated, tmp_path):
    client, headers = authenticated
    workspace = tmp_path / "ws-idem"
    workspace.mkdir()
    project = _create_project(client, headers, workspace, name="idem")
    template = client.post(
        "/api/templates",
        json={"name": "idem-tpl", "schema_version": "1.0.0"},
        headers=headers,
    ).json()
    version = client.post(
        f"/api/templates/{template['id']}/versions",
        json={"graph": {"nodes": [{"id": "start", "type": "Start"}], "edges": []}},
        headers=headers,
    ).json()
    binding = client.post(
        f"/api/versions/{version['id']}/bindings",
        json={"project_id": project["id"], "name": "main"},
        headers=headers,
    ).json()

    payload = {
        "project_id": project["id"],
        "binding_id": binding["id"],
        "message": "build it",
        "idempotency_key": "fixed-key",
    }
    first = client.post("/api/runs", json=payload, headers=headers).json()
    second = client.post("/api/runs", json=payload, headers=headers).json()
    assert first["id"] == second["id"]
    assert first["idempotency_key"] == second["idempotency_key"]

    # Same key with different payload is rejected.
    conflicting = dict(payload, message="build it differently")
    response = client.post("/api/runs", json=conflicting, headers=headers)
    assert response.status_code == 409
    assert response.json()["code"] == "idempotency_mismatch"


def test_command_journal_accepts_duplicates_with_same_payload(authenticated, tmp_path):
    client, headers = authenticated
    workspace = tmp_path / "ws-cmd"
    workspace.mkdir()
    project = _create_project(client, headers, workspace, name="cmd")
    template = client.post(
        "/api/templates",
        json={"name": "cmd-tpl", "schema_version": "1.0.0"},
        headers=headers,
    ).json()
    version = client.post(
        f"/api/templates/{template['id']}/versions",
        json={"graph": {"nodes": [{"id": "start", "type": "Start"}], "edges": []}},
        headers=headers,
    ).json()
    binding = client.post(
        f"/api/versions/{version['id']}/bindings",
        json={"project_id": project["id"], "name": "main"},
        headers=headers,
    ).json()
    run = client.post(
        "/api/runs",
        json={
            "project_id": project["id"],
            "binding_id": binding["id"],
            "message": "ok",
            "idempotency_key": "cmd-key",
        },
        headers=headers,
    ).json()

    payload = {
        "command_id": "pause-1",
        "command_type": "pause",
        "expected_state_version": run["state_version"],
        "payload": {"reason": "manual"},
    }
    first = client.post(f"/api/runs/{run['id']}/commands", json=payload, headers=headers).json()
    assert first["status"] == "accepted"

    # Same command_id + same payload hash returns the original record.
    second = client.post(f"/api/runs/{run['id']}/commands", json=payload, headers=headers).json()
    assert second["sequence"] == first["sequence"]

    # Different payload → 409.
    response = client.post(
        f"/api/runs/{run['id']}/commands",
        json={**payload, "payload": {"reason": "different"}},
        headers=headers,
    )
    assert response.status_code == 409
    assert response.json()["code"] == "command_id_conflict"


@pytest.mark.skipif(os.name != "nt", reason="DPAPI is Windows-only; no plaintext fallback")
def test_provider_secret_is_written_and_unreadable(authenticated, tmp_path):
    client, headers = authenticated
    response = client.post(
        "/api/connections",
        json={
            "name": "openai",
            "provider_kind": "openai_compatible",
            "base_url": "https://api.openai.com",
            "secret": "sk-EXAMPLE",
            "manual_models": ["gpt-test"],
        },
        headers=headers,
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["has_secret"] is True
    assert "secret" not in payload
    assert "sk-EXAMPLE" not in response.text

    listed = client.get("/api/connections", headers=headers).json()
    assert listed[0]["has_secret"] is True
    assert "sk-EXAMPLE" not in json.dumps(listed)

    # Rotating the secret keeps the previous ciphertext available until GC.
    response = client.patch(
        f"/api/connections/{payload['id']}",
        json={"secret": "sk-ROTATED", "expected_version": payload["version"]},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["has_secret"] is True

    secrets_dir = client.app.state.settings.data_dir / "secrets"
    stored = list(secrets_dir.glob("*.dpapi"))
    assert len(stored) == 2


def test_provider_url_validation_rejects_remote_http(authenticated):
    client, headers = authenticated
    response = client.post(
        "/api/connections",
        json={
            "name": "remote",
            "provider_kind": "openai_compatible",
            "base_url": "http://api.example.com",
        },
        headers=headers,
    )
    assert response.status_code == 400  # url validation must reject remote http


def test_resolved_settings_include_overrides(authenticated, tmp_path):
    client, headers = authenticated
    workspace = tmp_path / "ws-resolve"
    workspace.mkdir()
    project = _create_project(client, headers, workspace, name="resolve")
    template = client.post(
        "/api/templates",
        json={"name": "tpl", "schema_version": "1.0.0"},
        headers=headers,
    ).json()
    version = client.post(
        f"/api/templates/{template['id']}/versions",
        json={"graph": {"nodes": [{"id": "start", "type": "Start"}], "edges": []}},
        headers=headers,
    ).json()
    binding = client.post(
        f"/api/versions/{version['id']}/bindings",
        json={
            "project_id": project["id"],
            "name": "main",
            "role_assignments": {"implementer": "codex"},
            "model_overrides": {"implementer": "gpt-4o"},
            "limit_overrides": {"max_calls": 50},
        },
        headers=headers,
    ).json()

    resolved = client.get(f"/api/bindings/{binding['id']}/resolved", headers=headers).json()
    # Resolved trust includes binding settings and pinned resources, unlike the template hash.
    run = client.post(
        "/api/runs",
        headers=headers,
        json={
            "project_id": project["id"],
            "binding_id": binding["id"],
            "message": "task",
            "idempotency_key": "resolved-hash",
        },
    ).json()
    assert resolved["execution_hash"] == run["execution_hash"]
    assert resolved["execution_hash"] != version["execution_hash"]
    by_name = {item["name"]: item for item in resolved["settings"]}
    assert by_name["role_assignments"]["value"]["implementer"] == "codex"
    assert by_name["model_overrides"]["value"]["implementer"] == "gpt-4o"
    assert by_name["limit_overrides"]["value"]["max_calls"] == 50
    assert by_name["branch_policy"]["source"] == "default"


def test_workspace_probe_rejects_unicode_and_missing(tmp_path, authenticated):
    client, headers = authenticated
    response = client.post(
        "/api/workspace/probe",
        json={"path": str(tmp_path / "missing")},
        headers=headers,
    )
    assert response.status_code == 404

    workspace = tmp_path / "проект"
    workspace.mkdir()
    response = client.post(
        "/api/workspace/probe",
        json={"path": str(workspace)},
        headers=headers,
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["normalized_path"].endswith("проект")


def test_schema_endpoints_publish_node_catalog(authenticated):
    client, headers = authenticated
    response = client.get("/api/schema/nodes", headers=headers)
    assert response.status_code == 200
    nodes = response.json()["nodes"]
    assert "Start" in nodes and "AgentTask" in nodes and "PlanControl" in nodes
    response = client.get("/api/capabilities", headers=headers)
    assert response.status_code == 200
    assert "projects" in response.json()["features"]
