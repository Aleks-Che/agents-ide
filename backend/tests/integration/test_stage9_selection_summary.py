"""Run candidate group summary surfaced through /api/runs/{id}/snapshot.

The selection summary lists every node with a model selection (group or
direct), the ordered pinned candidates per node, and the runner's current
visit state (current member, retries, selection_round, history). It derives
everything from the immutable Run snapshot and the durable runtime, never
recomputing against the live resource catalog.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import delete

from agents_ide.adapters.fake import FakeAgentAdapter, FakeLLMAdapter
from agents_ide.engine.queue import claim_next_job, release_job
from agents_ide.engine.runner import Runner
from agents_ide.persistence.models import Run as RunModel
from agents_ide.persistence.models import RunEvent
from agents_ide.security.filesystem import prepare_data_dir
from agents_ide.services.run_selection import build_selection_summary


def _project(client, headers, workspace, name: str) -> dict:
    response = client.post(
        "/api/projects",
        headers=headers,
        json={"name": name, "workspace_path": str(workspace)},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _connection(client, headers, suffix: str) -> dict:
    response = client.post(
        "/api/connections",
        headers=headers,
        json={"name": f"c-{suffix}", "base_url": "http://127.0.0.1:9/v1"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _group_with_disabled(
    client,
    headers,
    *,
    enabled_id: str,
    disabled_id: str,
    suffix: str,
) -> dict:
    response = client.post(
        "/api/model_groups/llm",
        headers=headers,
        json={
            "name": f"g-{suffix}",
            "members": [
                {"provider_connection_id": disabled_id, "model_id": "alpha", "enabled": False},
                {"provider_connection_id": enabled_id, "model_id": "beta"},
            ],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _version_with_nodes(
    client,
    headers,
    *,
    suffix: str,
    group_id: str | None = None,
    include_direct: bool = False,
) -> dict:
    nodes = [
        {"id": "s", "type": "Start"},
        {
            "id": "check",
            "type": "LLMRequest",
            "config": {"prompt": "evaluate"},
        },
    ]
    if include_direct:
        nodes.append(
            {
                "id": "implement",
                "type": "AgentTask",
                "config": {
                    "prompt": "do work",
                    "model_selection": {
                        "kind": "direct",
                        "model_id": "manual-agent",
                        "harness_profile_id": "placeholder",
                    },
                },
            }
        )
    nodes.append({"id": "e", "type": "End"})
    edges = [
        {"from": "s", "to": "check"},
        {"from": "check", "to": "e"},
    ]
    if include_direct:
        edges.insert(1, {"from": "check", "to": "implement"})
        edges.insert(2, {"from": "implement", "to": "e"})
    if group_id:
        for node in nodes:
            if node.get("id") in {"check"}:
                node["config"]["model_selection"] = {
                    "kind": "group",
                    "group_id": group_id,
                }
    template = client.post("/api/templates", headers=headers, json={"name": f"t-{suffix}"}).json()
    response = client.post(
        f"/api/templates/{template['id']}/versions",
        headers=headers,
        json={"graph": {"nodes": nodes, "edges": edges}},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_snapshot_summary_surfaces_group_and_candidates(authenticated, tmp_path) -> None:
    client, headers = authenticated
    workspace = tmp_path / "sel-ws-1"
    workspace.mkdir()
    project = _project(client, headers, workspace, "sel-1")
    disabled_conn = _connection(client, headers, "alpha")
    enabled_conn = _connection(client, headers, "beta")
    group = _group_with_disabled(
        client,
        headers,
        enabled_id=enabled_conn["id"],
        disabled_id=disabled_conn["id"],
        suffix="disabled-first",
    )
    version = _version_with_nodes(client, headers, suffix="with-group", group_id=group["id"])
    binding = client.post(
        f"/api/versions/{version['id']}/bindings",
        headers=headers,
        json={"project_id": project["id"], "name": "sel-binding"},
    ).json()
    start = client.post(
        "/api/runs",
        headers=headers,
        json={
            "execution_mode": "simulated",
            "project_id": project["id"],
            "binding_id": binding["id"],
            "message": "go",
            "idempotency_key": "selection-summary-1",
            "overrides": {},
        },
    )
    assert start.status_code == 201, start.text
    run = start.json()
    snapshot = client.get(f"/api/runs/{run['id']}/snapshot", headers=headers).json()
    assert snapshot["selection"] is not None
    selection = snapshot["selection"]
    assert selection["group_changes_apply_only_to_new_runs"] is False
    groups = {entry["id"]: entry for entry in selection["groups"]}
    assert group["id"] in groups
    assert groups[group["id"]]["enabled_count"] == 1
    nodes = {entry["node_id"]: entry for entry in selection["nodes"]}
    check = nodes["check"]
    assert check["model_group_id"] == group["id"]
    assert check["selection_kind"] == "group"
    candidates = check["candidates"]
    assert len(candidates) == 2
    assert candidates[0]["model_id"] == "alpha"
    assert candidates[0]["enabled"] is False
    assert candidates[0]["state"] == "skipped"
    assert candidates[0]["last_reason"] == "disabled"
    assert candidates[1]["model_id"] == "beta"
    assert candidates[1]["enabled"] is True
    assert candidates[1]["state"] == "available"
    assert selection["visit"] is None


def test_snapshot_summary_includes_visit_state(authenticated, tmp_path) -> None:
    client, headers = authenticated
    workspace = tmp_path / "sel-ws-2"
    workspace.mkdir()
    project = _project(client, headers, workspace, "sel-2")
    conn = _connection(client, headers, "primary")
    response = client.post(
        "/api/model_groups/llm",
        headers=headers,
        json={
            "name": "g-sel-2",
            "members": [
                {"provider_connection_id": conn["id"], "model_id": "a"},
                {"provider_connection_id": conn["id"], "model_id": "b"},
            ],
        },
    )
    assert response.status_code == 201, response.text
    group = response.json()
    version = _version_with_nodes(client, headers, suffix="visit", group_id=group["id"])
    binding = client.post(
        f"/api/versions/{version['id']}/bindings",
        headers=headers,
        json={"project_id": project["id"], "name": "sel-binding"},
    ).json()
    start = client.post(
        "/api/runs",
        headers=headers,
        json={
            "execution_mode": "simulated",
            "project_id": project["id"],
            "binding_id": binding["id"],
            "message": "go",
            "idempotency_key": "selection-summary-visit",
            "overrides": {},
        },
    )
    assert start.status_code == 201, start.text
    run = start.json()
    snapshot = client.get(f"/api/runs/{run['id']}/snapshot", headers=headers).json()
    selection = snapshot["selection"]
    assert selection["visit"] is None
    assert selection["nodes"][0]["actual"] is None


def test_snapshot_summary_for_direct_node_marks_no_group(authenticated, tmp_path) -> None:
    client, headers = authenticated
    workspace = tmp_path / "sel-ws-3"
    workspace.mkdir()
    project = _project(client, headers, workspace, "sel-3")
    profile = client.post(
        "/api/harness_profiles",
        headers=headers,
        json={"name": "p-direct", "harness_kind": "codex", "settings": {}},
    ).json()
    nodes = [
        {"id": "s", "type": "Start"},
        {
            "id": "implement",
            "type": "AgentTask",
            "config": {
                "prompt": "do work",
                "model_selection": {
                    "kind": "direct",
                    "model_id": "manual-agent",
                    "harness_profile_id": profile["id"],
                },
            },
        },
        {"id": "e", "type": "End"},
    ]
    template = client.post("/api/templates", headers=headers, json={"name": "t-direct"}).json()
    response = client.post(
        f"/api/templates/{template['id']}/versions",
        headers=headers,
        json={
            "graph": {
                "nodes": nodes,
                "edges": [
                    {"from": "s", "to": "implement"},
                    {"from": "implement", "to": "e"},
                ],
            }
        },
    )
    assert response.status_code == 201, response.text
    version = response.json()
    binding = client.post(
        f"/api/versions/{version['id']}/bindings",
        headers=headers,
        json={"project_id": project["id"], "name": "b-direct"},
    ).json()
    start = client.post(
        "/api/runs",
        headers=headers,
        json={
            "execution_mode": "simulated",
            "project_id": project["id"],
            "binding_id": binding["id"],
            "message": "go",
            "idempotency_key": "selection-summary-direct",
            "overrides": {},
        },
    )
    assert start.status_code == 201, start.text
    run = start.json()
    snapshot = client.get(f"/api/runs/{run['id']}/snapshot", headers=headers).json()
    selection = snapshot["selection"]
    nodes_by_id = {entry["node_id"]: entry for entry in selection["nodes"]}
    implement = nodes_by_id["implement"]
    assert implement["selection_kind"] == "direct"
    assert implement["model_group_id"] is None
    assert implement["direct_model_id"] == "manual-agent"
    candidates = implement["candidates"]
    assert len(candidates) == 1
    assert candidates[0]["member_index"] == 0
    assert candidates[0]["model_id"] == "manual-agent"
    assert selection["groups"] == []


def _review_run(authenticated, tmp_path, *, kind="llm", responses=None, legacy=False):
    client, headers = authenticated
    tmp_path.mkdir(exist_ok=True)
    project = _project(client, headers, tmp_path, "review")
    if kind == "agent":
        endpoint = "/api/harness_profiles"
        payload = {"name": "h", "harness_kind": "codex", "settings": {}}
        resource_key, node_type = "harness_profile_id", "AgentTask"
    else:
        endpoint = "/api/connections"
        payload = {"name": "c", "base_url": "http://127.0.0.1:9/v1"}
        resource_key, node_type = "provider_connection_id", "LLMRequest"
    resource = client.post(endpoint, headers=headers, json=payload).json()
    group = client.post(
        f"/api/model_groups/{kind}",
        headers=headers,
        json={
            "name": "heavy",
            "members": [
                {resource_key: resource["id"], "model_id": "disabled", "enabled": False},
                {resource_key: resource["id"], "model_id": "alpha"},
                {resource_key: resource["id"], "model_id": "beta"},
            ],
        },
    ).json()
    nodes = [{"id": "s", "type": "Start"}]
    for node_id in ("first", "second"):
        config = {
            "role": node_id,
            "prompt": "work",
            "model_selection": {"kind": "group", "group_id": group["id"]},
        }
        if legacy:
            config = {
                "prompt": "work",
                "model": "legacy",
                "harness_profile_id" if kind == "agent" else "connection_id": resource["id"],
            }
        nodes.append({"id": node_id, "type": node_type, "max_retries": 0, "config": config})
    nodes.append({"id": "e", "type": "End"})
    template = client.post("/api/templates", headers=headers, json={"name": "review"}).json()
    version = client.post(
        f"/api/templates/{template['id']}/versions",
        headers=headers,
        json={
            "graph": {
                "nodes": nodes,
                "edges": [
                    {"from": a, "to": b}
                    for a, b in (("s", "first"), ("first", "second"), ("second", "e"))
                ],
            }
        },
    )
    assert version.status_code == 201, version.text
    binding = client.post(
        f"/api/versions/{version.json()['id']}/bindings",
        headers=headers,
        json={"project_id": project["id"], "name": "review"},
    ).json()
    response = client.post(
        "/api/runs",
        headers=headers,
        json={
            "project_id": project["id"],
            "binding_id": binding["id"],
            "execution_mode": "simulated",
            "idempotency_key": str(uuid.uuid4()),
            "message": "go",
            "fake_scenario": {"responses": responses or []},
        },
    )
    assert response.status_code == 201, response.text
    return response.json(), group


def _execute(client, run, settings):
    factory = client.app.state.session_factory
    job = claim_next_job(factory, worker_id="selection-review", lease_seconds=30)
    assert job and job.run_id == run["id"]
    result = Runner(
        session_factory=factory,
        worker_id="selection-review",
        generation=job.generation,
        data_dir=settings.data_dir,
        secret_store=None,
    ).execute(run["id"])
    release_job(
        factory, job_id=job.job_id, worker_id="selection-review", expected_generation=job.generation
    )
    return result


def _summary(client, run):
    response = client.get(f"/api/runs/{run['id']}/snapshot")
    assert response.status_code == 200, response.text
    return response.json()["selection"]


@pytest.mark.parametrize("kind", ["agent", "llm"])
def test_actual_selection_is_isolated_by_node_and_survives_end_and_retention(
    authenticated, tmp_path, settings, monkeypatch, kind
):
    client, headers = authenticated
    run, group = _review_run(
        authenticated,
        tmp_path / "ws",
        kind=kind,
        responses=[
            {
                "node_id": "first",
                "attempt_index": 1,
                "outcome": "unavailable",
                "error_code": "quota",
                "retry_safety": "safe",
                "no_effect": True,
            }
        ],
    )
    adapter_class = FakeAgentAdapter if kind == "agent" else FakeLLMAdapter
    original = adapter_class.run
    observed = []

    def inspect(adapter, request):
        if request.context_package["__node_id__"] == "first" and request.attempt_index == 2:
            summary = _summary(client, run)
            first, second = summary["nodes"]
            assert summary["visit"]["current_node_id"] == "first"
            assert summary["visit"]["current_model_id"] == "beta"
            assert [c["state"] for c in first["candidates"]] == ["skipped", "consumed", "current"]
            assert second["actual"] is None and second["visit"] is None
            assert second["candidates"][1]["last_reason"] is None
            observed.append(True)
        return original(adapter, request)

    monkeypatch.setattr(adapter_class, "run", inspect)
    assert _execute(client, run, settings).final_state == "completed"
    assert observed
    # The next CLI/API startup must accept the private fake workspace created by Runner.
    prepare_data_dir(settings.data_dir)
    before = _summary(client, run)
    first, second = before["nodes"]
    assert before["visit"] is None
    assert first["kind"] == kind and first["actual"]["model_id"] == "beta"
    assert first["candidates"][0]["state"] == "skipped"
    assert first["candidates"][1]["last_reason"] == "quota"
    assert first["candidates"][2]["state"] == "succeeded"
    assert second["actual"]["model_id"] == "alpha"
    assert second["candidates"][1]["last_reason"] is None
    assert first["visit"]["history_complete"] is True
    assert second["visit"]["selection_round"] == 0
    # Current groups and the retained event window must not rewrite this summary.
    archived = client.post(
        f"/api/model_groups/{group['id']}/archive",
        headers=headers,
        params={"expected_revision": group["revision"]},
    )
    assert archived.status_code == 200, archived.text
    with client.app.state.session_factory() as session:
        session.execute(delete(RunEvent).where(RunEvent.run_id == run["id"]))
        session.commit()
    assert _summary(client, run) == before


def test_exhaustion_resume_rechecks_pinned_candidates_and_preserves_rounds(
    authenticated, tmp_path, settings
):
    client, headers = authenticated
    run, _ = _review_run(
        authenticated,
        tmp_path / "ws",
        responses=[
            {
                "node_id": "first",
                "attempt_index": index,
                "outcome": "unavailable",
                "error_code": f"quota_{index}",
                "retry_safety": "safe",
                "no_effect": True,
            }
            for index in (1, 2)
        ],
    )
    assert _execute(client, run, settings).waiting_reason.code == "model_group_exhausted"
    before = _summary(client, run)
    assert before["visit"]["current_member_index"] is None
    assert [c["state"] for c in before["nodes"][0]["candidates"]] == [
        "skipped",
        "consumed",
        "consumed",
    ]
    assert [c["last_reason"] for c in before["nodes"][0]["candidates"]] == [
        "disabled",
        "quota_1",
        "quota_2",
    ]
    assert before["nodes"][1]["candidates"][1]["last_reason"] is None
    current = client.get(f"/api/runs/{run['id']}").json()
    resumed = client.post(
        f"/api/runs/{run['id']}/commands",
        headers=headers,
        json={
            "command_id": str(uuid.uuid4()),
            "command_type": "resume",
            "expected_state_version": current["state_version"],
            "payload": {},
        },
    )
    assert resumed.status_code == 200, resumed.text
    assert _summary(client, run)["visit"]["selection_round"] == 1
    assert _execute(client, run, settings).final_state == "completed"
    after = _summary(client, run)
    assert after["nodes"][0]["actual"]["model_id"] == "alpha"
    assert after["nodes"][0]["visit"]["selection_round"] == 1
    assert after["nodes"][1]["visit"]["selection_round"] == 0
    assert len(after["nodes"][0]["visit"]["history"]) == 4
    current = client.get(f"/api/runs/{run['id']}").json()
    assert current["snapshot_hash"] == run["snapshot_hash"]
    assert current["runtime"]["external_calls"] == 4


@pytest.mark.parametrize("kind", ["agent", "llm"])
def test_legacy_direct_execution_remains_visible(authenticated, tmp_path, settings, kind):
    client, _ = authenticated
    run, _ = _review_run(authenticated, tmp_path / "ws", kind=kind, legacy=True)
    assert _execute(client, run, settings).final_state == "completed"
    node = _summary(client, run)["nodes"][0]
    assert node["selection_kind"] == "direct"
    assert node["direct_model_id"] == node["actual"]["model_id"] == "legacy"
    # Older completed runs still expose the attempted executor when checkpoints were absent.
    with client.app.state.session_factory() as session:
        row = session.get(RunModel, run["id"])
        runtime = json.loads(row.runtime_json)
        runtime.pop("node_selections")
        row.runtime_json = json.dumps(runtime)
        session.commit()
    node = _summary(client, run)["nodes"][0]
    assert node["actual"]["model_id"] == "legacy"
    assert node["visit"]["history_complete"] is False


def test_no_dependencies_returns_null(authenticated):
    client, _ = authenticated
    with client.app.state.session_factory() as session:
        assert (
            build_selection_summary(session, RunModel(snapshot_json="{}", runtime_json="{}"))
            is None
        )


def test_clock_rollback_keeps_run_snapshot_readable(authenticated, tmp_path, settings, monkeypatch):
    import agents_ide.engine.runner as runner_module

    client, _ = authenticated
    run, _ = _review_run(authenticated, tmp_path / "clock", kind="llm", legacy=True)
    original = Runner._state

    def state(self, session, row, next_state, reason=None):
        if next_state == "completed":
            earlier = runner_module.utc_now() - 60
            monkeypatch.setattr(runner_module, "utc_now", lambda: earlier)
        return original(self, session, row, next_state, reason)

    monkeypatch.setattr(Runner, "_state", state)
    assert _execute(client, run, settings).final_state == "completed"
    response = client.get(f"/api/runs/{run['id']}")
    assert response.status_code == 200, response.text
    interval = response.json()["active_intervals"][0]
    assert interval["quality"] == "unknown"
    assert interval["ended_at"] == interval["started_at"]
    assert client.get(f"/api/runs/{run['id']}/snapshot").status_code == 200


def test_latest_visit_does_not_mix_attempts_from_previous_cycle(
    authenticated, tmp_path, settings, monkeypatch
):
    from test_stage4_review import execute, make_run, repair_graph, verdict

    client, _ = authenticated
    run, factory = make_run(authenticated, tmp_path, graph=repair_graph())
    result, _ = execute(
        run, factory, settings, monkeypatch, [verdict("failed", 1), verdict("passed", 2)]
    )
    assert result.final_state == "completed"
    summary = _summary(client, run)
    assert summary["visit"] is None
    for node in summary["nodes"]:
        if node["node_id"] == "unknown":
            assert node["visit"] is None and node["actual"] is None
            continue
        assert node["visit"]["visit_index"] == 2
        assert node["visit"]["cycle_id"] == 2
        assert node["visit"]["history"] == []
        assert node["actual"]["status"] == "succeeded"
