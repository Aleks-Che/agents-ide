"""Review regressions: actual transport, durable Runner state and process ownership."""

import importlib.util
import json
import socket
import sys
import threading
import time
from dataclasses import replace
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from sqlalchemy import select

from agents_ide.adapters.base import AgentAdapterRequest, ExternalOutcome
from agents_ide.adapters.opencode import (
    MAX_EVENT_BYTES,
    OpenCodeAdapter,
    ResponseLimit,
    RunSession,
    _validate_loopback_url,
    sse_events,
)
from agents_ide.engine.opencode_runtime import (
    OpenCodeRuntime,
    server_environment,
    validate_settings,
)
from agents_ide.engine.queue import claim_next_job
from agents_ide.engine.runner import Runner
from agents_ide.errors import AppError
from agents_ide.persistence.database import check_database
from agents_ide.persistence.models import (
    AgentSession,
    HarnessProfile,
    ProcessSupervision,
    Run,
    RunEvent,
)
from agents_ide.worker.processes import ProcessGroup, ProcessRegistry

FIXTURE = Path(__file__).parents[1] / "fixtures" / "fake_opencode_server.py"
spec = importlib.util.spec_from_file_location("review_opencode_fixture", FIXTURE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.fixture
def server(tmp_path):
    handler = type(
        "Scenario",
        (module.Handler,),
        {
            "sessions": {},
            "subscribers": [],
            "aborts": set(),
            "requests": [],
            "password": "private-local-password",
        },
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    adapter = OpenCodeAdapter(
        session=RunSession(
            f"http://127.0.0.1:{server.server_port}",
            "opencode",
            handler.password,
            str(tmp_path),
            {},
            "1.18.30-fixture",
        )
    )
    request = AgentAdapterRequest(
        "reviewer",
        "anthropic/claude-sonnet-4-20250514",
        "hello",
        {"work": {"scope": "P1"}, "input": {"task": "check"}},
        str(tmp_path),
        {},
        {},
    )
    try:
        yield handler, adapter, request
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_protocol_schema_context_and_event_filtering(server):
    handler, adapter, request = server
    events = []
    result = adapter.run(replace(request, emit_event=lambda t, p: events.append((t, p))))
    assert result.outcome == ExternalOutcome.SUCCEEDED, result
    contract = json.loads((FIXTURE.parent / "opencode-1.18.30-requests.json").read_text())
    for row in handler.requests:
        key = "session" if row["path"] == "/session" else "message"
        Draft202012Validator({**contract[key], "$defs": contract["$defs"]}).validate(row["body"])
    message = handler.requests[-1]["body"]
    assert message["model"] == {"providerID": "anthropic", "modelID": "claude-sonnet-4-20250514"}
    assert json.dumps(request.context_package) in message["parts"][1]["text"]
    assert any(t == "agent.session_created" for t, _ in events)
    assert any(t == "attempt.text_delta" and p["text"] == "hello" for t, p in events)
    assert "FOREIGN" not in json.dumps(events)
    assert result.raw_text == "hello from opencode"


def test_explicit_resume_uses_same_id_and_missing_session_creates_new(server):
    handler, adapter, request = server
    assert adapter.run(request).succeeded
    sid = adapter.external_session_id
    other = OpenCodeAdapter(session=adapter.session)
    assert other.run(replace(request, resume_session_id=sid)).succeeded
    assert other.external_session_id == sid
    del handler.sessions[sid]
    events = []
    assert other.run(
        replace(request, resume_session_id=sid, emit_event=lambda t, p: events.append(t))
    ).succeeded
    assert other.external_session_id != sid
    assert "agent.session_invalidated" in events


@pytest.mark.parametrize("status", [404, 429, 500])
def test_post_dispatch_http_error_never_permits_fallback(server, status):
    handler, adapter, request = server
    handler.response_status = status
    result = adapter.run(request)
    assert result.outcome == ExternalOutcome.UNKNOWN
    assert not result.no_effect
    assert len([r for r in handler.requests if r["path"].endswith("/message")]) == 1


def test_provider_error_in_successful_http_is_not_success(server):
    handler, adapter, request = server
    handler.response_error = {"name": "APIError", "data": {"message": "private-provider-error"}}
    result = adapter.run(request)
    assert result.outcome == ExternalOutcome.UNKNOWN
    assert "private-provider-error" not in repr(result)


def test_http_forbidden_is_permission_failure_without_fallback(server):
    handler, adapter, request = server
    handler.response_status = 403
    result = adapter.run(request)
    assert result.outcome == ExternalOutcome.PERMISSION_DENIED
    assert not result.no_effect


def test_response_limit_is_enforced_while_streaming_and_aborts(server):
    handler, adapter, request = server
    handler.response_size = 10000
    adapter.max_response_bytes = 1024
    result = adapter.run(request)
    assert result.outcome == ExternalOutcome.UNKNOWN
    assert any(r["path"].endswith("/abort") for r in handler.requests)


def test_permission_is_rejected_and_not_a_fallback(server):
    handler, adapter, request = server
    handler.pending_permission = True
    handler.delay = 0.4
    events = []
    result = adapter.run(replace(request, emit_event=lambda t, p: events.append(t)))
    assert result.outcome == ExternalOutcome.PERMISSION_DENIED
    assert any(r["body"] == {"reply": "reject"} for r in handler.requests)
    assert "agent.permission_requested" in events and "agent.permission_resolved" in events


def test_stream_loss_aborts_and_never_succeeds(server):
    handler, adapter, request = server
    handler.close_stream = True
    handler.delay = 0.4
    result = adapter.run(request)
    assert result.outcome == ExternalOutcome.UNKNOWN
    assert any(r["path"].endswith("/abort") for r in handler.requests)


def test_stop_during_slow_response_sends_native_abort(server):
    handler, adapter, request = server
    handler.delay = 3
    stop = threading.Event()

    def request_stop():
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if any(r["path"].endswith("/message") for r in handler.requests):
                stop.set()
                return
            time.sleep(0.01)

    timer = threading.Thread(target=request_stop)
    timer.start()
    started = time.monotonic()
    result = adapter.run(replace(request, stop_event=stop))
    timer.join()
    assert time.monotonic() - started < 2
    assert any(r["path"].endswith("/abort") for r in handler.requests)
    assert not result.succeeded


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:0",
        "http://127.0.0.1:bad",
        "http://127.0.0.1:4096/base",
        "http://127.0.0.1:4096?x=1",
        "http://user@127.0.0.1:4096",
        "http://127.0.0.1:65536",
    ],
)
def test_invalid_origins_are_rejected(url):
    with pytest.raises(AppError):
        _validate_loopback_url(url)


def test_split_sse_utf8_multiline_and_bound():
    payload = (
        'data: {"type":"message.part.delta",\r\ndata: "properties":{"delta":"тест"}}\r\n\r\n'
    ).encode()
    events = list(sse_events(bytes([b]) for b in payload))
    assert events[0]["properties"]["delta"] == "тест"
    with pytest.raises(ResponseLimit):
        list(sse_events([b"data:" + b"x" * MAX_EVENT_BYTES]))


@pytest.mark.parametrize(
    "settings",
    [
        {},
        {"auth": False},
        {"permission_mode": "workspace_write"},
        {"permission_mode": "no_tools", "serve_args": ["--hostname", "0.0.0.0"]},
        {"permission_mode": "no_tools", "env": {"NODE_OPTIONS": "x"}},
    ],
)
def test_unverified_policies_and_transport_overrides_are_blocked(settings):
    with pytest.raises(AppError):
        validate_settings(settings)


def test_environment_does_not_inherit_provider_or_runtime_injection(monkeypatch):
    for key in (
        "OPENAI_API_KEY",
        "HTTP_PROXY",
        "NODE_OPTIONS",
        "BUN_OPTIONS",
        "OPENCODE_CONFIG_CONTENT",
    ):
        monkeypatch.setenv(key, "do-not-inherit")
    env = server_environment("local-password")
    assert "do-not-inherit" not in json.dumps(env)
    assert env["PATH"]
    assert env["OPENCODE_SERVER_PASSWORD"] == "local-password"


@pytest.fixture
def launch_fixture(monkeypatch, tmp_path):
    original = ProcessGroup.start
    trace = tmp_path / "trace.jsonl"
    sessions = tmp_path / "sessions.json"
    launches = []

    def launch(self, argv, cwd, env, before_resume=None):
        if "serve" in argv:
            launches.append((argv, dict(env)))
            env = {**env, "FIXTURE_TRACE": str(trace), "FIXTURE_SESSIONS": str(sessions)}
            argv = [sys.executable, str(FIXTURE), *argv[1:]]
        return original(self, argv, cwd, env, before_resume)

    monkeypatch.setattr(ProcessGroup, "start", launch)
    return launches, trace


def seed(
    authenticated,
    tmp_path,
    roles=("implementer", "reviewer", "implementer"),
    group=False,
    permission_mode="no_tools",
    prompt="force_decision=passed",
):
    client, headers = authenticated

    def post(path, data):
        response = client.post("/api" + path, headers=headers, json=data)
        if path == "/runs" and permission_mode != "no_tools":
            assert response.status_code == 422, response.text
            assert "configuration_invalid" in response.text
            return response.json()
        assert response.status_code in {200, 201}, response.text
        return response.json()

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    project = post("/projects", {"name": "oc-project", "workspace_path": str(workspace)})
    profile = post(
        "/harness_profiles",
        {
            "name": "oc-profile",
            "harness_kind": "opencode",
            "executable_path": sys.executable,
            "settings": {"permission_mode": permission_mode},
        },
    )
    template = post("/templates", {"name": "oc-template"})
    selection = {
        "kind": "direct",
        "harness_profile_id": profile["id"],
        "model_id": "anthropic/claude-sonnet-4-20250514",
    }
    if group:
        grp = post(
            "/model_groups/agent",
            {
                "name": "oc-group",
                "members": [
                    {"harness_profile_id": profile["id"], "model_id": "anthropic/unavailable"},
                    {
                        "harness_profile_id": profile["id"],
                        "model_id": "anthropic/claude-sonnet-4-20250514",
                    },
                ],
            },
        )
        selection = {"kind": "group", "group_id": grp["id"]}
    nodes = [
        {"id": "start", "type": "Start"},
        *[
            {
                "id": f"a{i}",
                "type": "AgentTask",
                "config": {
                    "role": role,
                    "prompt": prompt,
                    "response_format": "json",
                    "model_selection": selection,
                },
            }
            for i, role in enumerate(roles)
        ],
        {"id": "end", "type": "End"},
    ]
    graph = {
        "nodes": nodes,
        "edges": [
            {"id": f"e{i}", "from": left["id"], "to": right["id"]}
            for i, (left, right) in enumerate(zip(nodes, nodes[1:], strict=False))
        ],
    }
    version = post(f"/templates/{template['id']}/versions", {"graph": graph})
    binding = post(
        f"/versions/{version['id']}/bindings", {"project_id": project["id"], "name": "oc-binding"}
    )
    run = post(
        "/runs",
        {
            "project_id": project["id"],
            "binding_id": binding["id"],
            "execution_mode": "real",
            "idempotency_key": "oc-run",
            "message": "go",
        },
    )
    return run, profile


def runner_for(client, settings):
    factory = client.app.state.session_factory
    job = claim_next_job(factory, worker_id="review6a", lease_seconds=300)
    assert job
    runner = Runner(
        session_factory=factory,
        worker_id="review6a",
        generation=job.generation,
        data_dir=settings.data_dir,
        secret_store=None,
        registry=ProcessRegistry(),
    )
    original = runner._call_monitored

    def monitor(fn, *args, **kwargs):
        errors = []

        def traced():
            try:
                return fn()
            except Exception:
                import traceback

                errors.append(traceback.format_exc())
                raise

        result = original(traced, *args, **kwargs)
        assert not errors, "\n".join(errors)
        return result

    runner._call_monitored = monitor
    return runner


def test_runner_keeps_password_isolates_roles_and_persists_sessions(
    authenticated, settings, tmp_path, launch_fixture
):
    run, _ = seed(authenticated, tmp_path)
    client, _ = authenticated
    runner = runner_for(client, settings)
    result = runner.execute(run["id"])
    assert result.final_state == "completed", result
    launches, trace = launch_fixture
    assert len(launches) == 1
    with client.app.state.session_factory() as db:
        sessions = list(db.scalars(select(AgentSession).order_by(AgentSession.started_at)))
        assert len(sessions) == 3
        assert sessions[0].external_session_id == sessions[2].external_session_id
        assert sessions[0].external_session_id != sessions[1].external_session_id
        assert all(s.external_turn_id and s.finished_at for s in sessions)
        assert all(
            json.loads(s.capabilities_json)["server_version"] == "1.18.30-fixture" for s in sessions
        )
        assert all(p.state == "finished" for p in db.scalars(select(ProcessSupervision)))
        saved = db.get(Run, run["id"])
        assert launches[0][1]["OPENCODE_SERVER_PASSWORD"] not in saved.runtime_json
        events = list(db.scalars(select(RunEvent)))
        assert any(e.type == "agent.session_resumed" for e in events)
        assert "FOREIGN" not in "".join(e.payload_json for e in events)
    assert (
        len(
            [
                json.loads(line)
                for line in trace.read_text().splitlines()
                if json.loads(line)["path"].endswith("/message")
            ]
        )
        == 3
    )
    assert not runner.registry.by_run(run["id"])


def test_group_auth_fallback_uses_new_native_session(
    authenticated, settings, tmp_path, launch_fixture
):
    run, _ = seed(authenticated, tmp_path, roles=("reviewer",), group=True)
    runner = runner_for(authenticated[0], settings)
    result = runner.execute(run["id"])
    assert result.final_state == "completed", result
    rows = [json.loads(line) for line in launch_fixture[1].read_text().splitlines()]
    messages = [r for r in rows if r["path"].endswith("/message")]
    assert [r["body"]["model"]["modelID"] for r in messages] == [
        "unavailable",
        "claude-sonnet-4-20250514",
    ]
    assert messages[0]["path"] != messages[1]["path"]


def test_preflight_blocks_unverified_write_before_launch(
    authenticated, settings, tmp_path, launch_fixture
):
    run, _ = seed(
        authenticated, tmp_path, roles=("implementer",), permission_mode="workspace_write"
    )
    assert "id" not in run
    assert not launch_fixture[0]


def test_probe_api_really_launches_caches_and_invalidates(authenticated, tmp_path, launch_fixture):
    client, headers = authenticated
    profile = client.post(
        "/api/harness_profiles",
        headers=headers,
        json={"name": "probe", "harness_kind": "opencode", "executable_path": sys.executable},
    ).json()
    probe = client.post(f"/api/harness_profiles/{profile['id']}/test", headers=headers)
    assert probe.status_code == 200, probe.text
    assert probe.json()["status"] == "ok", probe.json()
    catalog = client.get(f"/api/harness_profiles/{profile['id']}/models", headers=headers).json()
    assert catalog["status"] == "fresh" and len(catalog["models"]) == 2
    assert check_database(client.app.state.engine)
    current = client.get(f"/api/harness_profiles/{profile['id']}", headers=headers).json()
    updated = client.patch(
        f"/api/harness_profiles/{profile['id']}",
        headers=headers,
        json={"expected_version": current["version"], "executable_path": None},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["catalog_models"] == [] and updated.json()["last_test_status"] is None
    with client.app.state.session_factory() as db:
        row = db.get(HarnessProfile, profile["id"])
        assert row.executable_path is None


def test_occupied_port_is_not_attached_or_killed(tmp_path):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    try:
        with pytest.raises(AppError):
            OpenCodeRuntime.start(
                executable=sys.executable,
                workspace_path=tmp_path,
                port=listener.getsockname()[1],
                start_timeout=1,
            )
        assert listener.getsockname()[1]
    finally:
        listener.close()


def test_session_rolls_over_after_three_continuations(
    authenticated, settings, tmp_path, launch_fixture
):
    run, _ = seed(authenticated, tmp_path, roles=("reviewer",) * 5)
    runner = runner_for(authenticated[0], settings)
    assert runner.execute(run["id"]).final_state == "completed"
    with authenticated[0].app.state.session_factory() as db:
        rows = list(db.scalars(select(AgentSession).order_by(AgentSession.started_at)))
        assert [r.resume_count for r in rows] == [0, 1, 2, 3, 0]
        assert len({r.external_session_id for r in rows[:4]}) == 1
        assert rows[-1].external_session_id != rows[0].external_session_id


def test_stop_resume_restarts_owned_server_and_resumes_native_session(
    authenticated, settings, tmp_path, launch_fixture
):
    client, headers = authenticated
    run, _ = seed(authenticated, tmp_path, roles=("reviewer",), prompt="slow force_decision=passed")
    runner = runner_for(client, settings)
    results, failures = [], []

    def execute():
        try:
            results.append(runner.execute(run["id"]))
        except Exception as exc:
            failures.append(exc)

    thread = threading.Thread(target=execute)
    thread.start()
    trace = launch_fixture[1]
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if trace.exists() and any(
            json.loads(line)["path"].endswith("/message") for line in trace.read_text().splitlines()
        ):
            break
        time.sleep(0.02)
    else:
        pytest.fail("Harness message was not dispatched")

    def control(kind):
        current = client.get(f"/api/runs/{run['id']}", headers=headers).json()
        response = client.post(
            f"/api/runs/{run['id']}/commands",
            headers=headers,
            json={
                "command_id": kind,
                "command_type": kind,
                "expected_state_version": current["state_version"],
            },
        )
        assert response.status_code == 200, response.text

    control("stop")
    thread.join(timeout=20)
    assert not thread.is_alive() and not failures, failures
    assert results[0].final_state == "stopped", results
    assert not runner.registry.by_run(run["id"])
    control("resume")
    next_runner = runner_for(client, settings)
    final = next_runner.execute(run["id"])
    assert final.final_state == "completed", final
    assert len(launch_fixture[0]) == 2
    assert (
        launch_fixture[0][0][1]["OPENCODE_SERVER_PASSWORD"]
        != launch_fixture[0][1][1]["OPENCODE_SERVER_PASSWORD"]
    )
    with client.app.state.session_factory() as db:
        rows = list(db.scalars(select(AgentSession).order_by(AgentSession.started_at)))
        assert len(rows) == 2 and rows[0].external_session_id == rows[1].external_session_id
        assert all(p.state == "finished" for p in db.scalars(select(ProcessSupervision)))


def test_catalog_probe_does_not_overwrite_concurrent_profile_edit(authenticated, monkeypatch):
    import agents_ide.engine.opencode_runtime as runtime_module

    client, headers = authenticated
    profile = client.post(
        "/api/harness_profiles",
        headers=headers,
        json={"name": "race", "harness_kind": "opencode", "executable_path": sys.executable},
    ).json()

    def probe(*args):
        with client.app.state.session_factory() as db:
            row = db.get(HarnessProfile, profile["id"])
            row.version += 1
            row.name = "edited"
            db.commit()
        return "1.18.30", ("provider/model",)

    monkeypatch.setattr(runtime_module, "probe_executable", probe)
    response = client.post(f"/api/harness_profiles/{profile['id']}/test", headers=headers)
    assert response.status_code == 409
    with client.app.state.session_factory() as db:
        row = db.get(HarnessProfile, profile["id"])
        assert row.name == "edited" and row.catalog_models_json == "[]"
