"""Regression cases from the stage 3 contract review."""

from copy import deepcopy

import pytest

from agents_ide.domain.graph_ast import (
    ASTError,
    ASTNode,
    EvaluationContext,
    TruthValue,
    Value,
    evaluate_truth,
    substitute,
)
from agents_ide.domain.graph_validation import validate_graph


def linear(node=None):
    nodes = [{"id": "s", "type": "Start"}]
    if node:
        nodes.append(node)
    nodes.append({"id": "e", "type": "End"})
    return {
        "nodes": nodes,
        "edges": [
            {"id": f"edge{i}", "from": a["id"], "to": b["id"]}
            for i, (a, b) in enumerate(zip(nodes, nodes[1:], strict=False))
        ],
    }


@pytest.mark.parametrize("config", [{}, {"commands": []}, {"commands": "shell"}])
def test_command_schema_is_enforced(config):
    assert not validate_graph(linear({"id": "c", "type": "Command", "config": config})).ok


@pytest.mark.parametrize("extra", ["nodes", "edges"])
def test_non_object_entries_are_rejected(extra):
    graph = linear()
    graph[extra].append(7)
    assert not validate_graph(graph).ok


def test_disconnected_end_is_invalid_even_in_draft_report():
    graph = linear()
    graph["edges"] = []
    assert not validate_graph(graph).ok


def test_ordinary_node_cannot_fork():
    graph = linear()
    graph["nodes"].append({"id": "other", "type": "End"})
    graph["edges"].append({"id": "fork", "from": "s", "to": "other"})
    assert not validate_graph(graph).ok


@pytest.mark.parametrize("value,expected", [(None, TruthValue.TRUE), (False, TruthValue.TRUE)])
def test_exists_means_presence(value, expected):
    ctx = EvaluationContext(inputs={"x": Value.of(value)})
    assert (
        evaluate_truth(ASTNode.from_json({"op": "exists", "target": "inputs.x"}), ctx) == expected
    )


def test_exists_missing_is_false_but_bad_scope_is_error():
    ctx = EvaluationContext()
    assert (
        evaluate_truth(ASTNode.from_json({"op": "exists", "target": "inputs.x"}), ctx)
        == TruthValue.FALSE
    )
    with pytest.raises(ASTError):
        evaluate_truth(ASTNode.from_json({"op": "exists", "target": "secrets.key"}), ctx)


def test_incompatible_types_do_not_turn_into_false():
    with pytest.raises(ASTError):
        evaluate_truth(
            ASTNode.from_json({"op": "eq", "left": {"const": 1}, "right": {"const": "1"}}),
            EvaluationContext(),
        )


def test_missing_substitution_is_not_silently_deleted():
    with pytest.raises(ASTError):
        substitute("Task: {{ inputs.task }}", EvaluationContext())


def test_malformed_ast_is_reported_without_500(authenticated):
    client, headers = authenticated
    response = client.post(
        "/api/graphs/validate",
        headers=headers,
        json={"graph": linear({"id": "c", "type": "Condition", "expression": {"op": "not"}})},
    )
    assert response.status_code == 200
    assert not response.json()["ok"]


@pytest.mark.parametrize(
    "field,value", [("schema_version", "2.0.0"), ("required_features", ["unimplemented"])]
)
def test_import_rejects_unknown_version_or_feature(authenticated, field, value):
    client, headers = authenticated
    body = {"graph": linear(), field: value}
    response = client.post("/api/graphs/import", headers=headers, json=body)
    assert response.status_code == 200
    assert not response.json()["ok"]


def test_execution_hash_covers_assignments_but_not_labels():
    graph = linear()
    original = validate_graph(graph).graph_hash
    decorated = deepcopy(graph)
    decorated["edges"][0]["label"] = "Decoration"
    assert validate_graph(decorated).graph_hash == original
    graph["edges"][0]["assignments"] = {"work.mode": {"const": "repair"}}
    assert validate_graph(graph).graph_hash != original


def loop_graph():
    return {
        "nodes": [
            {"id": "s", "type": "Start"},
            {"id": "a", "type": "Condition", "expression": {"const": True}},
            {"id": "b", "type": "Condition", "expression": {"const": False}},
            {"id": "e", "type": "End"},
        ],
        "edges": [
            {"id": "enter", "from": "s", "to": "a"},
            {"id": "ab", "from": "a", "to": "b", "when": "true"},
            {"id": "ae", "from": "a", "to": "e", "when": "false"},
            {
                "id": "aa",
                "from": "a",
                "to": "a",
                "when": "unknown",
                "loop": {"id": "evidence", "max_iterations": 2},
            },
            {"id": "be", "from": "b", "to": "e", "when": "true"},
            {
                "id": "ba",
                "from": "b",
                "to": "a",
                "when": "false",
                "loop": {"id": "repair", "max_iterations": 5},
                "assignments": {"work.mode": {"const": "repair"}},
            },
            {"id": "bu", "from": "b", "to": "e", "when": "unknown"},
        ],
    }


def test_two_bounded_back_edges_and_order_invariance():
    graph = loop_graph()
    report = validate_graph(graph)
    assert report.ok, report.to_dict()
    graph["nodes"].reverse()
    graph["edges"].reverse()
    reordered = validate_graph(graph)
    assert reordered.ok and reordered.graph_hash == report.graph_hash
    graph["edges"][1]["loop"]["max_iterations"] = 3
    assert validate_graph(graph).graph_hash != report.graph_hash


@pytest.mark.parametrize(
    "mutation", ["unbounded", "duplicate_branch", "immutable_assignment", "bad_mode", "no_exit"]
)
def test_invalid_transitions(mutation):
    graph = loop_graph()
    if mutation == "unbounded":
        graph["edges"][5].pop("loop")
    elif mutation == "duplicate_branch":
        graph["edges"][2]["when"] = "true"
    elif mutation == "immutable_assignment":
        graph["edges"][5]["assignments"] = {"inputs.task": {"const": "replace"}}
    elif mutation == "bad_mode":
        graph["edges"][5]["assignments"] = {"work.mode": {"const": "anything"}}
    else:
        for edge in graph["edges"]:
            if edge["to"] == "e":
                edge["to"] = "b"
    assert not validate_graph(graph).ok


@pytest.mark.parametrize(
    "path", ["secrets.key", "work.unknown", "steps.a.latest.typo", "input.unknown", "input..task"]
)
def test_bad_references_in_prompt_and_exists(path):
    graph = loop_graph()
    graph["nodes"][1]["expression"] = {"op": "exists", "target": path}
    assert not validate_graph(graph).ok
    graph = linear({"id": "agent", "type": "AgentTask", "config": {"prompt": "{{ " + path + " }}"}})
    assert not validate_graph(graph).ok


def test_forward_reference_and_stale_evidence():
    from agents_ide.domain.graph_ast import LatestResult

    graph = loop_graph()
    graph["nodes"][1]["expression"] = {"ref": "steps.b.latest.decision"}
    assert validate_graph(graph).ok
    expression = ASTNode.from_json({"ref": "steps.b.latest.decision"})
    for cycle, scope, evidence in [
        (2, "item", "current"),
        (1, "other", "current"),
        (1, "item", "old"),
    ]:
        ctx = EvaluationContext(
            known_node_ids=frozenset({"b"}),
            cycle_id=1,
            scope="item",
            evidence_manifest_id="current",
            latest={
                "b": LatestResult(
                    decision=TruthValue.TRUE,
                    cycle_id=cycle,
                    scope=scope,
                    evidence_manifest_id=evidence,
                )
            },
        )
        assert evaluate_truth(expression, ctx) == TruthValue.UNKNOWN


def test_substitution_is_single_pass_and_supports_structured_context():
    ctx = EvaluationContext(
        inputs={"task": Value.of("{{ secrets.key }}"), "names": Value.of(["one"])}
    )
    assert substitute("{{ input.task }} {{ inputs.names }}", ctx) == '{{ secrets.key }} ["one"]'
    assert substitute("{{ inputs.absent }}", ctx, strict=False) == "{{ inputs.absent }}"


@pytest.mark.parametrize(
    "params", [{"reasoning_effort": "banana"}, {"temperature": True}, {"unsupported": 1}]
)
def test_invalid_generation_parameters(params):
    assert not validate_graph(
        linear({"id": "a", "type": "LLMRequest", "config": {"prompt": "x", "params": params}})
    ).ok


def post(client, headers, path, body):
    response = client.post(path, headers=headers, json=body)
    assert response.is_success, response.text
    return response.json()


def binding_fixture(authenticated, tmp_path, *, graph=None, origin="local", settings=None):
    client, headers = authenticated
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    project = post(
        client, headers, "/api/projects", {"name": "p", "workspace_path": str(workspace)}
    )
    template = post(client, headers, "/api/templates", {"name": "t"})
    version = post(
        client,
        headers,
        f"/api/templates/{template['id']}/versions",
        {
            "graph": graph or linear(),
            "origin": origin,
            "settings": settings or {},
        },
    )
    binding = post(
        client,
        headers,
        f"/api/versions/{version['id']}/bindings",
        {"project_id": project["id"], "name": "main"},
    )
    return project, version, binding


def test_preflight_inputs_overrides_and_run_hash(authenticated, tmp_path):
    client, headers = authenticated
    profile = post(
        client,
        headers,
        "/api/harness_profiles",
        {"name": "h", "harness_kind": "codex", "settings": {"permission_mode": "read_only"}},
    )
    graph = linear(
        {"id": "a", "type": "AgentTask", "config": {"role": "dev", "prompt": "{{ input.task }}"}}
    )
    graph["input_schema"] = {
        "type": "object",
        "properties": {"task": {"type": "string"}},
        "required": ["task"],
        "additionalProperties": False,
    }
    project, _, binding = binding_fixture(authenticated, tmp_path, graph=graph)
    assert not post(client, headers, f"/api/bindings/{binding['id']}/preflight", {})["ok"]
    payload = {
        "inputs": {"task": "hello"},
        "overrides": {
            "model_selections": {
                "dev": {"kind": "direct", "model_id": "m", "harness_profile_id": profile["id"]}
            }
        },
    }
    preview = post(client, headers, f"/api/bindings/{binding['id']}/preflight", payload)
    assert preview["ok"], preview
    assert preview["candidates"]["a"][0]["selection_source"] == "run"
    run = post(
        client,
        headers,
        "/api/runs",
        {
            "project_id": project["id"],
            "binding_id": binding["id"],
            "message": "x",
            "idempotency_key": "inputs",
            **payload,
        },
    )
    assert run["execution_hash"] == preview["execution_hash"]
    invalid = deepcopy(payload)
    invalid["inputs"]["task"] = 7
    response = client.post(
        "/api/runs",
        headers=headers,
        json={
            "project_id": project["id"],
            "binding_id": binding["id"],
            "message": "x",
            "idempotency_key": "bad-input",
            **invalid,
        },
    )
    assert response.status_code == 422


def test_import_roundtrip_trust_and_settings(authenticated, tmp_path):
    client, headers = authenticated
    body = {
        "graph": linear(),
        "settings": {"limit_overrides": {"max_calls": 20}},
        "inputs": {"task": "x"},
        "schema_version": "1.0.0",
        "required_features": [],
        "trusted": True,
        "origin": "local",
        "execution_hash": "f" * 64,
    }
    imported = post(client, headers, "/api/graphs/import", body)
    assert imported["ok"] and imported["body"]["origin"] == "imported"
    assert imported["body"]["settings"] == body["settings"]
    project, _, binding = binding_fixture(
        authenticated, tmp_path, origin="imported", settings=body["settings"]
    )
    preview = post(client, headers, f"/api/bindings/{binding['id']}/preflight", {})
    start = {
        "project_id": project["id"],
        "binding_id": binding["id"],
        "message": "x",
        "idempotency_key": "trust",
    }
    response = client.post("/api/runs", headers=headers, json=start)
    assert response.status_code == 409 and response.json()["code"] == "import_trust_required"
    start["trusted_execution_hash"] = preview["execution_hash"]
    run = post(client, headers, "/api/runs", start)
    assert post(client, headers, "/api/runs", start)["id"] == run["id"]
    post(client, headers, f"/api/bindings/{binding['id']}/preflight", {})
    changed = {
        **start,
        "idempotency_key": "changed",
        "overrides": {"limit_overrides": {"max_calls": 10}},
    }
    assert client.post("/api/runs", headers=headers, json=changed).status_code == 409


def test_group_preview_all_candidates_and_exhaustion(authenticated, tmp_path):
    client, headers = authenticated
    profiles = [
        post(
            client,
            headers,
            "/api/harness_profiles",
            {
                "name": f"h{i}",
                "harness_kind": "codex",
                "settings": {"permission_mode": "read_only"},
            },
        )
        for i in range(2)
    ]
    group = post(
        client,
        headers,
        "/api/model_groups/agent",
        {
            "name": "g",
            "members": [{"model_id": "m", "harness_profile_id": p["id"]} for p in profiles],
        },
    )
    graph = linear({"id": "a", "type": "AgentTask", "config": {"role": "dev", "prompt": "x"}})
    _, _, binding = binding_fixture(
        authenticated,
        tmp_path,
        graph=graph,
        settings={"model_selections": {"dev": {"kind": "group", "group_id": group["id"]}}},
    )
    for index, profile in enumerate(profiles):
        assert client.post(
            f"/api/harness_profiles/{profile['id']}/archive?expected_version=1", headers=headers
        ).is_success
        preview = post(client, headers, f"/api/bindings/{binding['id']}/preflight", {})
        assert preview["ok"] is (index == 0)
        assert [c["member_index"] for c in preview["candidates"]["a"]] == [0, 1]
        assert preview["candidates"]["a"][0]["reason"] == "archived"


@pytest.mark.parametrize(
    "bad",
    [{"nodes": 1, "edges": []}, {"nodes": [{"id": "s"}], "edges": []}, {"nodes": [], "edges": {}}],
)
def test_malformed_payload_never_500(authenticated, bad):
    client, headers = authenticated
    response = client.post("/api/graphs/validate", headers=headers, json={"graph": bad})
    assert response.status_code == 200 and not response.json()["ok"]


def test_command_preflight_missing_program_and_path_escape(authenticated, tmp_path):
    client, headers = authenticated
    graph = linear(
        {
            "id": "c",
            "type": "Command",
            "config": {
                "commands": [
                    {
                        "id": "check",
                        "program": "nonexistent-program-stage3",
                        "args": [],
                        "success_exit_codes": [0],
                    }
                ]
            },
        }
    )
    _, _, binding = binding_fixture(authenticated, tmp_path, graph=graph)
    preview = post(client, headers, f"/api/bindings/{binding['id']}/preflight", {})
    assert not preview["ok"] and any(e["code"] == "command_unavailable" for e in preview["errors"])
    graph["nodes"][1]["config"]["commands"][0]["cwd"] = "../outside"
    assert not validate_graph(graph).ok


def test_remote_input_schema_does_not_resolve_url():
    graph = linear()
    graph["input_schema"] = {"$ref": "https://example.invalid/schema"}
    assert not validate_graph(graph).ok


def test_imported_definition_can_be_published_without_losing_provenance(authenticated, tmp_path):
    client, headers = authenticated
    imported = post(
        client, headers, "/api/graphs/import", {"graph": linear(), "inputs": {"task": "x"}}
    )
    template = post(client, headers, "/api/templates", {"name": "imported-template"})
    version = post(client, headers, f"/api/templates/{template['id']}/versions", imported["body"])
    assert (
        version["origin"] == "imported" and version["execution_hash"] == imported["execution_hash"]
    )
    response = client.put(
        f"/api/templates/{template['id']}/draft",
        headers=headers,
        json={**imported["body"], "expected_version": 1},
    )
    assert response.is_success, response.text
    response = client.put(
        f"/api/templates/{template['id']}/draft",
        headers=headers,
        json={"graph": linear(), "expected_version": 2},
    )
    assert response.is_success and response.json()["draft"]["origin"] == "imported"


def test_run_inputs_change_trusted_hash(authenticated, tmp_path):
    client, headers = authenticated
    project, _, binding = binding_fixture(authenticated, tmp_path, origin="imported")
    preview = post(
        client,
        headers,
        f"/api/bindings/{binding['id']}/preflight",
        {"inputs": {"task": "original"}},
    )
    response = client.post(
        "/api/runs",
        headers=headers,
        json={
            "project_id": project["id"],
            "binding_id": binding["id"],
            "message": "x",
            "inputs": {"task": "changed"},
            "trusted_execution_hash": preview["execution_hash"],
            "idempotency_key": "changed-inputs",
        },
    )
    assert response.status_code == 409


def test_missing_secret_is_visible_and_next_llm_candidate_is_usable(authenticated, tmp_path):
    client, headers = authenticated
    first = post(
        client,
        headers,
        "/api/connections",
        {"name": "protected", "base_url": "https://one.invalid"},
    )
    second = post(
        client, headers, "/api/connections", {"name": "public", "base_url": "https://two.invalid"}
    )
    group = post(
        client,
        headers,
        "/api/model_groups/llm",
        {
            "name": "llms",
            "members": [
                {"provider_connection_id": p["id"], "model_id": "m"} for p in (first, second)
            ],
        },
    )
    graph = linear(
        {
            "id": "a",
            "type": "LLMRequest",
            "config": {
                "prompt": "x",
                "model_selection": {"kind": "group", "group_id": group["id"]},
            },
        }
    )
    _, _, binding = binding_fixture(authenticated, tmp_path, graph=graph)
    from agents_ide.persistence.models import ProviderConnection

    with client.app.state.session_factory() as session:
        from agents_ide.domain.common import new_id

        protected = session.get(ProviderConnection, first["id"])
        reference = protected.secret_reference = new_id()
        session.commit()
    (client.app.state.settings.data_dir / "secrets" / f"{reference}.dpapi").write_bytes(b"broken")
    preview = post(client, headers, f"/api/bindings/{binding['id']}/preflight", {})
    assert preview["ok"] and preview["candidates"]["a"][0]["reason"] == "secret_unavailable"
    import json

    serialized = json.dumps(preview)
    assert reference not in serialized and "regression-value" not in serialized
    assert {d["base_url"] for d in preview["data_destinations"]} == {
        first["base_url"],
        second["base_url"],
    }


def test_preflight_rejects_unmeasurable_budget(authenticated, tmp_path):
    client, headers = authenticated
    _, _, binding = binding_fixture(authenticated, tmp_path)
    preview = post(
        client,
        headers,
        f"/api/bindings/{binding['id']}/preflight",
        {"overrides": {"limit_overrides": {"max_cost": 1}}},
    )
    assert not preview["ok"] and preview["errors"][0]["code"] == "budget_unsupported"
