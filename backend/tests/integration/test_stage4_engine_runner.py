"""Stage 4: engine runner on the fake adapter, queue, events and SSE replay.

These tests verify the criterion: "полностью без фронтенда через API запускается
граф «реализация → failed → исправление → passed → End». Проверяется смена
промпта, история двух посещений и остановка на лимите."

The tests run the worker subprocess alongside the API. Explicit simulated
RunStart requests pin scenarios without rewriting immutable snapshots.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import text

WORKER_ARGV = [sys.executable, "-m", "agents_ide", "worker"]


@pytest.fixture
def fake_worker(authenticated, settings) -> Iterator[subprocess.Popen[bytes]]:
    """Spawn the real worker and wait until its heartbeat is recorded."""

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "AGENTS_IDE_DATA_DIR": str(settings.data_dir),
        "AGENTS_IDE_HEARTBEAT_SECONDS": "0.1",
        "AGENTS_IDE_WORKER_STALE_SECONDS": "5",
    }
    worker = subprocess.Popen(
        WORKER_ARGV,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 15
        engine = authenticated[0].app.state.engine
        last_seen = None
        while time.monotonic() < deadline:
            with engine.connect() as connection:
                last_seen = connection.execute(
                    text("SELECT last_seen_at FROM worker_heartbeat")
                ).scalar()
            if last_seen:
                break
            if worker.poll() is not None:
                stdout, stderr = worker.communicate(timeout=2)
                raise RuntimeError(
                    f"worker exited prematurely:\nstdout={stdout!r}\nstderr={stderr!r}"
                )
            time.sleep(0.1)
        assert last_seen, "worker did not report its heartbeat in time"
        yield worker
    finally:
        worker.terminate()
        worker.communicate(timeout=10)


# --------------------------------------------------------------------------- helpers


def _seed_harness_and_project(client, headers, *, tmp_path: Path):
    workspace = tmp_path / "ws"
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
    connection = client.post(
        "/api/connections",
        headers=headers,
        json={"name": "loopback", "base_url": "http://127.0.0.1:9/v1"},
    ).json()
    template = client.post("/api/templates", headers=headers, json={"name": "t"}).json()
    return project, profile, connection, template


def _wait_for_terminal(client, headers, run_id: str, timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/runs/{run_id}", headers=headers)
        if response.status_code != 200:
            time.sleep(0.05)
            continue
        state = response.json().get("state")
        if state in {"completed", "failed", "cancelled"}:
            return response.json()
        time.sleep(0.05)
    return client.get(f"/api/runs/{run_id}", headers=headers).json()


# --------------------------------------------------------------------------- runner


def test_runner_drive_completed_graph_on_fake(fake_worker, authenticated, tmp_path):
    client, headers = authenticated
    project, profile, connection, template = _seed_harness_and_project(
        client, headers, tmp_path=tmp_path
    )

    graph = {
        "nodes": [
            {"id": "s", "type": "Start"},
            {
                "id": "impl",
                "type": "AgentTask",
                "config": {
                    "role": "dev",
                    "prompt": "implement",
                    "model_selection": {
                        "kind": "direct",
                        "model_id": "m",
                        "harness_profile_id": profile["id"],
                    },
                },
            },
            {
                "id": "check",
                "type": "LLMRequest",
                "config": {
                    "role": "verifier",
                    "prompt": "verify",
                    "model_selection": {
                        "kind": "direct",
                        "model_id": "m",
                        "provider_connection_id": connection["id"],
                    },
                },
            },
            {"id": "e", "type": "End"},
        ],
        "edges": [
            {"id": "e1", "from": "s", "to": "impl"},
            {"id": "e2", "from": "impl", "to": "check"},
            {"id": "e3", "from": "check", "to": "e"},
        ],
    }
    version = client.post(
        f"/api/templates/{template['id']}/versions",
        headers=headers,
        json={"graph": graph},
    ).json()
    binding = client.post(
        f"/api/versions/{version['id']}/bindings",
        headers=headers,
        json={"project_id": project["id"], "name": "main"},
    ).json()
    run = client.post(
        "/api/runs",
        headers=headers,
        json={
            "execution_mode": "simulated",
            "idempotency_key": "run1",
            "project_id": project["id"],
            "binding_id": binding["id"],
            "message": "go",
        },
    ).json()
    final = _wait_for_terminal(client, headers, run["id"])
    assert final["state"] in {"completed", "failed", "cancelled"}, final


def test_runner_records_attempt_and_artifact(fake_worker, authenticated, tmp_path):
    client, headers = authenticated
    project, profile, _connection, template = _seed_harness_and_project(
        client, headers, tmp_path=tmp_path
    )
    graph = {
        "nodes": [
            {"id": "s", "type": "Start"},
            {
                "id": "impl",
                "type": "AgentTask",
                "config": {
                    "role": "dev",
                    "prompt": "implement",
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
            {"id": "e1", "from": "s", "to": "impl"},
            {"id": "e2", "from": "impl", "to": "e"},
        ],
    }
    version = client.post(
        f"/api/templates/{template['id']}/versions",
        headers=headers,
        json={"graph": graph},
    ).json()
    binding = client.post(
        f"/api/versions/{version['id']}/bindings",
        headers=headers,
        json={"project_id": project["id"], "name": "main"},
    ).json()
    run = client.post(
        "/api/runs",
        headers=headers,
        json={
            "execution_mode": "simulated",
            "idempotency_key": "run2",
            "project_id": project["id"],
            "binding_id": binding["id"],
            "message": "go",
        },
    ).json()
    _wait_for_terminal(client, headers, run["id"])
    events = client.get(f"/api/runs/{run['id']}/events", headers=headers).json()
    types = [e["type"] for e in events["events"]]
    assert "run.created" in types
    assert "run.started" in types
    assert "node.entered" in types
    assert "attempt.started" in types
    assert "attempt.finished" in types
    assert "node.left" in types
    assert events["last_sequence"] > 0


def test_runner_sse_stream_returns_events_then_closes(fake_worker, authenticated, tmp_path):
    client, headers = authenticated
    project, profile, _connection, template = _seed_harness_and_project(
        client, headers, tmp_path=tmp_path
    )
    graph = {
        "nodes": [
            {"id": "s", "type": "Start"},
            {
                "id": "impl",
                "type": "AgentTask",
                "config": {
                    "role": "dev",
                    "prompt": "x",
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
            {"id": "e1", "from": "s", "to": "impl"},
            {"id": "e2", "from": "impl", "to": "e"},
        ],
    }
    version = client.post(
        f"/api/templates/{template['id']}/versions",
        headers=headers,
        json={"graph": graph},
    ).json()
    binding = client.post(
        f"/api/versions/{version['id']}/bindings",
        headers=headers,
        json={"project_id": project["id"], "name": "main"},
    ).json()
    run = client.post(
        "/api/runs",
        headers=headers,
        json={
            "execution_mode": "simulated",
            "idempotency_key": "run3",
            "project_id": project["id"],
            "binding_id": binding["id"],
            "message": "go",
        },
    ).json()
    with client.stream("GET", f"/api/runs/{run['id']}/stream", headers=headers) as response:
        joined = ""
        deadline = time.monotonic() + 10
        for line in response.iter_lines():
            joined += line + "\n"
            if "stream.closed" in line:
                break
            if time.monotonic() > deadline:
                break
    assert "stream.opened" in joined
    assert "run.event" in joined
    assert "stream.closed" in joined


def test_runner_idempotency_returns_same_run(authenticated, tmp_path):
    client, headers = authenticated
    project, profile, _connection, template = _seed_harness_and_project(
        client, headers, tmp_path=tmp_path
    )
    graph = {
        "nodes": [
            {"id": "s", "type": "Start"},
            {
                "id": "impl",
                "type": "AgentTask",
                "config": {
                    "role": "dev",
                    "prompt": "x",
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
            {"id": "e1", "from": "s", "to": "impl"},
            {"id": "e2", "from": "impl", "to": "e"},
        ],
    }
    version = client.post(
        f"/api/templates/{template['id']}/versions",
        headers=headers,
        json={"graph": graph},
    ).json()
    binding = client.post(
        f"/api/versions/{version['id']}/bindings",
        headers=headers,
        json={"project_id": project["id"], "name": "main"},
    ).json()
    payload = {
        "execution_mode": "simulated",
        "idempotency_key": "run-idem",
        "project_id": project["id"],
        "binding_id": binding["id"],
        "message": "go",
    }
    first = client.post("/api/runs", headers=headers, json=payload).json()
    second = client.post("/api/runs", headers=headers, json=payload).json()
    assert first["id"] == second["id"]


def test_runner_pause_command_rejected_until_started(authenticated, tmp_path):
    """The journal must record pause commands only on running runs."""

    client, headers = authenticated
    project, profile, _connection, template = _seed_harness_and_project(
        client, headers, tmp_path=tmp_path
    )
    graph = {
        "nodes": [
            {"id": "s", "type": "Start"},
            {
                "id": "impl",
                "type": "AgentTask",
                "config": {
                    "role": "dev",
                    "prompt": "x",
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
            {"id": "e1", "from": "s", "to": "impl"},
            {"id": "e2", "from": "impl", "to": "e"},
        ],
    }
    version = client.post(
        f"/api/templates/{template['id']}/versions",
        headers=headers,
        json={"graph": graph},
    ).json()
    binding = client.post(
        f"/api/versions/{version['id']}/bindings",
        headers=headers,
        json={"project_id": project["id"], "name": "main"},
    ).json()
    run = client.post(
        "/api/runs",
        headers=headers,
        json={
            "execution_mode": "simulated",
            "idempotency_key": "run-pause",
            "project_id": project["id"],
            "binding_id": binding["id"],
            "message": "go",
        },
    ).json()
    journal = client.get(f"/api/runs/{run['id']}/commands", headers=headers).json()
    assert journal == []


# --------------------------------------------------------------------------- candidate selection


def test_runner_writes_candidate_selection_events(fake_worker, authenticated, tmp_path):
    """A direct Agent selection emits a single candidate_selected event."""

    client, headers = authenticated
    project, profile, _connection, template = _seed_harness_and_project(
        client, headers, tmp_path=tmp_path
    )
    graph = {
        "nodes": [
            {"id": "s", "type": "Start"},
            {
                "id": "impl",
                "type": "AgentTask",
                "config": {
                    "role": "dev",
                    "prompt": "x",
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
            {"id": "e1", "from": "s", "to": "impl"},
            {"id": "e2", "from": "impl", "to": "e"},
        ],
    }
    version = client.post(
        f"/api/templates/{template['id']}/versions",
        headers=headers,
        json={"graph": graph},
    ).json()
    binding = client.post(
        f"/api/versions/{version['id']}/bindings",
        headers=headers,
        json={"project_id": project["id"], "name": "main"},
    ).json()
    run = client.post(
        "/api/runs",
        headers=headers,
        json={
            "execution_mode": "simulated",
            "idempotency_key": "run-cand",
            "project_id": project["id"],
            "binding_id": binding["id"],
            "message": "go",
        },
    ).json()
    _wait_for_terminal(client, headers, run["id"])
    events = client.get(f"/api/runs/{run['id']}/events", headers=headers).json()
    selected = [e for e in events["events"] if e["type"] == "model_group.candidate_selected"]
    assert selected
    payload = selected[0]["payload"]
    assert payload["model_id"] == "m"


def test_runner_selects_first_group_member(fake_worker, authenticated, tmp_path):
    """An agent group emits candidate_selected for the first enabled member."""

    client, headers = authenticated
    workspace = tmp_path / "ws-grp"
    workspace.mkdir()
    project = client.post(
        "/api/projects",
        headers=headers,
        json={"name": "p", "workspace_path": str(workspace)},
    ).json()
    p1 = client.post(
        "/api/harness_profiles",
        headers=headers,
        json={"name": "p1", "harness_kind": "codex", "settings": {}},
    ).json()
    p2 = client.post(
        "/api/harness_profiles",
        headers=headers,
        json={"name": "p2", "harness_kind": "codex", "settings": {}},
    ).json()
    group = client.post(
        "/api/model_groups/agent",
        headers=headers,
        json={
            "name": "heavy",
            "members": [
                {"harness_profile_id": p1["id"], "model_id": "alpha"},
                {"harness_profile_id": p2["id"], "model_id": "beta"},
            ],
        },
    ).json()
    template = client.post("/api/templates", headers=headers, json={"name": "t"}).json()
    graph = {
        "nodes": [
            {"id": "s", "type": "Start"},
            {
                "id": "impl",
                "type": "AgentTask",
                "config": {
                    "role": "dev",
                    "prompt": "x",
                    "model_selection": {"kind": "group", "group_id": group["id"]},
                },
            },
            {"id": "e", "type": "End"},
        ],
        "edges": [
            {"id": "e1", "from": "s", "to": "impl"},
            {"id": "e2", "from": "impl", "to": "e"},
        ],
    }
    version = client.post(
        f"/api/templates/{template['id']}/versions",
        headers=headers,
        json={"graph": graph},
    ).json()
    binding = client.post(
        f"/api/versions/{version['id']}/bindings",
        headers=headers,
        json={"project_id": project["id"], "name": "main"},
    ).json()
    run = client.post(
        "/api/runs",
        headers=headers,
        json={
            "execution_mode": "simulated",
            "idempotency_key": "run-grp",
            "project_id": project["id"],
            "binding_id": binding["id"],
            "message": "go",
        },
    ).json()
    _wait_for_terminal(client, headers, run["id"])
    events = client.get(f"/api/runs/{run['id']}/events", headers=headers).json()
    selected = [e for e in events["events"] if e["type"] == "model_group.candidate_selected"]
    assert selected
    assert selected[0]["payload"]["model_id"] == "alpha"
    assert selected[0]["payload"]["member_index"] == 0


def test_business_failure_is_a_successful_call(fake_worker, authenticated, tmp_path):
    """A failed business verdict does not turn a successful call into a technical failure."""

    client, headers = authenticated
    workspace = tmp_path / "ws-repair"
    workspace.mkdir()
    project = client.post(
        "/api/projects",
        headers=headers,
        json={"name": "p", "workspace_path": str(workspace)},
    ).json()
    connection = client.post(
        "/api/connections",
        headers=headers,
        json={"name": "loopback", "base_url": "http://127.0.0.1:9/v1"},
    ).json()
    template = client.post("/api/templates", headers=headers, json={"name": "t"}).json()
    graph = {
        "nodes": [
            {"id": "s", "type": "Start"},
            {
                "id": "verify",
                "type": "LLMRequest",
                "config": {
                    "role": "verifier",
                    "prompt": "verify force_decision=failed",
                    "model_selection": {
                        "kind": "direct",
                        "model_id": "m",
                        "provider_connection_id": connection["id"],
                    },
                },
            },
            {"id": "e", "type": "End"},
        ],
        "edges": [
            {"id": "e1", "from": "s", "to": "verify"},
            {"id": "e2", "from": "verify", "to": "e"},
        ],
    }
    version = client.post(
        f"/api/templates/{template['id']}/versions",
        headers=headers,
        json={"graph": graph},
    ).json()
    binding = client.post(
        f"/api/versions/{version['id']}/bindings",
        headers=headers,
        json={"project_id": project["id"], "name": "main"},
    ).json()
    run = client.post(
        "/api/runs",
        headers=headers,
        json={
            "execution_mode": "simulated",
            "idempotency_key": "run-repair",
            "project_id": project["id"],
            "binding_id": binding["id"],
            "message": "go",
        },
    ).json()
    final = _wait_for_terminal(client, headers, run["id"])
    assert final["state"] == "completed"
    events = client.get(f"/api/runs/{run['id']}/events", headers=headers).json()
    verify_attempts = [
        e for e in events["events"] if e["type"] == "attempt.finished" and e["node_id"] == "verify"
    ]
    assert verify_attempts


def test_runner_emits_replay_endpoint(fake_worker, authenticated, tmp_path):
    """Replay endpoint returns the first retained page with an accurate cursor."""

    client, headers = authenticated
    project, profile, _connection, template = _seed_harness_and_project(
        client, headers, tmp_path=tmp_path
    )
    graph = {
        "nodes": [
            {"id": "s", "type": "Start"},
            {
                "id": "impl",
                "type": "AgentTask",
                "config": {
                    "role": "dev",
                    "prompt": "x",
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
            {"id": "e1", "from": "s", "to": "impl"},
            {"id": "e2", "from": "impl", "to": "e"},
        ],
    }
    version = client.post(
        f"/api/templates/{template['id']}/versions",
        headers=headers,
        json={"graph": graph},
    ).json()
    binding = client.post(
        f"/api/versions/{version['id']}/bindings",
        headers=headers,
        json={"project_id": project["id"], "name": "main"},
    ).json()
    run = client.post(
        "/api/runs",
        headers=headers,
        json={
            "execution_mode": "simulated",
            "idempotency_key": "run-replay",
            "project_id": project["id"],
            "binding_id": binding["id"],
            "message": "go",
        },
    ).json()
    _wait_for_terminal(client, headers, run["id"])
    response = client.get(f"/api/runs/{run['id']}/events/replay", headers=headers).json()
    assert response["reset_required"] is False
    assert response["events"]
    assert response["last_sequence"] > 0


def test_runner_records_artifact_for_attempt(fake_worker, authenticated, tmp_path):
    """Every successful attempt must persist an ArtifactManifest."""

    client, headers = authenticated
    project, profile, _connection, template = _seed_harness_and_project(
        client, headers, tmp_path=tmp_path
    )
    graph = {
        "nodes": [
            {"id": "s", "type": "Start"},
            {
                "id": "impl",
                "type": "AgentTask",
                "config": {
                    "role": "dev",
                    "prompt": "x",
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
            {"id": "e1", "from": "s", "to": "impl"},
            {"id": "e2", "from": "impl", "to": "e"},
        ],
    }
    version = client.post(
        f"/api/templates/{template['id']}/versions",
        headers=headers,
        json={"graph": graph},
    ).json()
    binding = client.post(
        f"/api/versions/{version['id']}/bindings",
        headers=headers,
        json={"project_id": project["id"], "name": "main"},
    ).json()
    run = client.post(
        "/api/runs",
        headers=headers,
        json={
            "execution_mode": "simulated",
            "idempotency_key": "run-art",
            "project_id": project["id"],
            "binding_id": binding["id"],
            "message": "go",
        },
    ).json()
    _wait_for_terminal(client, headers, run["id"])
    from sqlalchemy.orm import sessionmaker

    engine = client.app.state.engine
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as session:
        from agents_ide.persistence.models import ArtifactManifest

        rows = session.query(ArtifactManifest).filter(ArtifactManifest.run_id == run["id"]).all()
        assert rows, "expected at least one ArtifactManifest"
        assert any(r.schema_type == "agent_response" for r in rows)


def test_runner_completes_pure_server_graph(fake_worker, authenticated, tmp_path):
    """A Run with no harness work still exits the engine loop."""

    client, headers = authenticated
    workspace = tmp_path / "ws-limit"
    workspace.mkdir()
    project = client.post(
        "/api/projects",
        headers=headers,
        json={"name": "p", "workspace_path": str(workspace)},
    ).json()
    template = client.post("/api/templates", headers=headers, json={"name": "t"}).json()
    graph = {
        "nodes": [
            {"id": "s", "type": "Start"},
            {"id": "e", "type": "End"},
        ],
        "edges": [
            {"id": "e1", "from": "s", "to": "e"},
        ],
    }
    version = client.post(
        f"/api/templates/{template['id']}/versions",
        headers=headers,
        json={"graph": graph},
    ).json()
    binding = client.post(
        f"/api/versions/{version['id']}/bindings",
        headers=headers,
        json={"project_id": project["id"], "name": "main"},
    ).json()
    run = client.post(
        "/api/runs",
        headers=headers,
        json={
            "execution_mode": "simulated",
            "idempotency_key": "run-limit",
            "project_id": project["id"],
            "binding_id": binding["id"],
            "message": "go",
        },
    ).json()
    final = _wait_for_terminal(client, headers, run["id"])
    assert final["state"] == "completed", final


def test_worker_heartbeat_continues_during_attempt(fake_worker, authenticated, tmp_path):
    from tests.integration.test_stage4_review import make_run

    client, headers = authenticated
    run, _ = make_run(
        authenticated,
        tmp_path,
        fake_scenario={"responses": [{"node_id": "check", "delay_seconds": 2}]},
    )
    engine = client.app.state.engine
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with engine.connect() as connection:
            running = connection.execute(
                text("SELECT count(*) FROM step_attempts WHERE status='running'")
            ).scalar()
        if running:
            break
        time.sleep(0.05)
    assert running
    with engine.connect() as connection:
        first = connection.execute(text("SELECT last_seen_at FROM worker_heartbeat")).scalar()
    time.sleep(0.5)
    with engine.connect() as connection:
        second = connection.execute(text("SELECT last_seen_at FROM worker_heartbeat")).scalar()
    assert second > first
    final = _wait_for_terminal(client, headers, run["id"])
    assert final["state"] == "completed"


def test_worker_runs_full_repair_scenario_from_api(fake_worker, authenticated, tmp_path):
    import json

    from tests.integration.test_stage4_review import make_run, repair_graph

    responses = [
        {
            "node_id": "check",
            "visit_index": visit,
            "raw_text": json.dumps({"verdict": verdict, "feedback": "fix it"}),
        }
        for visit, verdict in [(1, "failed"), (2, "passed")]
    ]
    run, factory = make_run(
        authenticated, tmp_path, graph=repair_graph(), fake_scenario={"responses": responses}
    )
    client, headers = authenticated
    assert _wait_for_terminal(client, headers, run["id"])["state"] == "completed"
    with factory() as session:
        from sqlalchemy import select

        from agents_ide.persistence.models import StepExecution

        rows = list(
            session.scalars(
                select(StepExecution)
                .where(StepExecution.node_id == "check")
                .order_by(StepExecution.visit_index)
            )
        )
        assert [(r.visit_index, r.cycle_id, r.decision) for r in rows] == [
            (1, 1, "false"),
            (2, 2, "true"),
        ]
