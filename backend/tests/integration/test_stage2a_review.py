"""Regression checks for stage 2A contracts, persistence and resolution."""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from agents_ide.api.app import create_app
from agents_ide.persistence.database import SCHEMA_REVISION, check_database, migrate
from agents_ide.persistence.models import Run


def post(client, headers, path, payload):
    response = client.post("/api" + path, headers=headers, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def setup(client, headers, tmp_path, kind="agent", members=3):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    project = post(
        client,
        headers,
        "/projects",
        {
            "name": "project",
            "workspace_path": str(workspace),
        },
    )
    if kind == "agent":
        resource = post(
            client,
            headers,
            "/harness_profiles",
            {
                "name": "profile",
                "harness_kind": "codex",
                "settings": {"model": "profile-default", "reasoning_effort": "low"},
            },
        )
        ref = "harness_profile_id"
    else:
        resource = post(
            client,
            headers,
            "/connections",
            {
                "name": "provider",
                "base_url": "https://first.invalid/v1",
            },
        )
        ref = "provider_connection_id"
    group = post(
        client,
        headers,
        "/model_groups/" + kind,
        {
            "name": "heavy",
            "members": [
                {ref: resource["id"], "model_id": f"model-{i}", "params": {"temperature": 0.2}}
                for i in range(members)
            ],
        },
    )
    template = post(client, headers, "/templates", {"name": "template"})
    return project, resource, group, template


def version_binding(
    client,
    headers,
    project,
    template,
    selection,
    *,
    kind="agent",
    config=None,
    settings=None,
    binding_settings=None,
):
    version = post(
        client,
        headers,
        f"/templates/{template['id']}/versions",
        {
            "graph": {
                "nodes": [
                    {
                        "id": "work",
                        "type": "AgentTask" if kind == "agent" else "LLMRequest",
                        "config": {"role": "dev", "prompt": "task", **(config or {})},
                    }
                ],
                "edges": [],
            },
            "settings": settings or {},
        },
    )
    binding = post(
        client,
        headers,
        f"/versions/{version['id']}/bindings",
        {
            "project_id": project["id"],
            "name": "binding",
            "model_selections": {"dev": selection},
            **(binding_settings or {}),
        },
    )
    return version, binding


def start(client, headers, project, binding, key="run", overrides=None):
    return post(
        client,
        headers,
        "/runs",
        {
            "project_id": project["id"],
            "binding_id": binding["id"],
            "message": "task",
            "idempotency_key": key,
            "overrides": overrides or {},
        },
    )


def snapshot(client, run):
    with client.app.state.session_factory() as session:
        return json.loads(session.get(Run, run["id"]).snapshot_json)


def test_database_head_is_ready(authenticated):
    client, _ = authenticated
    assert client.get("/api/system/status").json()["database"] == "ready"


def test_binding_selection_patch(authenticated, tmp_path):
    client, headers = authenticated
    project, resource, group, template = setup(client, headers, tmp_path)
    _, binding = version_binding(
        client, headers, project, template, {"kind": "group", "group_id": group["id"]}
    )
    response = client.patch(
        f"/api/bindings/{binding['id']}",
        headers=headers,
        json={
            "expected_version": 1,
            "model_selections": {
                "dev": {
                    "kind": "direct",
                    "model_id": "direct",
                    "harness_profile_id": resource["id"],
                }
            },
        },
    )
    assert response.status_code == 200, response.text
    run = start(client, headers, project, binding)
    assert snapshot(client, run)["dependencies"]["nodes"]["work"]["model"] == "direct"


@pytest.mark.parametrize("kind", ["agent", "llm"])
def test_direct_selection_uses_its_own_resource(authenticated, tmp_path, kind):
    client, headers = authenticated
    project, resource, _, template = setup(client, headers, tmp_path, kind)
    ref = "harness_profile_id" if kind == "agent" else "provider_connection_id"
    _, binding = version_binding(
        client,
        headers,
        project,
        template,
        {"kind": "direct", ref: resource["id"], "model_id": "exact"},
        kind=kind,
    )
    dep = snapshot(client, start(client, headers, project, binding))["dependencies"]
    assert dep["nodes"]["work"]["model"] == "exact"
    assert resource["id"] in dep["harness_profiles" if kind == "agent" else "provider_connections"]


def test_group_pins_all_profiles_and_does_not_use_legacy_assignment(authenticated, tmp_path):
    client, headers = authenticated
    project, resource, group, template = setup(client, headers, tmp_path)
    _, binding = version_binding(
        client,
        headers,
        project,
        template,
        {"kind": "group", "group_id": group["id"]},
        binding_settings={"role_assignments": {"dev": "obsolete"}},
    )
    dep = snapshot(client, start(client, headers, project, binding))["dependencies"]
    assert dep["harness_profiles"][resource["id"]]["settings"]["reasoning_effort"] == "low"
    assert dep["nodes"]["work"]["model_group_id"] == group["id"]


def test_wrong_kind_binding_is_rejected(authenticated, tmp_path):
    client, headers = authenticated
    project, _, group, template = setup(client, headers, tmp_path, "llm")
    version = post(
        client,
        headers,
        f"/templates/{template['id']}/versions",
        {
            "graph": {
                "nodes": [{"id": "work", "type": "AgentTask", "config": {"role": "dev"}}],
                "edges": [],
            },
        },
    )
    response = client.post(
        f"/api/versions/{version['id']}/bindings",
        headers=headers,
        json={
            "name": "wrong",
            "project_id": project["id"],
            "model_selections": {"dev": {"kind": "group", "group_id": group["id"]}},
        },
    )
    assert response.status_code == 422, response.text


def test_replace_preserves_member_ids_and_copy_checks_revision(authenticated, tmp_path):
    client, headers = authenticated
    _, resource, group, _ = setup(client, headers, tmp_path)
    response = client.put(
        f"/api/model_groups/{group['id']}/agent/members",
        headers=headers,
        json={
            "expected_revision": 1,
            "members": [
                {"harness_profile_id": resource["id"], "model_id": item["model_id"]}
                for item in reversed(group["members"])
            ],
        },
    )
    assert response.status_code == 200, response.text
    assert [m["id"] for m in response.json()["members"]] == [
        m["id"] for m in reversed(group["members"])
    ]
    response = client.post(
        f"/api/model_groups/{group['id']}/copy",
        headers=headers,
        json={
            "name": "stale",
            "expected_revision": 1,
        },
    )
    assert response.status_code == 409


def test_wrong_member_route_is_atomic_422(authenticated, tmp_path):
    client, headers = authenticated
    _, _, group, _ = setup(client, headers, tmp_path)
    response = client.put(
        f"/api/model_groups/{group['id']}/llm/members",
        headers=headers,
        json={
            "expected_revision": 1,
            "members": [{"provider_connection_id": "missing", "model_id": "m"}],
        },
    )
    assert response.status_code == 422, response.text
    assert client.get(f"/api/model_groups/{group['id']}").json() == group


@pytest.mark.parametrize("params", [{"api_key": "private"}, {"permissions": {"write": "*"}}])
def test_candidate_params_cannot_store_credentials_or_widen_permissions(
    authenticated, tmp_path, params
):
    client, headers = authenticated
    _, resource, _, _ = setup(client, headers, tmp_path)
    response = client.post(
        "/api/model_groups/agent",
        headers=headers,
        json={
            "name": "unsafe",
            "members": [{"harness_profile_id": resource["id"], "model_id": "m", "params": params}],
        },
    )
    assert response.status_code == 422, response.text
    assert "private" not in response.text


def test_disabled_group_cannot_be_bound(authenticated, tmp_path):
    client, headers = authenticated
    project, resource, group, template = setup(client, headers, tmp_path)
    response = client.put(
        f"/api/model_groups/{group['id']}/agent/members",
        headers=headers,
        json={
            "expected_revision": 1,
            "members": [
                {"harness_profile_id": resource["id"], "model_id": "m", "enabled": False},
            ],
        },
    )
    # A group may be a disabled draft, but must never be usable by a new binding.
    if response.status_code == 422:
        return
    assert response.status_code == 200, response.text
    version = post(
        client,
        headers,
        f"/templates/{template['id']}/versions",
        {
            "graph": {"nodes": [], "edges": []},
        },
    )
    response = client.post(
        f"/api/versions/{version['id']}/bindings",
        headers=headers,
        json={
            "name": "disabled",
            "project_id": project["id"],
            "model_selections": {"dev": {"kind": "group", "group_id": group["id"]}},
        },
    )
    assert response.status_code in (409, 422)


def test_candidate_parameters_resolve_separately_and_do_not_merge_lists(authenticated, tmp_path):
    client, headers = authenticated
    project, resource, group, template = setup(client, headers, tmp_path)
    _, binding = version_binding(
        client,
        headers,
        project,
        template,
        {"kind": "group", "group_id": group["id"]},
        settings={"role_parameters": {"dev": {"reasoning_effort": "medium", "stop": ["template"]}}},
        binding_settings={
            "role_parameters": {"dev": {"reasoning_effort": "high", "stop": ["binding"]}}
        },
        config={"params": {"temperature": 0.8}},
    )
    run = start(
        client, headers, project, binding, overrides={"role_parameters": {"dev": {"stop": ["run"]}}}
    )
    data = snapshot(client, run)
    candidates = data["dependencies"]["nodes"]["work"]["candidates"]
    assert len(candidates) == 3
    for candidate in candidates:
        assert candidate["params"] == {
            "temperature": 0.8,
            "reasoning_effort": "high",
            "stop": ["run"],
        }
        assert candidate["parameter_sources"] == {
            "temperature": "node",
            "reasoning_effort": "role",
            "stop": "role",
        }
    assert data["dependencies"]["model_groups"][group["id"]]["members"][0]["params"] == {
        "temperature": 0.2
    }
    assert data["setting_sources"]["role_parameters.dev.stop"] == "run"
    assert data["required_features"] == ["model_groups"]


def test_selection_replaces_legacy_across_layers_in_both_directions(authenticated, tmp_path):
    client, headers = authenticated
    project, resource, group, template = setup(client, headers, tmp_path)
    _, binding = version_binding(
        client,
        headers,
        project,
        template,
        {"kind": "group", "group_id": group["id"]},
        settings={"model_overrides": {"dev": "old-template"}},
        binding_settings={"role_assignments": {"dev": resource["id"]}},
    )
    data = snapshot(client, start(client, headers, project, binding))
    assert data["resolved_settings"]["model_overrides"] == {}
    assert len(data["dependencies"]["nodes"]["work"]["candidates"]) == 3
    override = {"model_overrides": {"dev": "legacy-run"}}
    data = snapshot(client, start(client, headers, project, binding, "legacy", override))
    assert data["resolved_settings"]["model_selections"] == {}
    assert data["dependencies"]["model_groups"] == {}
    assert data["dependencies"]["nodes"]["work"]["model"] == "legacy-run"
    response = client.patch(
        f"/api/bindings/{binding['id']}",
        headers=headers,
        json={
            "expected_version": 1,
            "model_overrides": {"dev": "legacy-patch"},
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["model_selections"] == {}
    data = snapshot(client, start(client, headers, project, binding, "patched"))
    assert data["dependencies"]["nodes"]["work"]["model"] == "legacy-patch"


@pytest.mark.parametrize("kind", ["agent", "llm"])
def test_node_group_choice_without_role_works_for_single_request(authenticated, tmp_path, kind):
    client, headers = authenticated
    project, _, group, template = setup(client, headers, tmp_path, kind)
    version = post(
        client,
        headers,
        f"/templates/{template['id']}/versions",
        {
            "graph": {
                "nodes": [
                    {
                        "id": "single",
                        "type": "AgentTask" if kind == "agent" else "LLMRequest",
                        "config": {
                            "prompt": "single request",
                            "model_selection": {"kind": "group", "group_id": group["id"]},
                        },
                    }
                ],
                "edges": [],
            },
        },
    )
    assert version["required_features"] == ["model_groups"]
    binding = post(
        client,
        headers,
        f"/versions/{version['id']}/bindings",
        {
            "project_id": project["id"],
            "name": "single",
        },
    )
    data = snapshot(client, start(client, headers, project, binding))
    assert data["dependencies"]["nodes"]["single"]["model_group_id"] == group["id"]


def test_node_choice_overrides_role_as_whole(authenticated, tmp_path):
    client, headers = authenticated
    project, resource, group, template = setup(client, headers, tmp_path)
    _, binding = version_binding(
        client,
        headers,
        project,
        template,
        {"kind": "group", "group_id": group["id"]},
        config={
            "model_selection": {
                "kind": "direct",
                "harness_profile_id": resource["id"],
                "model_id": "node-direct",
            }
        },
    )
    data = snapshot(
        client,
        start(
            client, headers, project, binding, overrides={"model_overrides": {"dev": "run-legacy"}}
        ),
    )
    node = data["dependencies"]["nodes"]["work"]
    assert "model_group_id" not in node
    assert node["model"] == "node-direct"
    assert len(node["candidates"]) == 1


def test_archive_blocks_new_bindings_but_preserves_run_and_idempotency(authenticated, tmp_path):
    client, headers = authenticated
    project, _, group, template = setup(client, headers, tmp_path)
    version, binding = version_binding(
        client, headers, project, template, {"kind": "group", "group_id": group["id"]}
    )
    run = start(client, headers, project, binding)
    before = snapshot(client, run)
    response = client.post(
        f"/api/model_groups/{group['id']}/archive", headers=headers, params={"expected_revision": 1}
    )
    assert response.status_code == 200
    assert response.json()["revision"] == 2
    assert snapshot(client, run) == before
    assert start(client, headers, project, binding) == run
    response = client.post(
        f"/api/versions/{version['id']}/bindings",
        headers=headers,
        json={
            "project_id": project["id"],
            "name": "archived",
            "model_selections": {"dev": {"kind": "group", "group_id": group["id"]}},
        },
    )
    assert response.status_code == 409
    assert response.json()["code"] == "model_group_unavailable"


def test_group_and_profile_changes_invalidate_execution_hash(authenticated, tmp_path):
    client, headers = authenticated
    project, resource, group, template = setup(client, headers, tmp_path)
    _, binding = version_binding(
        client, headers, project, template, {"kind": "group", "group_id": group["id"]}
    )
    first = start(client, headers, project, binding)
    before = snapshot(client, first)
    preview = client.get(f"/api/bindings/{binding['id']}/resolved").json()
    assert preview["execution_hash"] == first["execution_hash"]
    assert preview["policy_hash"] == first["policy_hash"]
    assert (
        client.patch(
            f"/api/harness_profiles/{resource['id']}",
            headers=headers,
            json={"expected_version": 1, "settings": {"reasoning_effort": "high"}},
        ).status_code
        == 200
    )
    second = start(client, headers, project, binding, "second")
    assert second["execution_hash"] != first["execution_hash"]
    members = [
        {"id": m["id"], "harness_profile_id": resource["id"], "model_id": m["model_id"]}
        for m in reversed(group["members"])
    ]
    assert (
        client.put(
            f"/api/model_groups/{group['id']}/agent/members",
            headers=headers,
            json={"expected_revision": 1, "members": members},
        ).status_code
        == 200
    )
    third = start(client, headers, project, binding, "third")
    assert third["execution_hash"] != second["execution_hash"]
    assert snapshot(client, first) == before


def test_unavailable_first_profile_does_not_hide_other_candidates(authenticated, tmp_path):
    client, headers = authenticated
    project, resource, group, template = setup(client, headers, tmp_path)
    other = post(
        client,
        headers,
        "/harness_profiles",
        {"name": "other", "harness_kind": "opencode", "settings": {"reasoning_effort": "high"}},
    )
    response = client.put(
        f"/api/model_groups/{group['id']}/agent/members",
        headers=headers,
        json={
            "expected_revision": 1,
            "members": [
                {"harness_profile_id": resource["id"], "model_id": "one"},
                {"harness_profile_id": other["id"], "model_id": "two"},
            ],
        },
    )
    assert response.status_code == 200
    assert (
        client.post(
            f"/api/harness_profiles/{resource['id']}/archive",
            headers=headers,
            params={"expected_version": 1},
        ).status_code
        == 200
    )
    _, binding = version_binding(
        client, headers, project, template, {"kind": "group", "group_id": group["id"]}
    )
    data = snapshot(client, start(client, headers, project, binding))
    candidates = data["dependencies"]["nodes"]["work"]["candidates"]
    assert candidates[0]["unavailable_reason"] == "archived"
    assert candidates[1]["unavailable_reason"] is None
    assert candidates[0]["params"]["reasoning_effort"] == "low"
    assert candidates[1]["params"]["reasoning_effort"] == "high"


@pytest.mark.parametrize("kind", ["agent", "llm"])
def test_export_import_requires_bindings_and_never_overwrites(authenticated, tmp_path, kind):
    client, headers = authenticated
    _, resource, group, _ = setup(client, headers, tmp_path, kind)
    definition = client.get(f"/api/model_groups/{group['id']}/export").json()
    serialized = json.dumps(definition)
    assert (
        "secret" not in serialized and "base_url" not in serialized and "settings" not in serialized
    )
    assert (
        client.post(
            "/api/model_groups/import",
            headers=headers,
            json={
                "definition": definition,
                "resource_bindings": {},
            },
        ).status_code
        == 422
    )
    payload = {"definition": definition, "resource_bindings": {resource["id"]: resource["id"]}}
    assert client.post("/api/model_groups/import", headers=headers, json=payload).status_code == 409
    assert client.get(f"/api/model_groups/{group['id']}").json() == group
    imported = post(client, headers, "/model_groups/import", {**payload, "name": "imported"})
    assert imported["id"] != group["id"]
    assert [m["model_id"] for m in imported["members"]] == [m["model_id"] for m in group["members"]]
    settings = client.app.state.settings
    with TestClient(create_app(settings), base_url=settings.origin) as restarted:
        restarted.cookies.update(client.cookies)
        assert restarted.get(f"/api/model_groups/{imported['id']}").json() == imported
    payload["definition"] = {**definition, "schema_version": "2.0.0"}
    assert client.post("/api/model_groups/import", headers=headers, json=payload).status_code == 422


def test_concurrent_reorder_has_one_revision_winner(authenticated, tmp_path):
    client, headers = authenticated
    _, resource, group, _ = setup(client, headers, tmp_path)
    members = [
        {
            "id": m["id"],
            "harness_profile_id": resource["id"],
            "model_id": m["model_id"],
            "params": m["params"],
        }
        for m in reversed(group["members"])
    ]

    def update(_):
        return client.put(
            f"/api/model_groups/{group['id']}/agent/members",
            headers=headers,
            json={"expected_revision": 1, "members": members},
        ).status_code

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sorted(pool.map(update, range(4))) == [200, 409, 409, 409]
    current = client.get(f"/api/model_groups/{group['id']}").json()
    assert current["revision"] == 2
    assert [m["id"] for m in current["members"]] == [m["id"] for m in reversed(group["members"])]
    assert all(m["revision"] == (1 if i == 1 else 2) for i, m in enumerate(current["members"]))


def test_delete_first_candidate_keeps_dense_unique_positions(authenticated, tmp_path):
    client, headers = authenticated
    _, _, group, _ = setup(client, headers, tmp_path, members=8)
    response = client.delete(
        f"/api/model_groups/{group['id']}/members/{group['members'][0]['id']}",
        headers=headers,
        params={"expected_revision": 1},
    )
    assert response.status_code == 200, response.text
    assert [m["member_index"] for m in response.json()["members"]] == list(range(7))
    assert [m["id"] for m in response.json()["members"]] == [m["id"] for m in group["members"][1:]]


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE model_group_members SET provider_connection_id='other' WHERE id=:id",
        "UPDATE model_group_members SET harness_profile_id=NULL WHERE id=:id",
        "UPDATE model_group_members SET model_id='model-1' WHERE id=:id",
        "UPDATE model_group_members SET member_index=-1 WHERE id=:id",
        "UPDATE model_groups SET kind='llm' WHERE id=:group_id",
    ],
)
def test_sql_invariants_reject_invalid_updates(authenticated, tmp_path, mutation):
    client, headers = authenticated
    _, _, group, _ = setup(client, headers, tmp_path)
    with pytest.raises(IntegrityError), client.app.state.engine.begin() as connection:
        connection.exec_driver_sql(
            mutation, {"id": group["members"][0]["id"], "group_id": group["id"]}
        )
    assert client.get(f"/api/model_groups/{group['id']}").json() == group


def test_migration_keeps_existing_groups_and_run_snapshots(authenticated, tmp_path):
    client, headers = authenticated
    project, _, group, template = setup(client, headers, tmp_path)
    _, binding = version_binding(
        client, headers, project, template, {"kind": "group", "group_id": group["id"]}
    )
    run = start(client, headers, project, binding)
    before = snapshot(client, run)
    config = Config()
    config.set_main_option(
        "script_location",
        str(Path(__file__).resolve().parents[2] / "src/agents_ide/persistence/migrations"),
    )
    with client.app.state.engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "0004_model_groups")
    assert not check_database(client.app.state.engine)
    migrate(client.app.state.settings)
    assert check_database(client.app.state.engine)
    assert client.get(f"/api/model_groups/{group['id']}").json() == group
    assert snapshot(client, run) == before
    with client.app.state.engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert (
            connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar()
            == SCHEMA_REVISION
        )


@pytest.mark.skipif(os.name != "nt", reason="Requires real Windows DPAPI")
def test_llm_group_pins_secret_refs_for_every_candidate(authenticated, tmp_path):
    client, headers = authenticated
    project, resource, group, template = setup(client, headers, tmp_path, "llm")
    assert (
        client.patch(
            f"/api/connections/{resource['id']}",
            headers=headers,
            json={"expected_version": 1, "secret": "review-first-key"},
        ).status_code
        == 200
    )
    other = post(
        client,
        headers,
        "/connections",
        {"name": "second", "base_url": "https://second.invalid", "secret": "review-second-key"},
    )
    assert (
        client.put(
            f"/api/model_groups/{group['id']}/llm/members",
            headers=headers,
            json={
                "expected_revision": 1,
                "members": [
                    {"provider_connection_id": resource["id"], "model_id": "first"},
                    {"provider_connection_id": other["id"], "model_id": "second", "enabled": False},
                ],
            },
        ).status_code
        == 200
    )
    _, binding = version_binding(
        client, headers, project, template, {"kind": "group", "group_id": group["id"]}, kind="llm"
    )
    run = start(client, headers, project, binding)
    before = snapshot(client, run)
    providers = before["dependencies"]["provider_connections"]
    assert len(providers) == 2
    assert providers[other["id"]]["base_url"] == "https://second.invalid"
    reference = providers[resource["id"]]["secret_reference"]
    assert client.app.state.secrets.get(reference) == "review-first-key"
    assert "review-first-key" not in json.dumps(before)
    assert "review-second-key" not in json.dumps(before)
    assert (
        client.patch(
            f"/api/connections/{resource['id']}",
            headers=headers,
            json={
                "expected_version": 2,
                "secret": "review-replaced",
                "base_url": "https://changed.invalid",
            },
        ).status_code
        == 200
    )
    assert client.app.state.secrets.get(reference) == "review-first-key"
    assert snapshot(client, run) == before
    settings = client.app.state.settings
    with TestClient(create_app(settings), base_url=settings.origin) as restarted:
        restarted.cookies.update(client.cookies)
        assert restarted.get(f"/api/runs/{run['id']}").json() == run
        assert restarted.app.state.secrets.get(reference) == "review-first-key"


def test_model_group_routes_require_pairing_and_csrf(client, authenticated):
    _, headers = authenticated
    response = client.post("/api/model_groups/agent", json={"name": "denied", "members": []})
    assert response.status_code == 403
    client.cookies.clear()
    assert client.get("/api/model_groups").status_code == 401
    assert client.post("/api/model_groups/import", headers=headers, json={}).status_code == 401


def test_legacy_null_selection_fields_remain_readable(authenticated, tmp_path):
    client, headers = authenticated
    project, resource, _, template = setup(client, headers, tmp_path)
    _, binding = version_binding(
        client,
        headers,
        project,
        template,
        {
            "kind": "direct",
            "harness_profile_id": resource["id"],
            "model_id": "legacy",
        },
    )
    raw = {
        "dev": {
            "kind": "direct",
            "model_id": "legacy",
            "harness_profile_id": resource["id"],
            "group_id": None,
            "provider_connection_id": None,
        }
    }
    with client.app.state.engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE pipeline_bindings SET model_selections_json=?, settings_json=? WHERE id=?",
            (json.dumps(raw), json.dumps({"model_selections": raw}), binding["id"]),
        )
    response = client.get(f"/api/bindings/{binding['id']}")
    assert response.status_code == 200, response.text
    assert response.json()["model_selections"]["dev"]["model_id"] == "legacy"
    data = snapshot(client, start(client, headers, project, binding))
    assert data["dependencies"]["nodes"]["work"]["model"] == "legacy"
    assert data["dependencies"]["model_groups"] == {}


def test_legacy_credentials_in_member_params_are_not_exposed_or_exported(authenticated, tmp_path):
    client, headers = authenticated
    _, resource, group, _ = setup(client, headers, tmp_path)
    with client.app.state.engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE model_group_members SET params_json=? WHERE id=?",
            (json.dumps({"api_key": "legacy-sensitive-value"}), group["members"][0]["id"]),
        )
    for path in (f"/api/model_groups/{group['id']}", f"/api/model_groups/{group['id']}/export"):
        response = client.get(path)
        assert response.status_code == 422, response.text
        assert "legacy-sensitive-value" not in response.text
    response = client.put(
        f"/api/model_groups/{group['id']}/agent/members",
        headers=headers,
        json={
            "expected_revision": 1,
            "members": [{"harness_profile_id": resource["id"], "model_id": "repaired"}],
        },
    )
    assert response.status_code == 200, response.text
