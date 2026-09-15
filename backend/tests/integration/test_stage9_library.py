"""Stage 9 — library, provenance and binding warnings."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _create_project(client: TestClient, headers: dict[str, str], tmp_path: Path) -> dict:
    workspace = tmp_path / "library-workspace"
    workspace.mkdir()
    response = client.post(
        "/api/projects",
        json={"name": "library", "workspace_path": str(workspace)},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _create_template(client: TestClient, headers: dict[str, str], name: str) -> dict:
    response = client.post(
        "/api/templates",
        json={"name": name, "schema_version": "1.0.0"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _create_version_with_roles(
    client: TestClient,
    headers: dict[str, str],
    template_id: str,
    implementer: str | None,
    verifier: str | None,
) -> dict:
    graph = {
        "nodes": [
            {"id": "start", "type": "Start"},
            {"id": "end", "type": "End"},
        ],
        "edges": [{"id": "e1", "from": "start", "to": "end"}],
        "roles": {"implementer": "agent", "verifier": "llm"},
    }
    payload: dict = {"graph": graph, "settings": {"role_assignments": {}}}
    if implementer:
        payload["settings"]["model_overrides"] = {"implementer": implementer}
    if verifier:
        # The verifier role uses model_overrides too — binding-level overrides
        # remain supported for direct selections of LLMRequest.
        payload["settings"]["model_overrides"] = {
            **(payload["settings"].get("model_overrides") or {}),
            "verifier": verifier,
        }
    response = client.post(
        f"/api/templates/{template_id}/versions",
        json=payload,
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _create_binding(
    client: TestClient,
    headers: dict[str, str],
    project_id: str,
    version_id: str,
    selections: dict[str, dict],
) -> dict:
    response = client.post(
        f"/api/versions/{version_id}/bindings",
        json={
            "project_id": project_id,
            "name": "main",
            "model_selections": selections,
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_resolved_settings_expose_roles_provenance_and_warnings(authenticated, tmp_path):
    client, headers = authenticated
    project = _create_project(client, headers, tmp_path)
    profile = client.post(
        "/api/harness_profiles",
        json={"name": "p1", "harness_kind": "opencode"},
        headers=headers,
    ).json()
    connection = client.post(
        "/api/connections",
        json={"name": "c1", "base_url": "http://127.0.0.1:9/v1", "manual_models": ["m"]},
        headers=headers,
    ).json()
    template = _create_template(client, headers, "prov")
    version = _create_version_with_roles(client, headers, template["id"], "m", None)
    binding = _create_binding(
        client,
        headers,
        project["id"],
        version["id"],
        {
            "implementer": {
                "kind": "direct",
                "harness_profile_id": profile["id"],
                "model_id": "gpt-x",
            },
            "verifier": {
                "kind": "direct",
                "provider_connection_id": connection["id"],
                "model_id": "gpt-x",
            },
        },
    )

    response = client.get(f"/api/bindings/{binding['id']}/resolved", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["roles"]["implementer"]["kind"] == "agent"
    assert body["roles"]["verifier"]["kind"] == "llm"
    assert body["roles"]["implementer"]["model_id"] == "gpt-x"
    assert body["roles"]["verifier"]["model_id"] == "gpt-x"
    codes = [warning["code"] for warning in body["warnings"]]
    assert "implementer_verifier_same_model" in codes
    sources = {item["name"] for item in body["provenance"]}
    assert "role.implementer" in sources
    assert "role.verifier" in sources
    setting_sources = {item["source"] for item in body["settings"]}
    assert "default" in setting_sources or "binding" in setting_sources


def test_resolved_settings_have_no_warning_when_models_differ(authenticated, tmp_path):
    client, headers = authenticated
    project = _create_project(client, headers, tmp_path)
    profile = client.post(
        "/api/harness_profiles",
        json={"name": "p1", "harness_kind": "opencode"},
        headers=headers,
    ).json()
    connection = client.post(
        "/api/connections",
        json={"name": "c1", "base_url": "http://127.0.0.1:9/v1", "manual_models": ["m"]},
        headers=headers,
    ).json()
    template = _create_template(client, headers, "diff")
    version = _create_version_with_roles(client, headers, template["id"], None, None)
    binding = _create_binding(
        client,
        headers,
        project["id"],
        version["id"],
        {
            "implementer": {
                "kind": "direct",
                "harness_profile_id": profile["id"],
                "model_id": "implementer-model",
            },
            "verifier": {
                "kind": "direct",
                "provider_connection_id": connection["id"],
                "model_id": "verifier-model",
            },
        },
    )

    body = client.get(f"/api/bindings/{binding['id']}/resolved", headers=headers).json()
    assert body["warnings"] == []
    assert body["roles"]["implementer"]["model_id"] == "implementer-model"
    assert body["roles"]["verifier"]["model_id"] == "verifier-model"


def test_presets_endpoint_lists_builtin_presets(authenticated):
    client, headers = authenticated
    response = client.get("/api/presets", headers=headers)
    assert response.status_code == 200, response.text
    presets = response.json()
    assert isinstance(presets, list) and presets
    for preset in presets:
        assert {"id", "name", "template_id", "preset_version", "roles"} <= set(preset)
        assert preset["roles"]


def test_copy_preset_creates_user_template_with_versions(authenticated, tmp_path):
    client, headers = authenticated
    presets = client.get("/api/presets", headers=headers).json()
    assert presets, "presets are installed"
    preset = presets[0]
    response = client.post(
        f"/api/presets/{preset['id']}/copy",
        headers=headers,
        json={"name": "My copy"},
    )
    assert response.status_code == 201, response.text
    template = response.json()
    assert template["kind"] == "user"
    assert template["name"] == "My copy"
    versions = client.get(f"/api/templates/{template['id']}/versions", headers=headers).json()
    assert len(versions) == 1
    assert versions[0]["immutable"] is True
    # Source preset still listed and its system template unchanged
    after = client.get("/api/presets", headers=headers).json()
    assert {p["id"] for p in after} == {p["id"] for p in presets}


def test_copy_unknown_preset_returns_404(authenticated):
    client, headers = authenticated
    response = client.post(
        "/api/presets/missing-preset/copy",
        headers=headers,
        json={},
    )
    assert response.status_code == 404


@pytest.mark.parametrize("name", ["", "   ", "bad/name", "x" * 121])
def test_copy_rejects_invalid_names_without_creating_partial_template(authenticated, name):
    client, headers = authenticated
    preset = client.get("/api/presets").json()[0]
    before = client.get("/api/templates").json()
    response = client.post(
        f"/api/presets/{preset['id']}/copy", headers=headers, json={"name": name}
    )
    assert response.status_code == 422, response.text
    assert client.get("/api/templates").json() == before


def test_copy_default_name_is_editable_and_requires_csrf(authenticated):
    client, headers = authenticated
    preset = client.get("/api/presets").json()[0]
    url = f"/api/presets/{preset['id']}/copy"
    assert client.post(url, json={}).status_code == 403
    response = client.post(url, json={}, headers=headers)
    assert response.status_code == 201, response.text
    template = response.json()
    response = client.patch(
        f"/api/templates/{template['id']}",
        headers=headers,
        json={"expected_version": template["version"], "name": template["name"]},
    )
    assert response.status_code == 200, response.text


def test_provenance_uses_effective_layers_and_infers_node_roles(authenticated, tmp_path):
    client, headers = authenticated
    project = _create_project(client, headers, tmp_path)
    connection = client.post(
        "/api/connections",
        headers=headers,
        json={"name": "layers", "base_url": "http://127.0.0.1:9/v1"},
    ).json()
    group_response = client.post(
        "/api/model_groups/llm",
        headers=headers,
        json={
            "name": "layer group",
            "members": [
                {"provider_connection_id": connection["id"], "model_id": "first"},
                {
                    "provider_connection_id": connection["id"],
                    "model_id": "second",
                    "enabled": False,
                },
            ],
        },
    )
    assert group_response.status_code == 201, group_response.text
    group = group_response.json()
    direct = {
        "kind": "direct",
        "provider_connection_id": connection["id"],
        "model_id": "node-model",
    }
    template = _create_template(client, headers, "layered")
    nodes = [
        {"id": "start", "type": "Start"},
        {"id": "review", "type": "LLMRequest", "config": {"role": "verifier", "prompt": "review"}},
        {
            "id": "override",
            "type": "LLMRequest",
            "config": {"role": "verifier", "prompt": "review", "model_selection": direct},
        },
        {"id": "end", "type": "End"},
    ]
    response = client.post(
        f"/api/templates/{template['id']}/versions",
        headers=headers,
        json={
            "graph": {
                "nodes": nodes,
                "edges": [
                    {"id": f"e{i}", "from": a["id"], "to": b["id"]}
                    for i, (a, b) in enumerate(zip(nodes, nodes[1:], strict=False))
                ],
            },
            "settings": {
                "model_selections": {"verifier": {"kind": "group", "group_id": group["id"]}},
                "limit_overrides": {"max_calls": 150, "max_node_visits": 80},
                "role_parameters": {"verifier": {"temperature": 0.3, "max_tokens": 100}},
            },
        },
    )
    assert response.status_code == 201, response.text
    binding = client.post(
        f"/api/versions/{response.json()['id']}/bindings",
        headers=headers,
        json={
            "project_id": project["id"],
            "name": "layers",
            "limit_overrides": {"max_calls": 200},
            "role_parameters": {"verifier": {"temperature": 0.4}},
        },
    ).json()
    response = client.post(
        f"/api/bindings/{binding['id']}/resolved",
        headers=headers,
        json={"limit_overrides": {"max_calls": 300}},
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["roles"]["verifier"]["kind"] == "llm"
    provenance = {item["name"]: item for item in result["provenance"]}
    assert len(provenance) == len(result["provenance"])
    assert provenance["limit_overrides.max_calls"]["source"] == "run"
    assert provenance["limit_overrides.max_calls"]["value"] == 300
    assert provenance["limit_overrides.max_node_visits"]["source"] == "template"
    assert provenance["dirty_policy"]["source"] == "default"
    assert provenance["role_parameters.verifier.temperature"]["source"] == "binding"
    assert provenance["role_parameters.verifier.max_tokens"]["source"] == "template"
    assert provenance["role.verifier"]["source"] == "template"
    assert provenance["node.review.candidates"]["source"] == "template"
    assert [item["model_id"] for item in provenance["node.review.candidates"]["value"]] == [
        "first",
        "second",
    ]
    assert provenance["node.review.candidates"]["value"][1]["enabled"] is False
    assert provenance["node.override.candidates"]["source"] == "node"
    assert provenance["node.override.candidates"]["value"][0]["model_id"] == "node-model"
