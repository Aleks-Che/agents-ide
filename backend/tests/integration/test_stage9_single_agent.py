"""Single-agent launch from chat: ``RunStart.single_agent`` builds a synthetic
``Start → AgentTask|LLMRequest → End`` graph from a binding's existing version
without persisting a new ``PipelineVersion``.

These tests cover the API, the preflight hash, the immutable snapshot, and
rejection paths for unknown roles / wrong selection kinds.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from test_stage4_engine_runner import _wait_for_terminal
from test_stage4_engine_runner import fake_worker as fake_worker

from agents_ide.domain.schemas import SingleAgentSpec
from agents_ide.domain.single_agent import build_single_agent_graph
from agents_ide.engine.queue import claim_next_job
from agents_ide.engine.runner import Runner
from agents_ide.errors import AppError
from agents_ide.persistence.models import Run as RunModel
from agents_ide.persistence.models import StepAttempt, StepExecution


def _project(client, headers, workspace: Path, name: str) -> dict:
    workspace.mkdir()
    response = client.post(
        "/api/projects",
        json={"name": name, "workspace_path": str(workspace)},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _harness_profile(client, headers, name: str) -> dict:
    response = client.post(
        "/api/harness_profiles",
        json={"name": name, "harness_kind": "codex", "settings": {"model": "default"}},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _provider(client, headers, name: str) -> dict:
    response = client.post(
        "/api/connections",
        json={"name": name, "base_url": "https://example.invalid"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _template_with_agent(
    client,
    headers,
    name: str,
    *,
    node_type: str = "AgentTask",
    role: str = "dev",
    extra_nodes: list[dict] | None = None,
) -> dict:
    nodes: list[dict] = [
        {"id": "start", "type": "Start"},
        {"id": "agent", "type": node_type, "config": {"role": role, "prompt": "do work"}},
        {"id": "end", "type": "End"},
    ]
    if extra_nodes:
        nodes.extend(extra_nodes)
    template = client.post(
        "/api/templates", json={"name": name, "schema_version": "1.0.0"}, headers=headers
    ).json()
    return client.post(
        f"/api/templates/{template['id']}/versions",
        json={
            "graph": {
                "nodes": nodes,
                "edges": [
                    {"id": "e1", "from": "start", "to": "agent"},
                    {"id": "e2", "from": "agent", "to": "end"},
                ],
            },
        },
        headers=headers,
    ).json()


def _binding(client, headers, version_id: str, project_id: str, name: str, **overrides) -> dict:
    payload = {"project_id": project_id, "name": name}
    payload.update(overrides)
    response = client.post(f"/api/versions/{version_id}/bindings", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def _chat(client, headers, project_id: str) -> dict:
    response = client.post(
        f"/api/projects/{project_id}/chats",
        json={"title": "single agent"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_single_agent_launch_uses_synthetic_graph_and_pins_selection(authenticated, tmp_path):
    client, headers = authenticated
    project = _project(client, headers, tmp_path / "ws-single-1", "single-direct")
    profile = _harness_profile(client, headers, "single-direct-profile")
    provider = _provider(client, headers, "single-direct-conn")
    version = _template_with_agent(client, headers, "single-direct-tpl", role="implementer")
    binding = _binding(
        client,
        headers,
        version["id"],
        project["id"],
        "single-direct-binding",
        model_selections={
            "implementer": {
                "kind": "direct",
                "model_id": "from-binding",
                "harness_profile_id": profile["id"],
            },
            "verifier": {
                "kind": "direct",
                "model_id": "v",
                "provider_connection_id": provider["id"],
            },
        },
    )
    chat = _chat(client, headers, project["id"])

    parameters = {
        "execution_mode": "simulated",
        "inputs": {"task": "build a thing"},
        "overrides": {},
        "single_agent": {
            "role": "implementer",
            "selection": {
                "kind": "direct",
                "model_id": "from-run",
                "harness_profile_id": profile["id"],
            },
        },
    }
    checked = client.post(
        f"/api/bindings/{binding['id']}/preflight",
        json=parameters,
        headers=headers,
    ).json()
    assert checked["ok"], checked
    assert checked["execution_hash"]

    response = client.post(
        "/api/runs",
        json={
            **parameters,
            "project_id": project["id"],
            "chat_id": chat["id"],
            "binding_id": binding["id"],
            "idempotency_key": str(uuid.uuid4()),
            "trusted_execution_hash": checked["execution_hash"],
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    run = response.json()
    assert run["execution_hash"] == checked["execution_hash"]

    with client.app.state.session_factory() as session:
        run_model = session.scalar(select(RunModel).where(RunModel.id == run["id"]))
        snapshot = json.loads(run_model.snapshot_json)
    assert run_model.pipeline_version_id == version["id"]
    assert run_model.binding_id == binding["id"]
    graph_nodes = {node["id"]: node for node in snapshot["graph"]["nodes"]}
    assert set(graph_nodes) == {"start", "agent", "end"}
    assert graph_nodes["agent"]["type"] == "AgentTask"
    assert graph_nodes["agent"]["config"]["role"] == "implementer"
    assert graph_nodes["agent"]["config"]["model_selection"]["model_id"] == "from-run"
    assert snapshot["single_agent"] == {
        "role": "implementer",
        "selection": {
            "kind": "direct",
            "model_id": "from-run",
            "harness_profile_id": profile["id"],
        },
        "parameters": None,
    }
    deps_nodes = snapshot["dependencies"]["nodes"]
    assert deps_nodes["agent"]["model"] == "from-run"
    assert deps_nodes["agent"]["harness_profile_id"] == profile["id"]
    # The unrelated verifier selection from the binding must not be pinned.
    assert snapshot["dependencies"]["provider_connections"] == {}
    assert snapshot["dependencies"]["model_groups"] == {}
    assert "single_agent" in snapshot.get("required_features", [])


def test_single_agent_launch_falls_back_to_binding_selection(authenticated, tmp_path):
    client, headers = authenticated
    project = _project(client, headers, tmp_path / "ws-single-2", "single-fallback")
    profile = _harness_profile(client, headers, "single-fallback-profile")
    version = _template_with_agent(client, headers, "single-fallback-tpl", role="implementer")
    binding = _binding(
        client,
        headers,
        version["id"],
        project["id"],
        "single-fallback-binding",
        model_selections={
            "implementer": {
                "kind": "direct",
                "model_id": "binding-model",
                "harness_profile_id": profile["id"],
            },
        },
    )
    parameters = {
        "execution_mode": "simulated",
        "inputs": {},
        "overrides": {},
        "single_agent": {"role": "implementer"},
    }
    checked = client.post(
        f"/api/bindings/{binding['id']}/preflight", json=parameters, headers=headers
    ).json()
    assert checked["ok"], checked
    assert checked["candidates"]["agent"][0]["selection_source"] == "binding"
    with client.app.state.session_factory() as session:
        run = client.post(
            "/api/runs",
            json={
                **parameters,
                "project_id": project["id"],
                "binding_id": binding["id"],
                "message": "fallback",
                "idempotency_key": str(uuid.uuid4()),
                "trusted_execution_hash": checked["execution_hash"],
            },
            headers=headers,
        )
        assert run.status_code == 201, run.text
        snapshot = json.loads(
            session.scalar(select(RunModel).where(RunModel.id == run.json()["id"])).snapshot_json
        )
    assert snapshot["dependencies"]["nodes"]["agent"]["model"] == "binding-model"


def test_single_agent_launch_uses_llm_group_when_role_kind_matches(authenticated, tmp_path):
    client, headers = authenticated
    project = _project(client, headers, tmp_path / "ws-single-llm", "single-llm")
    provider = _provider(client, headers, "single-llm-conn")
    version = _template_with_agent(
        client, headers, "single-llm-tpl", node_type="LLMRequest", role="verifier"
    )
    group = client.post(
        "/api/model_groups/llm",
        json={
            "name": "single-llm-group",
            "members": [
                {"provider_connection_id": provider["id"], "model_id": "first"},
                {"provider_connection_id": provider["id"], "model_id": "second"},
            ],
        },
        headers=headers,
    ).json()
    binding = _binding(
        client,
        headers,
        version["id"],
        project["id"],
        "single-llm-binding",
        model_selections={"verifier": {"kind": "group", "group_id": group["id"]}},
    )
    parameters = {
        "execution_mode": "simulated",
        "inputs": {},
        "overrides": {},
        "single_agent": {
            "role": "verifier",
            "selection": {"kind": "group", "group_id": group["id"]},
        },
    }
    checked = client.post(
        f"/api/bindings/{binding['id']}/preflight", json=parameters, headers=headers
    ).json()
    assert checked["ok"], checked
    with client.app.state.session_factory() as session:
        response = client.post(
            "/api/runs",
            json={
                **parameters,
                "project_id": project["id"],
                "binding_id": binding["id"],
                "message": "group",
                "idempotency_key": str(uuid.uuid4()),
                "trusted_execution_hash": checked["execution_hash"],
            },
            headers=headers,
        )
        assert response.status_code == 201, response.text
        snapshot = json.loads(
            session.scalar(
                select(RunModel).where(RunModel.id == response.json()["id"])
            ).snapshot_json
        )
    candidates = snapshot["dependencies"]["nodes"]["agent"]["candidates"]
    assert [c["model_id"] for c in candidates] == ["first", "second"]
    assert group["id"] in snapshot["dependencies"]["model_groups"]


def test_single_agent_launch_rejects_unknown_role(authenticated, tmp_path):
    client, headers = authenticated
    project = _project(client, headers, tmp_path / "ws-single-bad-role", "single-bad-role")
    profile = _harness_profile(client, headers, "single-bad-profile")
    version = _template_with_agent(client, headers, "single-bad-role-tpl", role="dev")
    binding = _binding(
        client,
        headers,
        version["id"],
        project["id"],
        "single-bad-role-binding",
        model_selections={
            "dev": {
                "kind": "direct",
                "model_id": "m",
                "harness_profile_id": profile["id"],
            },
        },
    )
    response = client.post(
        f"/api/bindings/{binding['id']}/preflight",
        json={
            "execution_mode": "simulated",
            "inputs": {},
            "overrides": {},
            "single_agent": {
                "role": "ghost",
                "selection": {
                    "kind": "direct",
                    "model_id": "m",
                    "harness_profile_id": profile["id"],
                },
            },
        },
        headers=headers,
    )
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "single_agent_role_unknown"


def test_single_agent_launch_rejects_kind_mismatch(authenticated, tmp_path):
    client, headers = authenticated
    project = _project(client, headers, tmp_path / "ws-single-mismatch", "single-mismatch")
    provider = _provider(client, headers, "single-mm-conn")
    version = _template_with_agent(
        client, headers, "single-mm-tpl", node_type="AgentTask", role="dev"
    )
    binding = _binding(
        client,
        headers,
        version["id"],
        project["id"],
        "single-mm-binding",
    )
    # An LLMRequest selection against an AgentTask role is invalid.
    response = client.post(
        f"/api/bindings/{binding['id']}/preflight",
        json={
            "execution_mode": "simulated",
            "inputs": {},
            "overrides": {},
            "single_agent": {
                "role": "dev",
                "selection": {
                    "kind": "direct",
                    "model_id": "llm-model",
                    "provider_connection_id": provider["id"],
                },
            },
        },
        headers=headers,
    )
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "single_agent_selection_mismatch"


def test_single_agent_launch_isolates_unrelated_role_selections(authenticated, tmp_path):
    """A run with single_agent must not pin resources referenced by unrelated
    roles in the binding; otherwise archived groups elsewhere would block
    single-agent launches.
    """

    client, headers = authenticated
    project = _project(client, headers, tmp_path / "ws-single-isolate", "single-isolate")
    profile = _harness_profile(client, headers, "single-isolate-profile")
    version = _template_with_agent(client, headers, "single-isolate-tpl", role="dev")
    archived_group = client.post(
        "/api/model_groups/agent",
        json={
            "name": "archived-other",
            "members": [
                {"harness_profile_id": profile["id"], "model_id": "m"},
            ],
        },
        headers=headers,
    ).json()
    binding = _binding(
        client,
        headers,
        version["id"],
        project["id"],
        "single-isolate-binding",
        model_selections={
            "dev": {
                "kind": "direct",
                "model_id": "ok",
                "harness_profile_id": profile["id"],
            },
            "verifier": {"kind": "group", "group_id": archived_group["id"]},
        },
    )
    client.post(
        f"/api/model_groups/{archived_group['id']}/archive",
        params={"expected_revision": archived_group["revision"]},
        headers=headers,
    )
    parameters = {
        "execution_mode": "simulated",
        "inputs": {},
        "overrides": {},
        "single_agent": {
            "role": "dev",
            "selection": {
                "kind": "direct",
                "model_id": "ok",
                "harness_profile_id": profile["id"],
            },
        },
    }
    checked = client.post(
        f"/api/bindings/{binding['id']}/preflight", json=parameters, headers=headers
    ).json()
    assert checked["ok"], checked


def test_single_agent_launch_rejects_parameters_with_routing_keys(authenticated, tmp_path):
    client, headers = authenticated
    project = _project(client, headers, tmp_path / "ws-single-params", "single-params")
    profile = _harness_profile(client, headers, "single-params-profile")
    version = _template_with_agent(client, headers, "single-params-tpl", role="dev")
    binding = _binding(
        client,
        headers,
        version["id"],
        project["id"],
        "single-params-binding",
        model_selections={
            "dev": {
                "kind": "direct",
                "model_id": "m",
                "harness_profile_id": profile["id"],
            },
        },
    )
    response = client.post(
        f"/api/bindings/{binding['id']}/preflight",
        json={
            "execution_mode": "simulated",
            "inputs": {},
            "overrides": {},
            "single_agent": {
                "role": "dev",
                "selection": {
                    "kind": "direct",
                    "model_id": "m",
                    "harness_profile_id": profile["id"],
                },
                "parameters": {"harness_profile_id": profile["id"]},
            },
        },
        headers=headers,
    )
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_error"


def test_single_agent_launch_idempotent_replay(authenticated, tmp_path):
    client, headers = authenticated
    project = _project(client, headers, tmp_path / "ws-single-idemp", "single-idemp")
    profile = _harness_profile(client, headers, "single-idemp-profile")
    version = _template_with_agent(client, headers, "single-idemp-tpl", role="dev")
    binding = _binding(
        client,
        headers,
        version["id"],
        project["id"],
        "single-idemp-binding",
        model_selections={
            "dev": {
                "kind": "direct",
                "model_id": "m",
                "harness_profile_id": profile["id"],
            },
        },
    )
    parameters = {
        "execution_mode": "simulated",
        "inputs": {},
        "overrides": {},
        "single_agent": {
            "role": "dev",
            "selection": {
                "kind": "direct",
                "model_id": "m",
                "harness_profile_id": profile["id"],
            },
        },
    }
    checked = client.post(
        f"/api/bindings/{binding['id']}/preflight", json=parameters, headers=headers
    ).json()
    assert checked["ok"], checked
    body = {
        **parameters,
        "project_id": project["id"],
        "binding_id": binding["id"],
        "message": "idempotent",
        "idempotency_key": str(uuid.uuid4()),
        "trusted_execution_hash": checked["execution_hash"],
    }
    first = client.post("/api/runs", json=body, headers=headers)
    assert first.status_code == 201, first.text
    replay = client.post("/api/runs", json=body, headers=headers)
    assert replay.status_code == 201
    assert replay.json()["id"] == first.json()["id"]
    assert replay.json()["snapshot_hash"] == first.json()["snapshot_hash"]


@pytest.mark.usefixtures("fake_worker")
def test_single_agent_worker_subprocess_completes_run(authenticated, tmp_path):
    client, headers = authenticated
    project, binding, _, selection = _review_setup(authenticated, tmp_path)
    response = client.post(
        "/api/runs",
        headers=headers,
        json={
            "project_id": project["id"],
            "binding_id": binding["id"],
            "message": "single worker task",
            "idempotency_key": str(uuid.uuid4()),
            "execution_mode": "simulated",
            "single_agent": {"role": "dev", "node_id": "chosen", "selection": selection},
        },
    )
    assert response.status_code == 201, response.text
    final = _wait_for_terminal(client, headers, response.json()["id"])
    assert final["state"] == "completed", final


def _review_setup(authenticated, tmp_path, *, node_type="LLMRequest", duplicate=False):
    client, headers = authenticated
    project = _project(client, headers, tmp_path / "review-ws", "review")
    provider = _provider(client, headers, "review-provider")
    profile = _harness_profile(client, headers, "review-profile")
    field = "harness_profile_id" if node_type == "AgentTask" else "provider_connection_id"
    resource_id = profile["id"] if node_type == "AgentTask" else provider["id"]
    selection = {"kind": "direct", "model_id": "node-model", field: resource_id}
    selected = {
        "id": "chosen",
        "type": node_type,
        "timeout_seconds": 17,
        "max_retries": 0,
        "config": {
            "role": "dev",
            "prompt": "selected task",
            "model_selection": selection,
            "params": {"temperature": 0.6, "top_p": 0.8},
        },
    }
    other = {
        "id": "other",
        "type": "LLMRequest",
        "config": {"role": "dev" if duplicate else "reviewer", "prompt": "do not run"},
    }
    graph = {
        "nodes": [{"id": "s", "type": "Start"}, selected, other, {"id": "e", "type": "End"}],
        "edges": [
            {"from": "s", "to": "chosen"},
            {"from": "chosen", "to": "other"},
            {"from": "other", "to": "e"},
        ],
    }
    template = client.post(
        "/api/templates", json={"name": "review-template"}, headers=headers
    ).json()
    response = client.post(
        f"/api/templates/{template['id']}/versions", json={"graph": graph}, headers=headers
    )
    assert response.status_code == 201, response.text
    version = response.json()
    binding = _binding(client, headers, version["id"], project["id"], "review-binding")
    return project, binding, version, selection


@pytest.mark.parametrize("node_type", ["AgentTask", "LLMRequest"])
@pytest.mark.parametrize("grouped", [False, True])
def test_single_agent_reaches_completed_through_queue_and_runner(
    authenticated, tmp_path, settings, node_type, grouped
):
    client, headers = authenticated
    project, binding, version, selection = _review_setup(
        authenticated, tmp_path, node_type=node_type
    )
    if grouped:
        kind = "agent" if node_type == "AgentTask" else "llm"
        field = "harness_profile_id" if kind == "agent" else "provider_connection_id"
        group = client.post(
            f"/api/model_groups/{kind}",
            headers=headers,
            json={
                "name": "review-group",
                "members": [
                    {field: selection[field], "model_id": "disabled", "enabled": False},
                    {field: selection[field], "model_id": "available"},
                ],
            },
        ).json()
        selection = {"kind": "group", "group_id": group["id"]}
    parameters = {
        "execution_mode": "simulated",
        "single_agent": {
            "role": "dev",
            "node_id": "chosen",
            "selection": selection,
            "parameters": {"temperature": 0.2},
        },
        "fake_scenario": {"responses": [{"node_id": "chosen", "raw_text": "selected result"}]},
    }
    checked = client.post(
        f"/api/bindings/{binding['id']}/preflight", json=parameters, headers=headers
    ).json()
    assert checked["ok"], checked
    assert list(checked["candidates"]) == ["chosen"]
    assert checked["candidates"]["chosen"][-1]["params"] == {"temperature": 0.2, "top_p": 0.8}
    assert checked["candidates"]["chosen"][-1]["parameter_sources"]["temperature"] == "run"
    response = client.post(
        "/api/runs",
        headers=headers,
        json={
            **parameters,
            "project_id": project["id"],
            "binding_id": binding["id"],
            "message": "run selected",
            "idempotency_key": str(uuid.uuid4()),
            "trusted_execution_hash": checked["execution_hash"],
        },
    )
    assert response.status_code == 201, response.text
    run = response.json()
    factory = client.app.state.session_factory
    with factory() as session:
        before = session.get(RunModel, run["id"]).snapshot_json
        snapshot = json.loads(before)
        node = snapshot["graph"]["nodes"][1]
        assert node["id"] == "chosen"
        assert node["timeout_seconds"] == 17 and node["max_retries"] == 0
    job = claim_next_job(factory, worker_id="single-review", lease_seconds=30)
    assert job is not None
    result = Runner(
        session_factory=factory,
        worker_id="single-review",
        generation=job.generation,
        data_dir=settings.data_dir,
        secret_store=None,
    ).execute(run["id"])
    assert result.final_state == "completed", result
    with factory() as session:
        assert session.get(RunModel, run["id"]).snapshot_json == before
        executions = session.scalars(
            select(StepExecution).where(StepExecution.run_id == run["id"])
        ).all()
        assert [e.node_id for e in executions] == ["start", "chosen", "end"]
        attempts = session.scalars(
            select(StepAttempt).join(StepExecution).where(StepExecution.run_id == run["id"])
        ).all()
        assert len(attempts) == 1
    assert (
        client.get(f"/api/versions/{version['id']}", headers=headers).json()["graph"]
        == version["graph"]
    )


def test_single_agent_requires_node_for_shared_role(authenticated, tmp_path):
    client, headers = authenticated
    _, binding, _, _ = _review_setup(authenticated, tmp_path, duplicate=True)
    url = f"/api/bindings/{binding['id']}/preflight"
    response = client.post(url, headers=headers, json={"single_agent": {"role": "dev"}})
    assert response.status_code == 422
    assert response.json()["code"] == "single_agent_node_required"
    checked = client.post(
        url,
        headers=headers,
        json={
            "execution_mode": "simulated",
            "single_agent": {"role": "dev", "node_id": "chosen"},
        },
    ).json()
    assert checked["ok"], checked
    response = client.post(
        url, headers=headers, json={"single_agent": {"role": "dev", "node_id": "s"}}
    )
    assert response.status_code == 422


def test_single_agent_hash_ignores_visuals_and_guards_actual_parameters(authenticated, tmp_path):
    client, headers = authenticated
    project, binding, version, _ = _review_setup(authenticated, tmp_path)
    parameters = {"execution_mode": "simulated", "single_agent": {"role": "dev"}}
    checked = client.post(
        f"/api/bindings/{binding['id']}/preflight", json=parameters, headers=headers
    ).json()
    assert checked["ok"], checked
    moved = json.loads(json.dumps(version["graph"]))
    moved["nodes"][1].update(
        label="Moved task", position={"x": 12, "y": 50}, visual={"color": "red"}
    )
    copied = client.post("/api/templates", json={"name": "visual-copy"}, headers=headers).json()
    response = client.post(
        f"/api/templates/{copied['id']}/versions", json={"graph": moved}, headers=headers
    )
    assert response.status_code == 201, response.text
    moved_version = response.json()
    assert moved_version["execution_hash"] == version["execution_hash"]
    second = _binding(client, headers, moved_version["id"], project["id"], "moved")
    rechecked = client.post(
        f"/api/bindings/{second['id']}/preflight", json=parameters, headers=headers
    ).json()
    assert rechecked["execution_hash"] == checked["execution_hash"]
    rejected = client.post(
        "/api/runs",
        headers=headers,
        json={
            **parameters,
            "single_agent": {"role": "dev", "parameters": {"temperature": 0.2}},
            "project_id": project["id"],
            "binding_id": binding["id"],
            "message": "changed parameters",
            "idempotency_key": str(uuid.uuid4()),
            "trusted_execution_hash": checked["execution_hash"],
        },
    )
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["code"] == "execution_hash_changed"
    assert client.get(f"/api/runs?project_id={project['id']}", headers=headers).json() == []


@pytest.mark.parametrize(
    "prompt,extra",
    [
        ("Review {{steps.other.latest.decision}}", {}),
        ("Implement {{work.current_plan_item_id}}", {}),
        ("Review the plan", {"plan_check": "all"}),
    ],
)
def test_single_agent_rejects_missing_pipeline_context_before_dispatch(prompt, extra):
    version = SimpleNamespace(
        graph_json=json.dumps(
            {
                "nodes": [
                    {
                        "id": "selected",
                        "type": "LLMRequest",
                        "config": {"role": "dev", "prompt": prompt, **extra},
                    },
                ]
            }
        )
    )
    with pytest.raises(AppError) as failure:
        build_single_agent_graph(version, SingleAgentSpec(role="dev"))
    assert failure.value.code == "single_agent_context_required"


def test_single_agent_inherits_node_parameters_and_choice_over_archived_binding_group(
    authenticated, tmp_path
):
    client, headers = authenticated
    _, binding, _, selection = _review_setup(authenticated, tmp_path)
    group = client.post(
        "/api/model_groups/llm",
        headers=headers,
        json={
            "name": "obsolete",
            "members": [
                {"provider_connection_id": selection["provider_connection_id"], "model_id": "old"}
            ],
        },
    ).json()
    update = client.patch(
        f"/api/bindings/{binding['id']}",
        headers=headers,
        json={
            "expected_version": binding["version"],
            "model_selections": {"dev": {"kind": "group", "group_id": group["id"]}},
            "role_parameters": {"dev": {"temperature": 0.4}, "reviewer": {"top_p": 0.5}},
            "command_filter": ["pipeline-command"],
        },
    )
    assert update.status_code == 200, update.text
    archived = client.post(
        f"/api/model_groups/{group['id']}/archive",
        headers=headers,
        params={"expected_revision": group["revision"]},
    )
    assert archived.status_code == 200
    parameters = {"execution_mode": "simulated", "single_agent": {"role": "dev", "parameters": {}}}
    url = f"/api/bindings/{binding['id']}/preflight"
    checked = client.post(url, json=parameters, headers=headers).json()
    assert checked["ok"], checked
    candidate = checked["candidates"]["chosen"][0]
    assert candidate["model_id"] == "node-model"
    assert candidate["params"] == {"temperature": 0.6, "top_p": 0.8}
    assert candidate["selection_source"] == "node"
    assert checked["resolved_settings"]["command_filter"] == []
    assert set(checked["resolved_settings"]["role_parameters"]) == {"dev"}
    assert checked["setting_sources"]["model_selections.dev"] == "node"
    assert "role_parameters.reviewer.top_p" not in checked["setting_sources"]
    invalid = client.post(
        url,
        json={**parameters, "overrides": {"command_filter": ["pipeline-command"]}},
        headers=headers,
    ).json()
    assert any(error["code"] == "command_filter_invalid" for error in invalid["errors"])
