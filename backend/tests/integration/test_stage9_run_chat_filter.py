"""Run list filtering by chat and project for the stage 9 chat launch UI."""

from __future__ import annotations

import json
import uuid

from agents_ide.persistence.models import Run as RunModel


def _project(client, headers, tmp_path, name: str) -> dict:
    workspace = tmp_path / f"runs-chat-{name}"
    workspace.mkdir()
    response = client.post(
        "/api/projects",
        json={"name": f"runs chat {name}", "workspace_path": str(workspace)},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _template_version_binding(client, headers, project_id: str) -> dict:
    template = client.post(
        "/api/templates",
        json={"name": f"runs-chat {uuid.uuid4().hex[:8]}"},
        headers=headers,
    ).json()
    graph = {
        "nodes": [
            {"id": "start", "type": "Start"},
            {"id": "end", "type": "End"},
        ],
        "edges": [{"id": "e", "from": "start", "to": "end"}],
    }
    version = client.post(
        f"/api/templates/{template['id']}/versions",
        json={"graph": graph, "settings": {"role_assignments": {}}},
        headers=headers,
    ).json()
    binding = client.post(
        f"/api/versions/{version['id']}/bindings",
        json={"project_id": project_id, "name": "binding"},
        headers=headers,
    ).json()
    return binding


def _start_run(
    client,
    headers,
    *,
    project_id: str,
    binding_id: str,
    chat_id: str | None,
    message: str | None = None,
) -> dict:
    body = {
        "project_id": project_id,
        "binding_id": binding_id,
        "idempotency_key": str(uuid.uuid4()),
        "execution_mode": "simulated",
    }
    if chat_id is not None:
        body["chat_id"] = chat_id
    if message is not None:
        body["message"] = message
    if chat_id is None and message is None:
        body["message"] = "orphaned run"
    response = client.post("/api/runs", json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def test_run_list_filters_by_chat_id(authenticated, tmp_path):
    client, headers = authenticated
    project = _project(client, headers, tmp_path, "alpha")
    binding = _template_version_binding(client, headers, project["id"])
    chat_a = client.post(
        f"/api/projects/{project['id']}/chats",
        json={"title": "A"},
        headers=headers,
    ).json()
    chat_b = client.post(
        f"/api/projects/{project['id']}/chats",
        json={"title": "B"},
        headers=headers,
    ).json()
    run_a1 = _start_run(
        client,
        headers,
        project_id=project["id"],
        binding_id=binding["id"],
        chat_id=chat_a["id"],
    )
    run_a2 = _start_run(
        client,
        headers,
        project_id=project["id"],
        binding_id=binding["id"],
        chat_id=chat_a["id"],
    )
    run_b1 = _start_run(
        client,
        headers,
        project_id=project["id"],
        binding_id=binding["id"],
        chat_id=chat_b["id"],
    )

    listed = client.get(
        "/api/runs", params={"project_id": project["id"], "chat_id": chat_a["id"]}, headers=headers
    ).json()
    ids = {item["id"] for item in listed}
    assert ids == {run_a1["id"], run_a2["id"]}
    assert {item["chat_id"] for item in listed} == {chat_a["id"]}

    chat_only = client.get("/api/runs", params={"chat_id": chat_a["id"]}).json()
    assert {item["id"] for item in chat_only} == ids
    assert (
        client.get(
            "/api/runs", params={"project_id": "other-project", "chat_id": chat_a["id"]}
        ).json()
        == []
    )

    listed_b = client.get(
        "/api/runs", params={"project_id": project["id"], "chat_id": chat_b["id"]}, headers=headers
    ).json()
    assert {item["id"] for item in listed_b} == {run_b1["id"]}


def test_run_list_filters_by_project_only(authenticated, tmp_path):
    client, headers = authenticated
    project = _project(client, headers, tmp_path, "beta")
    binding = _template_version_binding(client, headers, project["id"])
    chat = client.post(
        f"/api/projects/{project['id']}/chats",
        json={"title": "single"},
        headers=headers,
    ).json()
    orphan = _start_run(
        client,
        headers,
        project_id=project["id"],
        binding_id=binding["id"],
        chat_id=None,
    )
    in_chat = _start_run(
        client,
        headers,
        project_id=project["id"],
        binding_id=binding["id"],
        chat_id=chat["id"],
    )

    listed = client.get("/api/runs", params={"project_id": project["id"]}, headers=headers).json()
    ids = {item["id"] for item in listed}
    assert ids == {orphan["id"], in_chat["id"]}


def test_chat_launch_preflight_inputs_context_snapshot_and_replay(authenticated, tmp_path):
    client, headers = authenticated
    project = _project(client, headers, tmp_path, "snapshot")
    template = client.post("/api/templates", json={"name": "input launch"}, headers=headers).json()
    version = client.post(
        f"/api/templates/{template['id']}/versions",
        json={
            "graph": {
                "nodes": [{"id": "start", "type": "Start"}, {"id": "end", "type": "End"}],
                "edges": [{"id": "edge", "from": "start", "to": "end"}],
                "input_schema": {
                    "type": "object",
                    "required": ["task"],
                    "properties": {"task": {"type": "string"}},
                },
            },
            "inputs": {"default_value": "from version"},
        },
        headers=headers,
    ).json()
    binding = client.post(
        f"/api/versions/{version['id']}/bindings",
        json={"project_id": project["id"], "name": "launch"},
        headers=headers,
    ).json()
    chat = client.post(
        f"/api/projects/{project['id']}/chats",
        json={"title": "context"},
        headers=headers,
    ).json()
    saved = client.post(
        f"/api/chats/{chat['id']}/messages",
        json={"role": "user", "content": "saved context"},
        headers=headers,
    ).json()
    url = f"/api/bindings/{binding['id']}/preflight"
    missing = client.post(url, json={"execution_mode": "simulated"}, headers=headers).json()
    assert not missing["ok"]
    assert any(e["details"].get("missing") == ["task"] for e in missing["errors"])
    parameters = {
        "execution_mode": "simulated",
        "inputs": {"task": "from run"},
        "overrides": {"branch_policy": "current"},
    }
    checked = client.post(url, json=parameters, headers=headers).json()
    assert checked["ok"], checked
    body = {
        **parameters,
        "project_id": project["id"],
        "chat_id": chat["id"],
        "binding_id": binding["id"],
        "message": "unsent draft",
        "idempotency_key": str(uuid.uuid4()),
        "trusted_execution_hash": checked["execution_hash"],
    }
    response = client.post("/api/runs", json=body, headers=headers)
    assert response.status_code == 201, response.text
    run = response.json()
    assert run["execution_hash"] == checked["execution_hash"]
    with client.app.state.session_factory() as session:
        snapshot = json.loads(session.get(RunModel, run["id"]).snapshot_json)
    assert snapshot["input"] == {
        "message": "unsent draft",
        "messages": [{"id": saved["id"], "role": "user", "content": "saved context"}],
        "values": {"task": "from run", "default_value": "from version"},
    }
    assert snapshot["resolved_settings"]["branch_policy"] == "current"
    assert len(client.get(f"/api/chats/{chat['id']}/messages").json()) == 1
    # A retry after subsequent chat/binding edits returns the original immutable snapshot.
    client.post(
        f"/api/chats/{chat['id']}/messages",
        json={"role": "user", "content": "later"},
        headers=headers,
    )
    client.patch(
        f"/api/bindings/{binding['id']}",
        json={"expected_version": binding["version"], "dirty_policy": "allow_nonoverlap"},
        headers=headers,
    )
    replay = client.post("/api/runs", json=body, headers=headers)
    assert replay.status_code == 201
    assert replay.json()["id"] == run["id"]
    assert replay.json()["snapshot_hash"] == run["snapshot_hash"]
    assert len(client.get("/api/runs", params={"chat_id": chat["id"]}).json()) == 1
    stale = client.post(
        "/api/runs",
        json={**body, "idempotency_key": str(uuid.uuid4()), "overrides": {}},
        headers=headers,
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "execution_hash_changed"


def test_chat_filtered_runs_require_pairing(client):
    assert client.get("/api/runs", params={"chat_id": "some-chat"}).status_code == 401
