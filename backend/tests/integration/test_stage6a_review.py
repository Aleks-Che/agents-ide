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
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator
from sqlalchemy import select

from agents_ide.adapters.base import AgentAdapterRequest, ExternalOutcome
from agents_ide.adapters.opencode import (
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
    StepAttempt,
    StepExecution,
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


def test_paused_session_missing_does_not_dispatch_new_task(server):
    handler, adapter, request = server
    result = adapter.run(replace(request, resume_session_id="ses_missing", resume_required=True))
    assert result.error.code == "session_resume_unavailable"
    assert result.no_effect
    assert not any(r["kind"] == "post" for r in handler.requests)


def test_request_loaded_before_pause_update_remains_compatible(server):
    _, adapter, request = server
    legacy_request = SimpleNamespace(
        **{key: value for key, value in vars(request).items() if key != "resume_required"}
    )
    assert adapter.run(legacy_request).succeeded


@pytest.mark.parametrize("status", [400, 404, 408, 429, 500])
def test_post_dispatch_http_error_permits_handoff_after_process_stop(server, status):
    handler, adapter, request = server
    handler.response_status = status
    result = adapter.run(request)
    assert result.outcome == ExternalOutcome.UNAVAILABLE
    assert result.can_handoff
    assert not result.no_effect
    assert len([r for r in handler.requests if r["path"].endswith("/message")]) == 1


@pytest.mark.parametrize("name", ["APIError", "ProviderAuthError", "UnknownError", "FutureError"])
def test_provider_error_in_successful_http_permits_handoff(server, name):
    handler, adapter, request = server
    handler.response_error = {"name": name, "data": {"message": "private-provider-error"}}
    result = adapter.run(request)
    assert result.outcome == ExternalOutcome.UNAVAILABLE
    assert "private-provider-error" not in repr(result)
    assert result.can_handoff
    assert not result.no_effect


@pytest.mark.parametrize("prior_output", [False, True])
@pytest.mark.parametrize(
    "status,message,retryable,code",
    [
        (
            429,
            "The Token Plan usage limit has been reached. (2067)",
            True,
            "provider_quota_exhausted",
        ),
        (429, "Too many requests", True, "provider_rate_limited"),
        (402, "Payment required", True, "provider_quota_exhausted"),
        (400, "Connection prematurely closed BEFORE response", False, "provider_unavailable"),
        (403, "Forbidden", False, "provider_unauthorized"),
        (404, "Model does not exist", False, "provider_unavailable"),
        (422, "Invalid parameter", False, "provider_unavailable"),
        (None, "Unknown provider failure", False, "provider_unavailable"),
        (
            None,
            "Cannot connect to API: Unable to connect. Is the computer able to access the url?",
            True,
            "provider_unavailable",
        ),
        (503, "Service unavailable", False, "provider_unavailable"),
        (408, "Request timeout", False, "provider_unavailable"),
    ],
)
def test_native_provider_failure_allows_handoff_without_claiming_no_effect(
    server, prior_output, status, message, retryable, code
):
    handler, adapter, request = server
    handler.auth_only_error = not prior_output
    handler.response_error = {
        "name": "APIError",
        "data": {"statusCode": status, "isRetryable": retryable, "message": message},
    }
    result = adapter.run(request)
    assert result.outcome == ExternalOutcome.UNAVAILABLE
    assert result.error.code == code
    assert result.can_handoff
    assert not result.no_effect
    assert result.error.retry_safety != "safe"


@pytest.mark.parametrize("prior_output", [False, True])
def test_native_provider_401_is_safe_only_without_prior_output(server, prior_output):
    handler, adapter, request = server
    handler.auth_only_error = not prior_output
    handler.response_error = {
        "name": "APIError",
        "data": {"statusCode": 401, "isRetryable": False, "message": "Synthetic key rejected"},
    }
    result = adapter.run(request)
    if prior_output:
        assert result.outcome == ExternalOutcome.UNAVAILABLE
        assert result.can_handoff
        assert not result.no_effect
    else:
        assert result.outcome == ExternalOutcome.UNAVAILABLE
        assert result.error.code == "provider_unauthorized"
        assert result.error.retry_safety == "safe" and result.no_effect


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
    assert result.outcome == ExternalOutcome.UNAVAILABLE
    assert result.can_handoff
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
    assert result.outcome == ExternalOutcome.UNAVAILABLE
    assert result.can_handoff
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
        list(sse_events([b"data:" + b"x" * 1024], limit=1024))


def test_large_tool_events_and_long_stream_do_not_abort(server):
    handler, adapter, request = server
    events = []
    result = adapter.run(
        replace(request, prompt="large tool stream", emit_event=lambda t, p: events.append((t, p)))
    )
    assert result.succeeded, result
    assert not handler.aborts
    tools = [p for t, p in events if t == "agent.tool_call"]
    assert len(tools) == 110
    assert all(p["status"] == "completed" and p["summary"] == "status.md" for p in tools)
    assert all(len(json.dumps(p)) < 1000 for p in tools)
    archived = [p for t, p in events if t == "agent.native_event"]
    assert sum(len(json.dumps(p)) for p in archived) > 10 * 1024 * 1024


def test_tool_output_burst_keeps_progress_current_with_slow_event_storage(server):
    handler, adapter, request = server
    events = []

    def persist(kind, payload):
        # A durable event callback must not be called for every output chunk.
        time.sleep(0.005)
        events.append((kind, payload))

    result = adapter.run(replace(request, prompt="tool output burst slow", emit_event=persist))
    assert result.succeeded, result
    assert not handler.aborts
    progress = [p for kind, p in events if kind == "agent.tool_call"]
    assert [(p["call_id"], p["status"]) for p in progress] == [
        (f"call_{call}", status)
        for call in ("first", "second")
        for status in ("pending", "running", "completed")
    ]
    snapshots = [
        p["payload"]["properties"]["part"]
        for kind, p in events
        if kind == "agent.native_event" and p["native_type"] == "message.part.updated"
    ]
    assert 6 <= len(snapshots) < 30
    assert [p["state"]["output"] for p in snapshots if p["state"]["status"] == "completed"] == [
        "complete output",
        "complete output",
    ]
    assert any(kind == "attempt.text_delta" and p["text"] == "hello" for kind, p in events)


def test_sse_accepts_single_frame_above_old_response_limit():
    event = {"type": "message.part.updated", "properties": {"output": "x" * (11 * 1024 * 1024)}}
    body = b"data: " + json.dumps(event).encode() + b"\n\n"
    assert list(sse_events(body[i : i + 65536] for i in range(0, len(body), 65536))) == [event]


def test_explicit_node_deadline_is_not_replaced_by_adapter_default(server):
    handler, adapter, request = server
    adapter.timeout_seconds = 0.1
    handler.delay = 0.4
    assert adapter.run(replace(request, deadline_at=time.time() + 5)).succeeded


def test_event_stream_can_take_longer_than_old_startup_limit(server, monkeypatch):
    handler, adapter, request = server
    original = handler.do_GET

    def delayed(self):
        if self.path.startswith("/event"):
            time.sleep(3.3)
        return original(self)

    monkeypatch.setattr(handler, "do_GET", delayed)
    result = adapter.run(request)
    assert result.succeeded, result


def test_stream_error_keeps_diagnostic_reason(server):
    handler, adapter, request = server
    handler.close_stream = True
    result = adapter.run(request)
    assert result.error.code == "event_stream_lost"
    assert result.error.details["reason"] == "event_stream_error"


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


def test_native_environment_preserves_user_tools_and_policy():
    env = server_environment("local-password", permission_mode="native")
    assert "permission" not in json.loads(env["OPENCODE_CONFIG_CONTENT"])
    assert "OPENCODE_DISABLE_PROJECT_CONFIG" not in env
    validate_settings({"permission_mode": "native", "auto_approve": True})


@pytest.mark.parametrize("automatic", [True, False])
def test_native_permission_reply_is_explicit_or_automatic(server, automatic):
    handler, adapter, request = server
    adapter.session = replace(
        adapter.session, settings={"permission_mode": "native", "auto_approve": automatic}
    )
    events, incoming = [], []

    def emit(kind, payload):
        events.append((kind, payload))
        if kind == "agent.input_requested":
            incoming.append(
                {
                    "command_id": "approve",
                    "permission_id": payload["permission_id"],
                    "permission_reply": "once",
                    "text": "approve",
                }
            )

    result = adapter.run(
        replace(
            request,
            prompt="permission slow",
            emit_event=emit,
            receive_message=lambda: incoming.pop(0) if incoming else None,
        )
    )
    assert result.succeeded
    assert handler.requests[0]["body"]["permission"] == []
    replies = [r for r in handler.requests if r["path"].startswith("/permission/")]
    assert len(replies) == 1 and replies[0]["body"] == {"reply": "once"}
    assert not handler.aborts
    assert any(t == "agent.input_requested" for t, _ in events) != automatic
    assert any(t == "agent.permission_resolved" for t, _ in events)


def test_broken_tool_stream_is_aborted_not_returned_as_success(server):
    handler, adapter, request = server
    events = []
    result = adapter.run(
        replace(
            request, prompt="broken tool stream slow", emit_event=lambda t, p: events.append((t, p))
        )
    )
    assert result.outcome == ExternalOutcome.UNAVAILABLE
    assert result.can_handoff
    assert result.error.code == "provider_tool_protocol_invalid"
    assert not result.no_effect
    assert handler.aborts
    assert len([e for e in events if e[0] == "attempt.text_delta"]) <= 17


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
    node_overrides=None,
    first_group_model="unavailable",
    last_group_model="claude-sonnet-4-20250514",
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
                    {
                        "harness_profile_id": profile["id"],
                        "model_id": f"anthropic/{first_group_model}",
                    },
                    {
                        "harness_profile_id": profile["id"],
                        "model_id": f"anthropic/{last_group_model}",
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
                    **({"harness_settings": node_overrides[i]} if node_overrides else {}),
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


def test_node_overrides_reach_preflight_runtime_and_do_not_reuse_other_permissions(
    authenticated, settings, tmp_path, launch_fixture
):
    overrides = [
        {"opencode": {"permission_mode": "native", "auto_approve": True}},
        {"opencode": {"permission_mode": "no_tools", "auto_approve": False}},
    ]
    run, _ = seed(
        authenticated, tmp_path, roles=("implementer", "implementer"), node_overrides=overrides
    )
    runner = runner_for(authenticated[0], settings)
    assert runner.execute(run["id"]).final_state == "completed"
    launches, trace = launch_fixture
    assert len(launches) == 2
    assert "--pure" not in launches[0][0] and "--pure" in launches[1][0]
    rows = [json.loads(line) for line in trace.read_text().splitlines()]
    sessions = [row["body"] for row in rows if row["path"] == "/session"]
    assert len(sessions) == 2
    assert sessions[0]["permission"] == []
    assert sessions[1]["permission"] == [{"permission": "*", "pattern": "*", "action": "deny"}]


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


@pytest.mark.parametrize("recover", [False, True])
@pytest.mark.parametrize(
    "model,code",
    [
        ("quota-exhausted", "provider_quota_exhausted"),
        ("connection-failed", "provider_unavailable"),
        ("bad-request", "provider_unavailable"),
    ],
)
def test_group_provider_handoff_preserves_partial_work_and_survives_recovery(
    authenticated, settings, tmp_path, launch_fixture, monkeypatch, recover, model, code
):
    from agents_ide.persistence.models import QueueJob

    run, _ = seed(
        authenticated,
        tmp_path,
        roles=("reviewer",),
        group=True,
        first_group_model=model,
    )
    client, _ = authenticated
    original = Runner._save_attempt
    crashed = False

    def save(self, visit, attempt_id, result, validation_error):
        nonlocal crashed
        original(self, visit, attempt_id, result, validation_error)
        if recover and result.can_handoff and not crashed:
            crashed = True
            raise RuntimeError("crash after durable handoff")

    monkeypatch.setattr(Runner, "_save_attempt", save)
    runner = runner_for(client, settings)
    if recover:
        with pytest.raises(RuntimeError, match="crash after durable handoff"):
            runner.execute(run["id"])
        with client.app.state.session_factory() as db:
            job = db.scalar(select(QueueJob))
            job.lease_expires_at = 0
            job.owner_pid, job.owner_create_time = 99999999, 1
            db.commit()
        runner = runner_for(client, settings)
    result = runner.execute(run["id"])
    assert result.final_state == "completed", result
    assert (tmp_path / "workspace" / "partial-work.txt").read_text() == "preserve this work"
    rows = [json.loads(line) for line in launch_fixture[1].read_text().splitlines()]
    messages = [r for r in rows if r["path"].endswith("/message")]
    assert [r["body"]["model"]["modelID"] for r in messages] == [
        model,
        "claude-sonnet-4-20250514",
    ]
    assert messages[0]["path"] == messages[1]["path"]  # retained OpenCode conversation
    assert "Preserve completed work" in messages[1]["body"]["parts"][0]["text"]
    context = json.loads(messages[1]["body"]["parts"][1]["text"].split("\n", 1)[1])
    assert context["agent_handoff"]["tool_calls"][0]["summary"] == "partial-work.txt"
    with client.app.state.session_factory() as db:
        attempts = list(
            db.scalars(
                select(StepAttempt)
                .join(StepExecution, StepExecution.id == StepAttempt.execution_id)
                .where(StepExecution.run_id == run["id"], StepExecution.node_id == "a0")
                .order_by(StepAttempt.started_at)
            )
        )
        assert [a.status for a in attempts] == ["failed", "succeeded"]
        details = json.loads(attempts[0].error_details_json)
        assert details["can_handoff"] and not details["no_effect"]
        assert attempts[0].error_code == code
        switched = list(
            db.scalars(
                select(RunEvent).where(
                    RunEvent.run_id == run["id"], RunEvent.type == "model_group.candidate_switched"
                )
            )
        )
        assert len(switched) == 1
        assert json.loads(switched[0].payload_json)["reason"] == code
    assert len(launch_fixture[0]) == 2  # old process tree stopped before the next candidate
    assert not runner.registry.by_run(run["id"])


@pytest.mark.parametrize(
    "first,last,code",
    [
        ("quota-exhausted", "quota-empty", "provider_quota_exhausted"),
        ("connection-failed", "connection-empty", "provider_unavailable"),
        ("bad-request", "bad-request-empty", "provider_unavailable"),
    ],
)
def test_group_waits_only_after_all_candidates_are_unavailable(
    authenticated, settings, tmp_path, launch_fixture, first, last, code
):
    run, _ = seed(
        authenticated,
        tmp_path,
        roles=("reviewer",),
        group=True,
        first_group_model=first,
        last_group_model=last,
    )
    result = runner_for(authenticated[0], settings).execute(run["id"])
    assert result.final_state == "waiting_input"
    assert result.waiting_reason.code == "model_group_exhausted"
    assert len(result.waiting_reason.details["candidates"]) == 2
    assert all(c["reason"] == code for c in result.waiting_reason.details["candidates"])
    rows = [json.loads(line) for line in launch_fixture[1].read_text().splitlines()]
    assert len([r for r in rows if r["path"].endswith("/message")]) == 2
    with authenticated[0].app.state.session_factory() as db:
        runtime = json.loads(db.get(Run, run["id"]).runtime_json)
        assert runtime["agent_handoff"]["tool_calls"][0]["summary"] == "partial-work.txt"


@pytest.mark.parametrize("model", ["quota-exhausted", "connection-failed", "bad-request"])
def test_provider_handoff_waits_if_old_process_stop_is_unconfirmed(
    authenticated, settings, tmp_path, launch_fixture, monkeypatch, model
):
    run, _ = seed(
        authenticated,
        tmp_path,
        roles=("reviewer",),
        group=True,
        first_group_model=model,
    )
    runner = runner_for(authenticated[0], settings)
    original = runner._close_harness_live
    failed = False

    def close():
        nonlocal failed
        if runner._opencode_live and not failed:
            failed = True
            raise AppError("process_not_responding", "Unconfirmed process stop", 409)
        original()

    monkeypatch.setattr(runner, "_close_harness_live", close)
    result = runner.execute(run["id"])
    assert result.final_state == "waiting_input"
    rows = [json.loads(line) for line in launch_fixture[1].read_text().splitlines()]
    messages = [r for r in rows if r["path"].endswith("/message")]
    assert len(messages) == 1
    with authenticated[0].app.state.session_factory() as db:
        runtime = json.loads(db.get(Run, run["id"]).runtime_json)
        assert not runtime.get("agent_handoff")


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


@pytest.mark.parametrize("path", ["/global/health", "/path", "/config/providers"])
def test_stalled_startup_probe_reconnects_without_restarting_task(
    authenticated, settings, tmp_path, launch_fixture, monkeypatch, path
):
    import agents_ide.engine.opencode_runtime as runtime_module

    launch = ProcessGroup.start

    def delayed_start(self, argv, cwd, env, before_resume=None):
        return launch(
            self,
            argv,
            cwd,
            {**env, "FIXTURE_STARTUP_STALL_PATH": path},
            before_resume,
        )

    monkeypatch.setattr(ProcessGroup, "start", delayed_start)
    monkeypatch.setattr(runtime_module, "HEALTH_TIMEOUT_SECONDS", 0.2)
    run, _ = seed(authenticated, tmp_path, roles=("reviewer",))
    runner = runner_for(authenticated[0], settings)
    assert runner.execute(run["id"]).final_state == "completed"
    launches, trace = launch_fixture
    assert len(launches) == 1
    rows = [json.loads(line) for line in trace.read_text().splitlines()]
    probes = [row for row in rows if row["kind"] == "startup_probe"]
    assert len(probes) >= 3
    assert [row["body"]["stall"] for row in probes[:3]] == [True, True, False]
    assert sum(row["path"] == "/session" for row in rows) == 1
    assert sum(row["path"].endswith("/message") for row in rows) == 1
    assert not runner.registry.by_run(run["id"])


def test_session_survives_more_than_three_continuations(
    authenticated, settings, tmp_path, launch_fixture
):
    run, _ = seed(authenticated, tmp_path, roles=("reviewer",) * 5)
    runner = runner_for(authenticated[0], settings)
    assert runner.execute(run["id"]).final_state == "completed"
    with authenticated[0].app.state.session_factory() as db:
        rows = list(db.scalars(select(AgentSession).order_by(AgentSession.started_at)))
        assert [r.resume_count for r in rows] == [0, 1, 2, 3, 4]
        assert len({r.external_session_id for r in rows}) == 1


@pytest.mark.parametrize(
    "control_kind", ["pause", "stop", "pause_then_stop", "pause_missing", "pause_unavailable"]
)
def test_pause_continues_session_while_stop_restarts_task(
    authenticated, settings, tmp_path, launch_fixture, control_kind, monkeypatch
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
                "command_id": f"{kind}-{current['state_version']}",
                "command_type": kind,
                "expected_state_version": current["state_version"],
            },
        )
        assert response.status_code == 200, response.text

    with client.app.state.session_factory() as db:
        saved_sessions = json.loads(db.get(Run, run["id"]).runtime_json).get("native_sessions", {})
    control("stop" if control_kind == "stop" else "pause")
    thread.join(timeout=20)
    assert not thread.is_alive() and not failures, failures
    assert results[0].final_state == ("stopped" if control_kind == "stop" else "paused"), results
    assert not runner.registry.by_run(run["id"])
    if control_kind == "stop":
        # Old STOP checkpoints retained native sessions without a current-session marker.
        with client.app.state.session_factory() as db:
            row = db.get(Run, run["id"])
            runtime = json.loads(row.runtime_json)
            runtime["native_sessions"] = saved_sessions
            runtime.pop("current_agent_session", None)
            row.runtime_json = json.dumps(runtime)
            db.commit()
    if control_kind == "pause_then_stop":
        control("stop")
    if control_kind == "pause_unavailable":
        with monkeypatch.context() as patched:
            patched.setattr(Runner, "_availability", lambda self, candidate: "outside_schedule")
            control("resume")
            unavailable = runner_for(client, settings).execute(run["id"])
        assert unavailable.final_state == "waiting_input"
        assert unavailable.waiting_reason.code == "session_resume_unavailable"
        assert (
            sum(
                json.loads(line)["path"].endswith("/message")
                for line in trace.read_text().splitlines()
            )
            == 1
        )
    if control_kind == "pause_missing":
        (tmp_path / "sessions.json").write_text("{}")
        control("resume")
        missing = runner_for(client, settings).execute(run["id"])
        assert missing.final_state == "waiting_input"
        assert missing.waiting_reason.code == "session_resume_unavailable"
        assert (
            sum(
                json.loads(line)["path"].endswith("/message")
                for line in trace.read_text().splitlines()
            )
            == 1
        )
        control("stop")
    control("resume")
    next_runner = runner_for(client, settings)
    final = next_runner.execute(run["id"])
    assert final.final_state == "completed", final
    assert len(launch_fixture[0]) == (3 if control_kind == "pause_missing" else 2)
    assert (
        launch_fixture[0][0][1]["OPENCODE_SERVER_PASSWORD"]
        != launch_fixture[0][1][1]["OPENCODE_SERVER_PASSWORD"]
    )
    with client.app.state.session_factory() as db:
        rows = list(db.scalars(select(AgentSession).order_by(AgentSession.started_at)))
        assert len(rows) == (3 if control_kind == "pause_missing" else 2)
        assert (rows[0].external_session_id == rows[-1].external_session_id) == (
            control_kind in {"pause", "pause_unavailable"}
        )
        assert all(p.state == "finished" for p in db.scalars(select(ProcessSupervision)))
    messages = [
        json.loads(line)["body"]
        for line in trace.read_text().splitlines()
        if json.loads(line)["path"].endswith("/message")
    ]
    prompts = [m["parts"][0]["text"] for m in messages]
    assert ("Continue the interrupted task" in prompts[1]) == (
        control_kind in {"pause", "pause_unavailable"}
    )
    if control_kind not in {"pause", "pause_unavailable"}:
        assert prompts[0] == prompts[1]


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


@pytest.mark.parametrize("kind,mode", [("codex", "workspace_write"), ("opencode", "native")])
def test_global_execution_settings_persist_and_reject_invalid_auto_approval(
    authenticated, kind, mode
):
    client, headers = authenticated
    profile = client.post(
        "/api/harness_profiles",
        headers=headers,
        json={"name": f"config-{kind}", "harness_kind": kind},
    ).json()
    url = f"/api/harness_profiles/{profile['id']}"
    response = client.patch(
        url,
        headers=headers,
        json={
            "expected_version": profile["version"],
            "settings": {"permission_mode": mode, "auto_approve": True},
        },
    )
    assert response.status_code == 200, response.text
    updated = response.json()
    assert client.get(url).json()["settings"] == {"permission_mode": mode, "auto_approve": True}
    invalid = client.patch(
        url,
        headers=headers,
        json={
            "expected_version": updated["version"],
            "settings": {"permission_mode": mode, "auto_approve": "yes"},
        },
    )
    assert invalid.status_code == 409
    assert client.get(url).json()["settings"]["auto_approve"] is True
