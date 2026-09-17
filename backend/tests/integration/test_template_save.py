"""Unified saving updates bindings atomically, retaining immutable run definitions."""

import pytest
from fastapi.testclient import TestClient

from agents_ide.api.app import create_app
from agents_ide.services.templates import sync_saved_template_bindings

GRAPH = {
    "nodes": [{"id": "start", "type": "Start"}, {"id": "end", "type": "End"}],
    "edges": [{"from": "start", "to": "end"}],
}


def test_save_updates_bindings_reuses_content_and_preserves_old_definition(authenticated, tmp_path):
    client, headers = authenticated
    template = client.post("/api/templates", json={"name": "Flow"}, headers=headers).json()
    url = f"/api/templates/{template['id']}"

    def save(expected, task):
        response = client.put(
            f"{url}/save",
            json={"expected_version": expected, "graph": GRAPH, "inputs": {"task": task}},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        return response.json()

    template = save(template["version"], "A")
    first = client.get(f"{url}/saved").json()
    assert client.get("/api/bindings").json() == []
    bindings = []
    for i in range(2):
        root = tmp_path / f"workspace{i}"
        root.mkdir()
        project = client.post(
            "/api/projects",
            json={"name": f"Project{i}", "workspace_path": str(root)},
            headers=headers,
        ).json()
        response = client.post(
            f"{url}/bindings",
            json={
                "project_id": project["id"],
                "name": f"Binding{i}",
                "limit_overrides": {"max_calls": 25},
            },
            headers=headers,
        )
        assert response.status_code == 201, response.text
        bindings.append(response.json())
    template = save(template["version"], "B")
    second = client.get(f"{url}/saved").json()
    assert second["id"] != first["id"]
    for binding in bindings:
        current = client.get(f"/api/bindings/{binding['id']}").json()
        assert current["version_id"] == second["id"]
        assert current["name"] == binding["name"]
        assert current["limit_overrides"] == binding["limit_overrides"]
        assert current["version"] == binding["version"] + 1
    assert client.get(f"/api/versions/{first['id']}").json() == first
    template = save(template["version"], "A")
    assert client.get(f"{url}/saved").json()["id"] == first["id"]
    with client.app.state.session_factory() as session:
        assert sync_saved_template_bindings(session) == 0
        session.commit()
    for binding in bindings:
        assert client.get(f"/api/bindings/{binding['id']}").json()["version_id"] == first["id"]
    template = save(template["version"], "A")
    assert len(client.get(f"{url}/versions").json()) == 2
    assert len(client.get("/api/bindings").json()) == 2
    copied = client.post(
        f"{url}/copy",
        json={"name": "Copy", "expected_version": template["version"]},
        headers=headers,
    ).json()
    assert client.get(f"/api/templates/{copied['id']}/saved").json()["inputs"] == {"task": "A"}


@pytest.mark.parametrize("graph", [{}, {"nodes": [{"id": "s", "type": "Start"}], "edges": []}])
def test_failed_save_leaves_template_and_bindings_unchanged(authenticated, graph):
    client, headers = authenticated
    template = client.post("/api/templates", json={"name": "Flow"}, headers=headers).json()
    url = f"/api/templates/{template['id']}"
    response = client.put(
        f"{url}/save",
        json={"expected_version": template["version"], "graph": graph},
        headers=headers,
    )
    assert response.status_code == 422, response.text
    assert client.get(url).json() == template
    assert client.get(f"{url}/saved").json() is None
    assert (
        client.put(
            f"{url}/save", json={"expected_version": 99, "graph": GRAPH}, headers=headers
        ).status_code
        == 409
    )
    assert client.get(url).json() == template


@pytest.mark.parametrize("unpublished_draft", [False, True])
def test_startup_repairs_legacy_binding_model_selection(
    authenticated, settings, tmp_path, unpublished_draft
):
    client, headers = authenticated
    project = client.post(
        "/api/projects", json={"name": "Project", "workspace_path": str(tmp_path)}, headers=headers
    ).json()
    connection = client.post(
        "/api/connections",
        json={"name": "LLM", "base_url": "http://127.0.0.1:9/v1", "manual_models": ["m"]},
        headers=headers,
    ).json()
    template = client.post("/api/templates", json={"name": "Legacy"}, headers=headers).json()
    url = f"/api/templates/{template['id']}"
    graph = {
        "nodes": [
            {"id": "start", "type": "Start"},
            {"id": "llmrequest_1", "type": "LLMRequest", "config": {"prompt": "YES or NO"}},
            {"id": "end", "type": "End"},
        ],
        "edges": [{"from": "start", "to": "llmrequest_1"}, {"from": "llmrequest_1", "to": "end"}],
    }
    first = client.post(f"{url}/versions", json={"graph": graph}, headers=headers).json()
    binding = client.post(
        f"/api/versions/{first['id']}/bindings",
        json={"project_id": project["id"], "name": "Pinned", "limit_overrides": {"max_calls": 25}},
        headers=headers,
    ).json()
    archived = client.post(
        f"/api/versions/{first['id']}/bindings",
        json={"project_id": project["id"], "name": "Archived"},
        headers=headers,
    ).json()
    response = client.post(
        f"/api/bindings/{archived['id']}/archive?expected_version={archived['version']}",
        headers=headers,
    )
    assert response.status_code == 200, response.text
    binding_url = f"/api/bindings/{binding['id']}"
    before = client.post(f"{binding_url}/preflight", json={}, headers=headers).json()
    assert "model_selection_missing" in [issue["code"] for issue in before["errors"]]
    graph["nodes"][1]["config"]["model_selection"] = {
        "kind": "direct",
        "provider_connection_id": connection["id"],
        "model_id": "m",
    }
    current = client.put(
        f"{url}/draft",
        json={"expected_version": template["version"], "graph": graph},
        headers=headers,
    ).json()
    saved = client.post(
        f"{url}/publish", json={"expected_version": current["version"]}, headers=headers
    ).json()
    if unpublished_draft:
        graph["nodes"][1]["config"].pop("model_selection")
        # The draft differs from every saved version, so startup must not publish it.
        graph["nodes"][1]["config"]["prompt"] = "Unsaved work"
        response = client.put(
            f"{url}/draft",
            json={"expected_version": current["version"], "graph": graph},
            headers=headers,
        )
        assert response.status_code == 200, response.text

    # Exercise the actual startup hook, not just the helper.
    with TestClient(create_app(settings), base_url=settings.origin):
        pass
    updated = client.get(binding_url).json()
    assert updated == {
        **binding,
        "version_id": saved["id"],
        "version": binding["version"] + 1,
        "updated_at": updated["updated_at"],
    }
    assert client.get(f"/api/bindings/{archived['id']}").json()["version_id"] == first["id"]
    assert client.get(f"/api/versions/{first['id']}").json() == first
    after = client.post(f"{binding_url}/preflight", json={}, headers=headers).json()
    assert "model_selection_missing" not in [issue["code"] for issue in after["errors"]]
    with client.app.state.session_factory() as session:
        assert sync_saved_template_bindings(session) == 0
        session.commit()
    assert client.get(binding_url).json() == updated
