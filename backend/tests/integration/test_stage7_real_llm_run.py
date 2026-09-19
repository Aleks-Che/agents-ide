"""Stage 7 end-to-end: real LLM Run via the worker against a local provider.

These tests spawn the real worker subprocess exactly like the stage 4 engine
runner tests, but the LLMRequest node points at a local OpenAI-compatible
server instead of the fake adapter. Credential isolation across group
candidates is verified on Windows where DPAPI can store secrets.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

WORKER_ARGV = [sys.executable, "-m", "agents_ide", "worker"]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    authorizations: list[str | None] = []
    responses: dict[str, dict] = {}
    bodies: list[dict] = []

    def log_message(self, *args: object) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length) if length else b"{}".encode()
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            body = {}
        type(self).authorizations.append(self.headers.get("Authorization"))
        type(self).bodies.append(body)
        route = self.responses.get(body.get("model"), self.responses.get("default", {}))
        status = route.get("status", 200)
        if status >= 300:
            payload = json.dumps(route.get("body", {"error": {"message": "no"}})).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        content = route.get("content", json.dumps({"verdict": "passed", "feedback": "ok"}))
        payload = json.dumps(
            {
                "choices": [{"message": {"role": "assistant", "content": content}}],
                "usage": {"total_tokens": 9},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def provider_server() -> Iterator[tuple[ThreadingHTTPServer, str]]:
    port = _free_port()
    Handler.authorizations = []
    Handler.bodies = []
    Handler.responses = {"default": {"status": 200}}
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{port}/v1"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def fake_worker(authenticated, settings) -> Iterator[subprocess.Popen[bytes]]:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "AGENTS_IDE_DATA_DIR": str(settings.data_dir),
        "AGENTS_IDE_HEARTBEAT_SECONDS": "0.1",
        "AGENTS_IDE_WORKER_STALE_SECONDS": "5",
    }
    worker = subprocess.Popen(WORKER_ARGV, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 15
        engine = authenticated[0].app.state.engine
        last_seen = None
        while time.monotonic() < deadline:
            from sqlalchemy import text

            with engine.connect() as connection:
                last_seen = connection.execute(
                    text("SELECT last_seen_at FROM worker_heartbeat")
                ).scalar()
            if last_seen:
                break
            if worker.poll() is not None:
                raise RuntimeError("worker exited prematurely")
            time.sleep(0.1)
        assert last_seen, "worker did not report its heartbeat"
        yield worker
    finally:
        worker.terminate()
        worker.communicate(timeout=10)


def _wait_for_terminal(client, headers, run_id: str, timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/runs/{run_id}", headers=headers)
        if response.status_code == 200:
            state = response.json().get("state")
            if state in {"completed", "failed", "cancelled", "waiting_input"}:
                return response.json()
        time.sleep(0.1)
    return client.get(f"/api/runs/{run_id}", headers=headers).json()


def _seed_project(client, headers, tmp_path: Path, *, base_url: str, secret: str | None = None):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    project = client.post(
        "/api/projects",
        headers=headers,
        json={"name": "p", "workspace_path": str(workspace)},
    ).json()
    payload: dict = {"name": "provider", "base_url": base_url}
    if secret is not None:
        payload["secret"] = secret
    connection = client.post("/api/connections", headers=headers, json=payload).json()
    template = client.post("/api/templates", headers=headers, json={"name": "t"}).json()
    return project, connection, template


@pytest.mark.parametrize(
    "raw_text,processing,error",
    [
        ('{"answer":"NO"}', {}, None),
        ('<think>Reasoning</think>\n{"answer":"NO"}', {}, "invalid_json"),
        (
            '<think>Reasoning</think>\n{"answer":"NO"}',
            {"strip_thinking_tags": True},
            None,
        ),
        (
            '<think>Reasoning</think>\n```json\n{"answer":"NO"}\n```',
            {"strip_thinking_tags": True, "extract_json": True},
            None,
        ),
        (
            '<think>Example: {"answer":"YES"}</think>\n{"answer":"NO"}',
            {"extract_json": True},
            None,
        ),
        (
            'Response: <think>Reasoning</think>\n```json\n{"answer":"NO"}\n```',
            {"extract_json": True},
            None,
        ),
        (
            '<think>Reasoning</think>\n{"answer":42}',
            {"extract_json": True},
            "schema_mismatch",
        ),
        ('```json\n{"answer":42}\n```', {"extract_json": True}, "schema_mismatch"),
    ],
)
def test_real_llm_run_completes_via_worker(
    fake_worker, authenticated, provider_server, tmp_path, raw_text, processing, error
):
    server, base_url = provider_server
    Handler.responses = {"default": {"status": 200, "content": raw_text}}
    client, headers = authenticated
    project, connection, template = _seed_project(client, headers, tmp_path, base_url=base_url)
    graph = {
        "nodes": [
            {"id": "s", "type": "Start"},
            {
                "id": "verify",
                "type": "LLMRequest",
                "config": {
                    "role": "verifier",
                    "prompt": "verify the implementation",
                    "response_format": "json",
                    "json_processing": processing,
                    "output_schema": {
                        "type": "object",
                        "properties": {"answer": {"type": "string"}},
                        "required": ["answer"],
                        "additionalProperties": False,
                    },
                    "model_selection": {
                        "kind": "direct",
                        "model_id": "alpha",
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
        f"/api/templates/{template['id']}/versions", headers=headers, json={"graph": graph}
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
            "execution_mode": "real",
            "idempotency_key": "real-llm-1",
            "project_id": project["id"],
            "binding_id": binding["id"],
            "message": "go",
        },
    ).json()
    final = _wait_for_terminal(client, headers, run["id"])
    assert final["state"] == ("waiting_input" if error else "completed"), final
    if error:
        assert final["waiting_reason"]["details"]["reason"] == error
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from agents_ide.persistence.models import ArtifactManifest, StepExecution

    with Session(client.app.state.engine) as session:
        artifact = session.scalar(
            select(ArtifactManifest).where(
                ArtifactManifest.run_id == run["id"],
                ArtifactManifest.schema_type == "llm_response",
            )
        )
        result = json.loads(artifact.body_json)
        assert result["raw_text"] == raw_text
        assert result["validated"] == (None if error else {"answer": "NO"})
        if not error:
            execution = session.scalar(
                select(StepExecution).where(
                    StepExecution.run_id == run["id"], StepExecution.node_id == "verify"
                )
            )
            assert json.loads(execution.validated_result_json) == {"answer": "NO"}
    events = client.get(f"/api/runs/{run['id']}/events", headers=headers).json()
    assert any(e["type"] == "attempt.finished" for e in events["events"])
    assert Handler.authorizations  # the worker actually called the provider


@pytest.mark.skipif(os.name != "nt", reason="DPAPI secrets are Windows-only")
@pytest.mark.parametrize("status", [404, 503, 429])
def test_llm_group_fallback_across_connections_isolates_credentials(
    fake_worker, authenticated, provider_server, tmp_path, status
):
    server, base_url = provider_server
    Handler.authorizations = []
    Handler.responses = {
        "alpha": {
            "status": status,
            "body": {
                "error": {
                    "message": "The Token Plan usage limit has been reached. (2067)"
                    if status == 429
                    else "model not found"
                }
            },
        },
        "beta": {"status": 200, "content": json.dumps({"verdict": "passed"})},
    }
    client, headers = authenticated
    workspace = tmp_path / "ws-grp"
    workspace.mkdir()
    project = client.post(
        "/api/projects",
        headers=headers,
        json={"name": "p", "workspace_path": str(workspace)},
    ).json()
    first = client.post(
        "/api/connections",
        headers=headers,
        json={"name": "first", "base_url": base_url, "secret": "sk-first"},
    ).json()
    second = client.post(
        "/api/connections",
        headers=headers,
        json={"name": "second", "base_url": base_url, "secret": "sk-second"},
    ).json()
    group = client.post(
        "/api/model_groups/llm",
        headers=headers,
        json={
            "name": "verifiers",
            "members": [
                {"provider_connection_id": first["id"], "model_id": "alpha"},
                {"provider_connection_id": second["id"], "model_id": "beta"},
            ],
        },
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
                    "prompt": "verify",
                    "response_format": "json",
                    "model_selection": {"kind": "group", "group_id": group["id"]},
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
        f"/api/templates/{template['id']}/versions", headers=headers, json={"graph": graph}
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
            "execution_mode": "real",
            "idempotency_key": "real-llm-group",
            "project_id": project["id"],
            "binding_id": binding["id"],
            "message": "go",
        },
    ).json()
    final = _wait_for_terminal(client, headers, run["id"])
    if status == 503:
        assert final["state"] == "waiting_input", final
        assert Handler.authorizations == ["Bearer sk-first"]
        return
    assert final["state"] == "completed", final
    events = client.get(f"/api/runs/{run['id']}/events", headers=headers).json()
    switched = [e for e in events["events"] if e["type"] == "model_group.candidate_switched"]
    assert switched
    if status == 429:
        assert Handler.authorizations == ["Bearer sk-first", "Bearer sk-second"]
    assert any("sk-first" in (auth or "") for auth in Handler.authorizations)
    assert any("sk-second" in (auth or "") for auth in Handler.authorizations)
    assert any("sk-first" in (auth or "") for auth in Handler.authorizations) and any(
        "sk-second" in (auth or "") for auth in Handler.authorizations
    )


def test_command_and_collected_evidence_reach_real_http_worker(
    fake_worker, authenticated, provider_server, tmp_path
):
    from sqlalchemy import select
    from test_stage7_review import chain, check_node, collect_node, verify_node

    from agents_ide.persistence.models import StepExecution

    client, headers = authenticated
    project, connection, template = _seed_project(
        client, headers, tmp_path, base_url=provider_server[1]
    )
    verifier = verify_node()
    verifier["config"]["model_selection"]["provider_connection_id"] = connection["id"]
    graph = chain(
        check_node(
            "from pathlib import Path; Path('a.txt').write_text('actual-code-evidence'); "
            "print('test-output-evidence'); raise SystemExit(1)"
        ),
        collect_node(
            [{"kind": "file", "path": "a.txt"}, {"kind": "command_report", "command_id": "cmd0"}]
        ),
        verifier,
    )
    version = client.post(
        f"/api/templates/{template['id']}/versions", headers=headers, json={"graph": graph}
    )
    assert version.status_code == 201, version.text
    binding = client.post(
        f"/api/versions/{version.json()['id']}/bindings",
        headers=headers,
        json={"project_id": project["id"], "name": "evidence"},
    ).json()
    run = client.post(
        "/api/runs",
        headers=headers,
        json={
            "execution_mode": "real",
            "idempotency_key": "http-evidence",
            "project_id": project["id"],
            "binding_id": binding["id"],
            "message": "go",
        },
    )
    assert run.status_code == 201, run.text
    final = _wait_for_terminal(client, headers, run.json()["id"])
    assert final["state"] == "completed", final
    sent = Handler.bodies[-1]["messages"][0]["content"]
    assert "actual-code-evidence" in sent and "test-output-evidence" in sent
    with client.app.state.session_factory() as session:
        step = session.scalar(
            select(StepExecution).where(
                StepExecution.run_id == run.json()["id"], StepExecution.node_id == "verify"
            )
        )
        assert step.decision == "false"
