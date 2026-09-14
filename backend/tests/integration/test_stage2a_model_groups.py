"""Stage 2A integration tests: model groups, priorities, snapshots and policy."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from agents_ide.persistence.models import Run as RunModel


def _project(client, headers, workspace: Path) -> dict[str, object]:
    response = client.post(
        "/api/projects",
        json={"name": workspace.name, "workspace_path": str(workspace)},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _harness_profile(client, headers, name: str, kind: str = "codex") -> dict[str, object]:
    response = client.post(
        "/api/harness_profiles",
        json={"name": name, "harness_kind": kind, "settings": {"model": "default"}},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _provider(client, headers, name: str) -> dict[str, object]:
    response = client.post(
        "/api/connections",
        json={"name": name, "base_url": "https://example.invalid"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _make_template(client, headers, name: str) -> tuple[dict[str, object], dict[str, object]]:
    template = client.post(
        "/api/templates", json={"name": name, "schema_version": "1.0.0"}, headers=headers
    ).json()
    version = client.post(
        f"/api/templates/{template['id']}/versions",
        json={
            "graph": {
                "nodes": [
                    {"id": "start", "type": "Start"},
                    {
                        "id": "agent",
                        "type": "AgentTask",
                        "config": {"role": "dev", "prompt": "fix"},
                    },
                    {"id": "end", "type": "End"},
                ],
                "edges": [],
            }
        },
        headers=headers,
    ).json()
    return template, version


def test_agent_group_create_lists_and_ordering(authenticated, tmp_path):
    client, headers = authenticated
    profile = _harness_profile(client, headers, "p-codex")
    payload = {
        "name": "heavy",
        "description": " heavy group",
        "members": [
            {
                "harness_profile_id": profile["id"],
                "model_id": "gpt-plan-a",
                "params": {"reasoning_effort": "high"},
            },
            {"harness_profile_id": profile["id"], "model_id": "gpt-plan-b"},
        ],
    }
    response = client.post("/api/model_groups/agent", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["kind"] == "agent"
    assert created["name"] == "heavy"
    assert [member["member_index"] for member in created["members"]] == [0, 1]
    assert created["members"][0]["model_id"] == "gpt-plan-a"
    assert created["members"][0]["params"]["reasoning_effort"] == "high"

    listed = client.get("/api/model_groups", headers=headers).json()
    assert [item["id"] for item in listed] == [created["id"]]

    listed_agent = client.get("/api/model_groups?kind=agent", headers=headers).json()
    assert listed_agent == listed

    listed_other = client.get("/api/model_groups?kind=llm", headers=headers).json()
    assert listed_other == []


def test_llm_group_requires_provider_and_agent_group_requires_profile(authenticated):
    client, headers = authenticated
    response = client.post(
        "/api/model_groups/llm",
        json={
            "name": "heavy-llm",
            "members": [{"model_id": "m", "provider_connection_id": "missing"}],
        },
        headers=headers,
    )
    assert response.status_code == 409
    assert response.json()["code"] == "connection_unavailable"

    response = client.post(
        "/api/model_groups/agent",
        json={"name": "bad", "members": [{"model_id": "m", "harness_profile_id": "missing"}]},
        headers=headers,
    )
    assert response.status_code == 409
    assert response.json()["code"] == "harness_unavailable"


def test_agent_and_llm_groups_can_share_name(authenticated):
    client, headers = authenticated
    profile = _harness_profile(client, headers, "p2-codex")
    provider = _provider(client, headers, "p2-conn")
    first = client.post(
        "/api/model_groups/agent",
        json={
            "name": "heavy",
            "members": [{"harness_profile_id": profile["id"], "model_id": "m"}],
        },
        headers=headers,
    )
    assert first.status_code == 201
    second = client.post(
        "/api/model_groups/llm",
        json={
            "name": "heavy",
            "members": [{"provider_connection_id": provider["id"], "model_id": "m"}],
        },
        headers=headers,
    )
    assert second.status_code == 201
    assert second.json()["kind"] == "llm"


def test_duplicate_name_within_kind_is_rejected(authenticated):
    client, headers = authenticated
    profile = _harness_profile(client, headers, "p3-codex")
    first = client.post(
        "/api/model_groups/agent",
        json={
            "name": "flash",
            "members": [{"harness_profile_id": profile["id"], "model_id": "m"}],
        },
        headers=headers,
    )
    assert first.status_code == 201
    second = client.post(
        "/api/model_groups/agent",
        json={
            "name": "flash",
            "members": [{"harness_profile_id": profile["id"], "model_id": "m2"}],
        },
        headers=headers,
    )
    assert second.status_code == 409


def test_duplicate_profile_and_model_in_group_is_rejected(authenticated):
    client, headers = authenticated
    profile = _harness_profile(client, headers, "p4-codex")
    response = client.post(
        "/api/model_groups/agent",
        json={
            "name": "duplicate",
            "members": [
                {"harness_profile_id": profile["id"], "model_id": "m"},
                {"harness_profile_id": profile["id"], "model_id": "m"},
            ],
        },
        headers=headers,
    )
    assert response.status_code == 422
    assert response.json()["code"] == "model_group_duplicate"


def test_group_member_mixed_kinds_rejected_by_schema(authenticated):
    client, headers = authenticated
    profile = _harness_profile(client, headers, "p5-codex")
    provider = _provider(client, headers, "p5-conn")
    response = client.post(
        "/api/model_groups/agent",
        json={
            "name": "mixed",
            "members": [
                {
                    "harness_profile_id": profile["id"],
                    "provider_connection_id": provider["id"],
                    "model_id": "m",
                }
            ],
        },
        headers=headers,
    )
    assert response.status_code == 422


def test_group_update_revision_conflict(authenticated):
    client, headers = authenticated
    profile = _harness_profile(client, headers, "p6-codex")
    response = client.post(
        "/api/model_groups/agent",
        json={
            "name": "editable",
            "members": [{"harness_profile_id": profile["id"], "model_id": "m1"}],
        },
        headers=headers,
    )
    group = response.json()
    response = client.patch(
        f"/api/model_groups/{group['id']}/agent",
        json={"name": "renamed", "expected_revision": 1},
        headers=headers,
    )
    assert response.status_code == 200
    response = client.patch(
        f"/api/model_groups/{group['id']}/agent",
        json={"name": "renamed-again", "expected_revision": 1},
        headers=headers,
    )
    assert response.status_code == 409
    assert response.json()["code"] == "version_conflict"


def test_replace_members_renumbers_and_enables(authenticated):
    client, headers = authenticated
    profile = _harness_profile(client, headers, "p7-codex")
    response = client.post(
        "/api/model_groups/agent",
        json={
            "name": "swap",
            "members": [
                {"harness_profile_id": profile["id"], "model_id": "m1", "enabled": False},
                {"harness_profile_id": profile["id"], "model_id": "m2"},
            ],
        },
        headers=headers,
    )
    group = response.json()
    response = client.put(
        f"/api/model_groups/{group['id']}/agent/members",
        json={
            "expected_revision": group["revision"],
            "members": [
                {"harness_profile_id": profile["id"], "model_id": "m3"},
                {"harness_profile_id": profile["id"], "model_id": "m4"},
                {"harness_profile_id": profile["id"], "model_id": "m5"},
            ],
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert [member["member_index"] for member in payload["members"]] == [0, 1, 2]
    assert all(member["enabled"] for member in payload["members"])


def test_delete_member_keeps_at_least_one_enabled(authenticated):
    client, headers = authenticated
    profile = _harness_profile(client, headers, "p8-codex")
    response = client.post(
        "/api/model_groups/agent",
        json={
            "name": "shrink",
            "members": [
                {"harness_profile_id": profile["id"], "model_id": "a"},
                {"harness_profile_id": profile["id"], "model_id": "b"},
            ],
        },
        headers=headers,
    )
    group = response.json()
    first_member = group["members"][0]["id"]
    second_member = group["members"][1]["id"]
    response = client.delete(
        f"/api/model_groups/{group['id']}/members/{second_member}",
        params={"expected_revision": group["revision"]},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    response = client.delete(
        f"/api/model_groups/{group['id']}/members/{first_member}",
        params={"expected_revision": response.json()["revision"]},
        headers=headers,
    )
    assert response.status_code == 422
    assert response.json()["code"] == "model_group_empty"


def test_archive_and_copy_preserve_kind_and_member_order(authenticated):
    client, headers = authenticated
    profile = _harness_profile(client, headers, "p9-codex")
    response = client.post(
        "/api/model_groups/agent",
        json={
            "name": "preserve",
            "members": [
                {"harness_profile_id": profile["id"], "model_id": "first"},
                {"harness_profile_id": profile["id"], "model_id": "second"},
            ],
        },
        headers=headers,
    )
    group = response.json()
    response = client.post(
        f"/api/model_groups/{group['id']}/copy",
        json={"name": "preserve-copy", "expected_revision": group["revision"]},
        headers=headers,
    )
    assert response.status_code == 201
    copied = response.json()
    assert copied["kind"] == "agent"
    assert [member["model_id"] for member in copied["members"]] == ["first", "second"]

    response = client.post(
        f"/api/model_groups/{group['id']}/archive",
        params={"expected_revision": group["revision"]},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["archived"] is True
    listed = client.get("/api/model_groups", headers=headers).json()
    assert [item["id"] for item in listed] == [copied["id"]]

    listed_archived = client.get("/api/model_groups?include_archived=true", headers=headers).json()
    assert {item["id"] for item in listed_archived} == {group["id"], copied["id"]}


def test_run_snapshot_pins_group_members_and_survives_edits(authenticated, tmp_path):
    client, headers = authenticated
    workspace = tmp_path / "ws"
    workspace.mkdir()
    project = _project(client, headers, workspace)
    profile = _harness_profile(client, headers, "snap-codex")
    provider = _provider(client, headers, "snap-conn")
    _template, version = _make_template(client, headers, "snap-tpl")
    group = client.post(
        "/api/model_groups/agent",
        json={
            "name": "snap",
            "members": [
                {"harness_profile_id": profile["id"], "model_id": "primary"},
                {"harness_profile_id": profile["id"], "model_id": "backup"},
            ],
        },
        headers=headers,
    ).json()
    binding = client.post(
        f"/api/versions/{version['id']}/bindings",
        json={
            "project_id": project["id"],
            "name": "snap-binding",
            "role_assignments": {"dev": profile["id"]},
            "model_selections": {
                "dev": {"kind": "group", "group_id": group["id"]},
                "verifier": {
                    "kind": "direct",
                    "model_id": "m",
                    "provider_connection_id": provider["id"],
                },
            },
        },
        headers=headers,
    ).json()
    run = client.post(
        "/api/runs",
        json={
            "project_id": project["id"],
            "binding_id": binding["id"],
            "message": "task",
            "idempotency_key": "snap-key",
        },
        headers=headers,
    ).json()
    assert run["state"] == "queued"
    with client.app.state.session_factory() as session:
        run_model = session.scalar(select(RunModel).where(RunModel.id == run["id"]))
        snapshot = json.loads(run_model.snapshot_json)
    pinned = snapshot["dependencies"]["model_groups"][group["id"]]
    assert pinned["revision"] == group["revision"]
    assert [member["model_id"] for member in pinned["members"]] == ["primary", "backup"]
    assert pinned["members"][0]["params"] == {}

    response = client.put(
        f"/api/model_groups/{group['id']}/agent/members",
        json={
            "expected_revision": group["revision"],
            "members": [{"harness_profile_id": profile["id"], "model_id": "replaced"}],
        },
        headers=headers,
    )
    assert response.status_code == 200
    new_group = response.json()
    assert [member["model_id"] for member in new_group["members"]] == ["replaced"]
    with client.app.state.session_factory() as session:
        run_model = session.scalar(select(RunModel).where(RunModel.id == run["id"]))
        snapshot_after = json.loads(run_model.snapshot_json)
    assert snapshot_after == snapshot


def test_run_snapshot_uses_direct_selection_for_llm_role(authenticated, tmp_path):
    client, headers = authenticated
    workspace = tmp_path / "ws2"
    workspace.mkdir()
    project = _project(client, headers, workspace)
    provider = _provider(client, headers, "direct-llm")
    profile = _harness_profile(client, headers, "direct-codex")
    _template, version = _make_template(client, headers, "direct-tpl")
    binding = client.post(
        f"/api/versions/{version['id']}/bindings",
        json={
            "project_id": project["id"],
            "name": "direct-binding",
            "role_assignments": {"dev": profile["id"]},
            "model_selections": {
                "dev": {
                    "kind": "direct",
                    "model_id": "gpt-direct",
                    "harness_profile_id": profile["id"],
                },
                "verifier": {
                    "kind": "direct",
                    "model_id": "llm-direct",
                    "provider_connection_id": provider["id"],
                },
            },
        },
        headers=headers,
    ).json()
    run = client.post(
        "/api/runs",
        json={
            "project_id": project["id"],
            "binding_id": binding["id"],
            "message": "task",
            "idempotency_key": "direct-key",
        },
        headers=headers,
    )
    assert run.status_code == 201, run.text
    with client.app.state.session_factory() as session:
        snapshot = json.loads(
            session.scalar(select(RunModel).where(RunModel.id == run.json()["id"])).snapshot_json
        )
    nodes = snapshot["dependencies"]["nodes"]
    assert nodes["agent"]["model"] == "gpt-direct"
    assert nodes["agent"]["harness_profile_id"] == profile["id"]
    assert snapshot["dependencies"]["model_groups"] == {}


def test_run_start_rejects_archived_group(authenticated, tmp_path):
    client, headers = authenticated
    workspace = tmp_path / "ws3"
    workspace.mkdir()
    project = _project(client, headers, workspace)
    profile = _harness_profile(client, headers, "arch-codex")
    _template, version = _make_template(client, headers, "arch-tpl")
    group = client.post(
        "/api/model_groups/agent",
        json={
            "name": "archived-group",
            "members": [{"harness_profile_id": profile["id"], "model_id": "m"}],
        },
        headers=headers,
    ).json()
    binding = client.post(
        f"/api/versions/{version['id']}/bindings",
        json={
            "project_id": project["id"],
            "name": "uses-archived",
            "role_assignments": {"dev": profile["id"]},
            "model_selections": {
                "dev": {"kind": "group", "group_id": group["id"]},
            },
        },
        headers=headers,
    ).json()
    client.post(
        f"/api/model_groups/{group['id']}/archive",
        params={"expected_revision": group["revision"]},
        headers=headers,
    )
    response = client.post(
        "/api/runs",
        json={
            "project_id": project["id"],
            "binding_id": binding["id"],
            "message": "task",
            "idempotency_key": "archived-key",
        },
        headers=headers,
    )
    assert response.status_code == 409
    assert response.json()["code"] == "model_group_unavailable"


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "direct", "model_id": "m"},
        {
            "kind": "direct",
            "model_id": "m",
            "harness_profile_id": "x",
            "provider_connection_id": "y",
        },
        {"kind": "group", "group_id": "x", "model_id": "m"},
        {"kind": "group"},
        {"kind": "direct"},
    ],
)
def test_model_selection_payload_validation(authenticated, payload):
    client, headers = authenticated
    template = client.post(
        "/api/templates", json={"name": "validation-tpl"}, headers=headers
    ).json()
    response = client.post(
        f"/api/templates/{template['id']}/versions",
        json={
            "graph": {"nodes": [{"id": "start", "type": "Start"}], "edges": []},
            "settings": {"model_selections": {"dev": payload}},
        },
        headers=headers,
    )
    assert response.status_code == 422, response.text


def test_run_overrides_selection_isolated_per_role(authenticated, tmp_path):
    client, headers = authenticated
    workspace = tmp_path / "ws4"
    workspace.mkdir()
    project = _project(client, headers, workspace)
    provider = _provider(client, headers, "override-llm")
    _template, version = _make_template(client, headers, "override-tpl")
    binding = client.post(
        f"/api/versions/{version['id']}/bindings",
        json={
            "project_id": project["id"],
            "name": "override-binding",
            "model_selections": {
                "verifier": {
                    "kind": "direct",
                    "model_id": "binding",
                    "provider_connection_id": provider["id"],
                },
            },
        },
        headers=headers,
    ).json()
    response = client.post(
        "/api/runs",
        json={
            "project_id": project["id"],
            "binding_id": binding["id"],
            "message": "task",
            "idempotency_key": "override-key",
            "overrides": {
                "model_selections": {
                    "verifier": {
                        "kind": "direct",
                        "model_id": "run",
                        "provider_connection_id": provider["id"],
                    },
                }
            },
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text


def test_binding_rejects_overlap_between_selections_and_overrides(authenticated, tmp_path):
    client, headers = authenticated
    workspace = tmp_path / "ws5"
    workspace.mkdir()
    project = _project(client, headers, workspace)
    profile = _harness_profile(client, headers, "overlap-codex")
    _template, version = _make_template(client, headers, "overlap-tpl")
    response = client.post(
        f"/api/versions/{version['id']}/bindings",
        json={
            "project_id": project["id"],
            "name": "overlap-binding",
            "role_assignments": {"dev": profile["id"]},
            "model_selections": {
                "dev": {
                    "kind": "direct",
                    "model_id": "binding",
                    "harness_profile_id": profile["id"],
                },
            },
            "model_overrides": {"dev": "binding"},
        },
        headers=headers,
    )
    assert response.status_code == 422


def test_schema_capabilities_include_model_groups(authenticated):
    client, headers = authenticated
    response = client.get("/api/schema/events", headers=headers).json()
    assert "model_group.candidate_selected" in response["events"]
    assert "model_group.exhausted" in response["events"]
    response = client.get("/api/capabilities", headers=headers).json()
    assert "model_groups" in response["features"]
    response = client.get("/api/schema/nodes", headers=headers).json()
    assert "AgentTask" in response["nodes"]


def test_empty_group_is_rejected(authenticated):
    client, headers = authenticated
    response = client.post(
        "/api/model_groups/agent",
        json={"name": "empty", "members": []},
        headers=headers,
    )
    assert response.status_code == 422
    assert response.json()["code"] == "model_group_empty"
