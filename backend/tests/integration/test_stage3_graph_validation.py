"""Stage 3: graph schema, AST, validation and preflight contracts."""

from __future__ import annotations

import pytest

# --------------------------------------------------------------------------- helpers


def _graph(nodes, edges=None, *, inputs=None):
    body = {
        "graph": {
            "nodes": nodes,
            "edges": edges or [],
        }
    }
    if inputs is not None:
        body["inputs"] = inputs
    return body


def _start_end(start_id="start", end_id="end"):
    return [
        {"id": start_id, "type": "Start"},
        {"id": end_id, "type": "End"},
    ]


# --------------------------------------------------------------------------- validation endpoint


def test_validate_endpoint_accepts_two_backward_edges(authenticated):
    from tests.integration.test_stage3_review import loop_graph

    client, headers = authenticated
    response = client.post("/api/graphs/validate", headers=headers, json={"graph": loop_graph()})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["errors"] == []
    assert body["graph_hash"]


def test_validate_endpoint_reports_missing_start_and_end(authenticated):
    client, headers = authenticated
    response = client.post(
        "/api/graphs/validate",
        headers=headers,
        json={
            "graph": {
                "nodes": [
                    {
                        "id": "do",
                        "type": "AgentTask",
                        "config": {"role": "dev", "prompt": "fix"},
                    }
                ],
                "edges": [],
            }
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    codes = sorted(issue["code"] for issue in body["errors"])
    assert codes == ["end_missing", "start_missing"]


def test_validate_endpoint_reports_invalid_node_id(authenticated):
    client, headers = authenticated
    response = client.post(
        "/api/graphs/validate",
        headers=headers,
        json={
            "graph": {
                "nodes": [
                    {"id": "1bad", "type": "Start"},
                    {"id": "ok", "type": "End"},
                ],
                "edges": [{"id": "e1", "from": "1bad", "to": "ok"}],
            }
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert any(issue["code"] == "node_id_invalid" for issue in body["errors"])


def test_validate_endpoint_reports_ambiguous_condition_reference(authenticated):
    client, headers = authenticated
    expression = {
        "op": "eq",
        "left": {"ref": "steps.unknown_node.latest.decision"},
        "right": {"const": True},
    }
    response = client.post(
        "/api/graphs/validate",
        headers=headers,
        json={
            "graph": {
                "nodes": [
                    {"id": "s", "type": "Start"},
                    {
                        "id": "cond",
                        "type": "Condition",
                        "expression": expression,
                    },
                    {"id": "e", "type": "End"},
                ],
                "edges": [
                    {"id": "e1", "from": "s", "to": "cond"},
                    {"id": "e2", "from": "cond", "to": "e"},
                ],
            }
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert any(issue["code"] == "condition_reference_unknown" for issue in body["errors"])


def test_validate_endpoint_rejects_ambiguous_unbounded_cycle(authenticated):
    client, headers = authenticated
    response = client.post(
        "/api/graphs/validate",
        headers=headers,
        json={
            "graph": {
                "nodes": [
                    {"id": "s", "type": "Start"},
                    {
                        "id": "a",
                        "type": "Command",
                        "config": {
                            "commands": [
                                {
                                    "id": "noop",
                                    "program": "true",
                                    "args": [],
                                    "success_exit_codes": [0],
                                }
                            ]
                        },
                    },
                    {"id": "e", "type": "End"},
                ],
                "edges": [
                    {"id": "e1", "from": "s", "to": "a"},
                    {"id": "e2", "from": "a", "to": "a"},
                    {"id": "e3", "from": "a", "to": "e"},
                ],
            }
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert any(issue["code"] == "cycle_unbounded" for issue in body["errors"])


def test_validate_endpoint_reports_unreachable_end(authenticated):
    client, headers = authenticated
    response = client.post(
        "/api/graphs/validate",
        headers=headers,
        json={
            "graph": {
                "nodes": [
                    {"id": "s", "type": "Start"},
                    {"id": "a", "type": "AgentTask", "config": {"role": "dev", "prompt": "x"}},
                    {"id": "orphan", "type": "End"},
                ],
                "edges": [{"id": "e1", "from": "s", "to": "a"}],
            }
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert any(issue["code"] == "end_unreachable" for issue in body["errors"])


def test_validate_endpoint_warns_on_unsupported_model_parameter(authenticated):
    client, headers = authenticated
    response = client.post(
        "/api/graphs/validate",
        headers=headers,
        json={
            "graph": {
                "nodes": [
                    {"id": "s", "type": "Start"},
                    {
                        "id": "llm",
                        "type": "LLMRequest",
                        "config": {
                            "connection_id": "ignored",
                            "model": "ignored",
                            "prompt": "x",
                            "params": {"model_id": "override"},
                        },
                    },
                    {"id": "e", "type": "End"},
                ],
                "edges": [
                    {"id": "e1", "from": "s", "to": "llm"},
                    {"id": "e2", "from": "llm", "to": "e"},
                ],
            }
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert any("параметр" in str(issue["message"]).lower() for issue in body["errors"])


def test_validate_endpoint_collects_required_features(authenticated):
    client, headers = authenticated
    response = client.post(
        "/api/graphs/validate",
        headers=headers,
        json=_graph(
            [
                {"id": "s", "type": "Start"},
                {
                    "id": "collect",
                    "type": "CollectContext",
                    "config": {"mode": "collect"},
                },
                {
                    "id": "commit",
                    "type": "GitCommit",
                },
                {"id": "e", "type": "End"},
            ],
            [
                {"id": "e1", "from": "s", "to": "collect"},
                {"id": "e2", "from": "collect", "to": "commit"},
                {"id": "e3", "from": "commit", "to": "e"},
            ],
        ),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert set(body["features"]) == {"collect_context", "git_commit"}


# --------------------------------------------------------------------------- AST behavior


def test_ast_evaluation_returns_unknown_for_missing_reference(authenticated):
    from agents_ide.domain.graph_ast import (
        ASTNode,
        EvaluationContext,
        TruthValue,
        evaluate_truth,
    )

    node = ASTNode.from_json(
        {
            "op": "eq",
            "left": {"ref": "steps.unknown.latest.decision"},
            "right": {"const": True},
        }
    )
    ctx = EvaluationContext(known_node_ids=frozenset({"unknown"}), latest={})
    assert evaluate_truth(node, ctx) == TruthValue.UNKNOWN


def test_ast_substitution_renders_missing_reference_strict(authenticated):
    from agents_ide.domain.graph_ast import (
        ASTError,
        EvaluationContext,
        substitute,
    )

    with pytest.raises(ASTError):
        substitute("hello {{ steps.unknown.latest.decision }}", EvaluationContext())


# --------------------------------------------------------------------------- import/export


def test_import_endpoint_ignores_trusted_field(authenticated):
    client, headers = authenticated
    graph = {
        "schema_version": "1.0.0",
        "graph": {
            "nodes": [
                {"id": "s", "type": "Start"},
                {"id": "e", "type": "End"},
            ],
            "edges": [{"id": "e1", "from": "s", "to": "e"}],
        },
        "inputs": {},
        "required_features": [],
        "trusted": True,
        "execution_hash": "deadbeef",
    }
    response = client.post("/api/graphs/import", headers=headers, json=graph)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["execution_hash"] != "deadbeef"
    assert "trusted" not in body["body"]
    assert "execution_hash" not in body["body"]


def test_export_endpoint_returns_server_hash(authenticated):
    client, headers = authenticated
    response = client.post(
        "/api/graphs/export",
        headers=headers,
        json=_graph(_start_end(), [{"id": "e1", "from": "start", "to": "end"}]),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["execution_hash"]
    assert body["schema_version"] == "1.0.0"


# --------------------------------------------------------------------------- preflight


def test_preflight_endpoint_succeeds_for_valid_binding(authenticated, tmp_path):
    client, headers = authenticated
    workspace = tmp_path / "ws-preflight-ok"
    workspace.mkdir()
    project = client.post(
        "/api/projects",
        headers=headers,
        json={"name": "p", "workspace_path": str(workspace)},
    ).json()
    profile = client.post(
        "/api/harness_profiles",
        headers=headers,
        json={"name": "profile", "harness_kind": "codex", "settings": {}},
    ).json()
    template = client.post("/api/templates", headers=headers, json={"name": "t"}).json()
    version = client.post(
        f"/api/templates/{template['id']}/versions",
        headers=headers,
        json={
            "graph": {
                "nodes": [
                    {"id": "s", "type": "Start"},
                    {
                        "id": "agent",
                        "type": "AgentTask",
                        "config": {
                            "role": "dev",
                            "prompt": "task",
                            "model_selection": {
                                "kind": "direct",
                                "model_id": "m",
                                "harness_profile_id": profile["id"],
                            },
                        },
                    },
                    {"id": "e", "type": "End"},
                ],
                "edges": [
                    {"id": "e1", "from": "s", "to": "agent"},
                    {"id": "e2", "from": "agent", "to": "e"},
                ],
            },
        },
    ).json()
    binding = client.post(
        f"/api/versions/{version['id']}/bindings",
        headers=headers,
        json={"project_id": project["id"], "name": "main"},
    ).json()
    response = client.post(f"/api/bindings/{binding['id']}/preflight", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["errors"] == []


def test_preflight_warns_when_group_candidate_is_archived(authenticated, tmp_path):
    client, headers = authenticated
    workspace = tmp_path / "ws-group"
    workspace.mkdir()
    project = client.post(
        "/api/projects",
        headers=headers,
        json={"name": "p", "workspace_path": str(workspace)},
    ).json()
    profile = client.post(
        "/api/harness_profiles",
        headers=headers,
        json={"name": "profile", "harness_kind": "codex", "settings": {}},
    ).json()
    profile_2 = client.post(
        "/api/harness_profiles",
        headers=headers,
        json={"name": "second", "harness_kind": "codex", "settings": {}},
    ).json()
    group = client.post(
        "/api/model_groups/agent",
        headers=headers,
        json={
            "name": "heavy",
            "members": [
                {"harness_profile_id": profile["id"], "model_id": "first"},
                {"harness_profile_id": profile_2["id"], "model_id": "second"},
            ],
        },
    ).json()
    archived = client.post(
        f"/api/harness_profiles/{profile['id']}/archive",
        headers=headers,
        params={"expected_version": 1},
    ).json()
    assert archived["archived"] is True
    template = client.post("/api/templates", headers=headers, json={"name": "t"}).json()
    version = client.post(
        f"/api/templates/{template['id']}/versions",
        headers=headers,
        json={
            "graph": {
                "nodes": [
                    {"id": "s", "type": "Start"},
                    {
                        "id": "agent",
                        "type": "AgentTask",
                        "config": {
                            "role": "dev",
                            "prompt": "task",
                            "model_selection": {
                                "kind": "group",
                                "group_id": group["id"],
                            },
                        },
                    },
                    {"id": "e", "type": "End"},
                ],
                "edges": [
                    {"id": "e1", "from": "s", "to": "agent"},
                    {"id": "e2", "from": "agent", "to": "e"},
                ],
            }
        },
    ).json()
    binding = client.post(
        f"/api/versions/{version['id']}/bindings",
        headers=headers,
        json={"project_id": project["id"], "name": "main"},
    ).json()
    response = client.post(f"/api/bindings/{binding['id']}/preflight", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert any(issue["code"] == "model_group_candidate_unavailable" for issue in body["warnings"])


# --------------------------------------------------------------------------- node schema


def test_node_schema_endpoint_includes_required_features(authenticated):
    client, headers = authenticated
    response = client.get("/api/schema/nodes", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == "1.0.0"
    for node_type in (
        "Start",
        "End",
        "AgentTask",
        "LLMRequest",
        "Command",
        "CollectContext",
        "GitCommit",
        "Condition",
        "PlanControl",
    ):
        assert node_type in body["nodes"]
    assert body["limits"]["max_nodes"] == 200
    assert body["limits"]["max_edges"] == 400
    assert body["limits"]["max_graph_bytes"] == 1048576
