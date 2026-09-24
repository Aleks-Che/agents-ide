"""Stage 6B review: wire protocol, bounded lifecycle and durable Runner state."""

import json
import os
import sys
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from test_stage6b_codex import FIXTURE, _adapter, _make_request, _read_records

from agents_ide.adapters.base import ExternalOutcome
from agents_ide.adapters.codex import CodexAdapter, CodexStream
from agents_ide.engine import codex_runtime
from agents_ide.engine.queue import claim_next_job
from agents_ide.engine.runner import Runner
from agents_ide.persistence.models import AgentSession, ProcessSupervision, RunEvent
from agents_ide.worker.processes import ProcessGroup, ProcessRegistry


@pytest.fixture
def transport(tmp_path):
    streams = []

    def create(**scenario):
        trace = tmp_path / f"trace-{len(streams)}.jsonl"
        env = {k: v for k, v in os.environ.items() if not k.startswith("FAKE_CODEX_")}
        env.update({f"FAKE_CODEX_{k.upper()}": str(v) for k, v in scenario.items()})
        env["FAKE_CODEX_TRACE"] = str(trace)
        stream = CodexStream.open([sys.executable, str(FIXTURE)], str(tmp_path), env)
        streams.append(stream)
        return _adapter(stream), trace

    yield create
    for stream in streams:
        stream.close()


def test_wire_ids_scoping_final_message_usage_and_feedback(transport):
    adapter, trace = transport(early_events=1, final_only=1, stderr_flood=1)
    events = []
    result = adapter.run(
        replace(
            _make_request(),
            feedback="retry feedback",
            emit_event=lambda t, p: events.append((t, p)),
        )
    )
    assert result.succeeded, result
    assert result.raw_text == "hello from codex"
    assert result.tokens_used == 18  # Not cumulative 99 or cached/reasoning double-counted.
    assert [p["context_tokens"] for t, p in events if t == "budget.updated"] == [18]
    assert result.cost_estimated is None
    assert len(result.tool_calls) == 1
    assert "FOREIGN" not in json.dumps(events)
    assert any(t == "attempt.text_delta" and p["text"] == "early" for t, p in events)
    assert any(t == "attempt.progress" and p.get("message_id") for t, p in events)
    records = [r["message"] for r in _read_records(trace)]
    call = next(r for r in records if r.get("method") == "turn/start")
    assert call["id"] is not None
    assert call["params"]["input"][-1]["text"] == "Feedback:\nretry feedback"
    assert call["params"]["sandboxPolicy"] == {"type": "readOnly"}
    assert len([r for r in records if r.get("method") == "initialize"]) == 1


def test_tool_history_disabled_keeps_final_answer_and_small_handoff_hints(transport, monkeypatch):
    from agents_ide.adapters import native_events
    from agents_ide.adapters.history import OMITTED_HISTORY_EVENTS

    adapter, _ = transport(early_events=1, final_only=1)
    events = []

    def forbidden(_):
        raise AssertionError("Discarded vendor bodies must not be serialized or sanitized")

    monkeypatch.setattr(native_events, "sanitize", forbidden)
    result = adapter.run(
        replace(
            _make_request(),
            record_tool_history=False,
            emit_event=lambda kind, body: events.append((kind, body)),
        )
    )
    assert result.succeeded and result.raw_text == "hello from codex"
    assert result.tool_calls and all(
        set(call) == {"tool", "status", "summary"} for call in result.tool_calls
    )
    assert not any(kind in OMITTED_HISTORY_EVENTS for kind, _ in events)
    assert any(kind == "attempt.text_delta" for kind, _ in events)


def test_codex_terminal_quota_after_tools_allows_handoff(transport):
    adapter, _ = transport(
        response_error_json=json.dumps(
            {
                "message": "Usage limit reached",
                "codexErrorInfo": "usageLimitExceeded",
            }
        )
    )
    result = adapter.run(_make_request())
    assert result.outcome == ExternalOutcome.UNAVAILABLE
    assert result.error.code == "provider_quota_exhausted"
    assert result.can_handoff and not result.no_effect
    assert result.raw_text == "hello from codex"


@pytest.mark.parametrize("kind", ["file", "command", "permissions"])
def test_approval_has_schema_correct_decline_and_no_success(transport, kind):
    adapter, trace = transport(pending_permission=1, permission_kind=kind)
    result = adapter.run(_make_request())
    assert result.outcome == ExternalOutcome.PERMISSION_DENIED
    assert not result.no_effect
    assert any(
        r["message"].get("id") == "permission-1" and "result" in r["message"]
        for r in _read_records(trace)
    )


def test_shared_connection_initializes_once_and_uses_unique_request_ids(transport):
    adapter, trace = transport()
    assert adapter.run(_make_request()).succeeded
    other = CodexAdapter(stream=adapter._stream, session=adapter.session)
    assert other.run(
        replace(_make_request(), resume_session_id=adapter.external_session_id)
    ).succeeded
    rows = [r["message"] for r in _read_records(trace)]
    ids = [r["id"] for r in rows if "method" in r and "id" in r]
    assert len(ids) == len(set(ids))
    assert len([r for r in rows if r.get("method") == "initialize"]) == 1
    resume = next(r["params"] for r in rows if r.get("method") == "thread/resume")
    assert resume["sandbox"] == "read-only" and resume["approvalPolicy"] == "never"


def test_resume_transient_error_does_not_silently_create_thread(transport):
    adapter, trace = transport(resume_error=1)
    result = adapter.run(replace(_make_request(), resume_session_id="existing-thread"))
    assert not result.succeeded
    assert not any(
        r["message"].get("method") in {"thread/start", "turn/start"} for r in _read_records(trace)
    )


def test_request_loaded_before_pause_update_remains_compatible(transport):
    adapter, trace = transport()
    legacy_request = SimpleNamespace(
        **{key: value for key, value in vars(_make_request()).items() if key != "resume_required"}
    )
    assert adapter.run(legacy_request).succeeded
    assert any(r["message"].get("method") == "turn/start" for r in _read_records(trace))


@pytest.mark.parametrize("malformed", ["thread_id_missing", "exception_with_private_text"])
def test_protocol_failure_records_location_without_provider_content(
    transport, monkeypatch, malformed
):
    adapter, trace = transport()
    request_response = adapter._request_response

    def respond(method, *args, **kwargs):
        if method == "thread/start":
            if malformed == "exception_with_private_text":
                raise ValueError("private-provider-content-and-credentials")
            return {"result": {"thread": {"turns": []}}}
        return request_response(method, *args, **kwargs)

    monkeypatch.setattr(adapter, "_request_response", respond)
    result = adapter.run(_make_request())
    assert result.error.code == "server_transport_error"
    assert result.no_effect
    assert "private-provider" not in repr(result)
    assert result.error.details["location"].startswith("_run:")
    if malformed == "thread_id_missing":
        assert result.error.details["phase"] == "validate_session"
        assert result.error.details["exception_type"] == "KeyError"
        assert result.error.details["field"] == "id"
    else:
        assert result.error.details["phase"] == "create_session"
        assert result.error.details["exception_type"] == "ValueError"
    assert not any(r["message"].get("method") == "turn/start" for r in _read_records(trace))


def test_paused_thread_missing_does_not_dispatch_new_task(transport):
    adapter, trace = transport()
    result = adapter.run(
        replace(_make_request(), resume_session_id="missing-thread", resume_required=True)
    )
    assert result.error.code == "session_resume_unavailable"
    assert result.no_effect
    assert not any(
        r["message"].get("method") in {"thread/start", "turn/start"} for r in _read_records(trace)
    )


@pytest.mark.parametrize("trigger", ["deadline", "stop", "native"])
def test_interrupt_has_turn_id_and_long_call_stops(transport, trigger):
    adapter, trace = transport(response_delay_seconds=20)
    ready = threading.Event()
    stop = threading.Event()
    request = replace(
        _make_request(),
        stop_event=stop,
        emit_event=lambda t, p: ready.set() if t == "attempt.progress" else None,
    )
    if trigger == "deadline":
        request = replace(request, deadline_at=time.time() + 1.5)
    results = []
    worker = threading.Thread(target=lambda: results.append(adapter.run(request)))
    worker.start()
    assert ready.wait(5)
    if trigger == "stop":
        stop.set()
    if trigger == "native":
        adapter.interrupt(adapter.external_session_id)
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert results[0].outcome == ExternalOutcome.UNKNOWN
    assert not results[0].no_effect
    assert not results[0].can_handoff
    rows = [r["message"] for r in _read_records(trace)]
    interrupts = [r for r in rows if r.get("method") == "turn/interrupt"]
    assert interrupts and all(r.get("id") and r["params"].get("turnId") for r in interrupts)


def test_post_dispatch_transport_loss_allows_handoff_without_claiming_no_effect(transport):
    adapter, _ = transport(close_stream=1)
    result = adapter.run(_make_request())
    assert result.outcome == ExternalOutcome.UNAVAILABLE and not result.no_effect
    assert result.can_handoff


@pytest.mark.parametrize(
    "stream_output",
    ["invalid json\n", "x" * (1024 * 1024 + 1) + "\n"],
    ids=["invalid-json", "oversize"],
)
def test_malformed_or_oversized_wire_output_is_not_silently_dropped(tmp_path, stream_output):
    script = tmp_path / "bad.py"
    script.write_text(
        "import sys,time\nsys.stdout.write("
        + repr(stream_output)
        + ");sys.stdout.flush();time.sleep(5)"
    )
    stream = CodexStream.open([sys.executable, str(script)], str(tmp_path), dict(os.environ))
    try:
        adapter = _adapter(stream, request_timeout=0.5)
        started = time.monotonic()
        result = adapter.run(_make_request())
        assert not result.succeeded and time.monotonic() - started < 2
    finally:
        stream.close()


@pytest.fixture
def runtime_launch(monkeypatch, tmp_path):
    original = ProcessGroup.popen_stdio
    calls = []
    trace = tmp_path / "runtime.jsonl"
    scenarios = {}

    def launch(self, argv, cwd, env=None, before_resume=None, **kwargs):
        if "app-server" in argv:
            calls.append(argv)
            argv = [sys.executable, str(FIXTURE)]
            env = {
                **(env or {}),
                "FAKE_CODEX_TRACE": str(trace),
                "FAKE_CODEX_SESSIONS": str(tmp_path / "sessions.json"),
                **scenarios,
            }
        return original(self, argv, cwd, env, before_resume, **kwargs)

    monkeypatch.setattr(ProcessGroup, "popen_stdio", launch)
    monkeypatch.setattr(
        codex_runtime, "fetch_codex_version", lambda *a, **k: "codex-cli 0.153.4-fixture"
    )
    return calls, trace, scenarios


def test_runtime_catalog_single_owned_process_and_cleanup(tmp_path, runtime_launch):
    calls, trace, _ = runtime_launch
    runtime = codex_runtime.CodexRuntime.start(executable=sys.executable, workspace_path=tmp_path)
    try:
        assert len(runtime.cached_models) == 3
        assert len(calls) == 1
        assert runtime.stream.group is not None
        assert runtime.stream.is_alive()
        rows = [r["message"] for r in _read_records(trace)]
        assert len([r for r in rows if r.get("method") == "initialize"]) == 1
        assert len([r for r in rows if r.get("method") == "model/list"]) == 2
    finally:
        runtime.close()
    assert runtime.stream.process.poll() is not None
    assert all(not t.is_alive() for t in runtime.stream._readers)


def test_runtime_startup_has_no_elapsed_time_limit(tmp_path, runtime_launch, monkeypatch):
    from itertools import count
    from types import SimpleNamespace

    clock = count(0, 60)
    monkeypatch.setattr(codex_runtime, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    runtime = codex_runtime.CodexRuntime.start(executable=sys.executable, workspace_path=tmp_path)
    try:
        assert runtime.cached_models
    finally:
        runtime.close()


@pytest.mark.parametrize("stage", ["INIT", "CATALOG"])
def test_runtime_failed_handshake_is_failed_probe(tmp_path, runtime_launch, stage):
    _, trace, scenarios = runtime_launch
    scenarios[f"FAKE_CODEX_{stage}_ERROR"] = "1"
    with pytest.raises(OSError):
        codex_runtime.probe_executable(sys.executable, tmp_path, {})
    assert not any(r["message"].get("method") == "turn/start" for r in _read_records(trace))


def seed(authenticated, tmp_path, *, settings=None, params=None, legacy=False):
    client, headers = authenticated

    def post(path, data):
        response = client.post("/api" + path, headers=headers, json=data)
        assert response.status_code in {200, 201}, response.text
        return response.json()

    workspace = tmp_path / "project"
    workspace.mkdir()
    project = post("/projects", {"name": "codex", "workspace_path": str(workspace)})
    profile = post(
        "/harness_profiles",
        {
            "name": "codex",
            "harness_kind": "codex",
            "executable_path": sys.executable,
            "settings": settings if settings is not None else {"permission_mode": "read_only"},
        },
    )
    template = post("/templates", {"name": "codex"})
    nodes = (
        [{"id": "start", "type": "Start"}]
        + [
            {
                "id": f"a{i}",
                "type": "AgentTask",
                "config": {
                    "role": role,
                    "prompt": "force_decision=passed",
                    "response_format": "json",
                    "output_schema": {
                        "type": "object",
                        "properties": {"verdict": {"type": "string"}},
                        "required": ["verdict"],
                    },
                    "params": params or {},
                    "model_selection": {
                        "kind": "direct",
                        "harness_profile_id": profile["id"],
                        "model_id": "gpt-5.6-sol",
                    },
                },
            }
            for i, role in enumerate(("implementer", "reviewer", "implementer"))
        ]
        + [{"id": "end", "type": "End"}]
    )
    if legacy:
        for node in nodes:
            if node["type"] == "AgentTask":
                node["config"].pop("model_selection")
                node["config"].update(model="gpt-5.6-sol", harness_profile_id=profile["id"])
    version = post(
        f"/templates/{template['id']}/versions",
        {
            "graph": {
                "nodes": nodes,
                "edges": [
                    {"id": f"e{i}", "from": a["id"], "to": b["id"]}
                    for i, (a, b) in enumerate(zip(nodes, nodes[1:], strict=False))
                ],
            }
        },
    )
    binding = post(
        f"/versions/{version['id']}/bindings", {"project_id": project["id"], "name": "codex"}
    )
    return {
        "project_id": project["id"],
        "binding_id": binding["id"],
        "idempotency_key": "codex-review",
        "execution_mode": "real",
        "message": "go",
    }


@pytest.mark.parametrize(
    "profile_settings,params",
    [
        ({}, None),
        ({"permission_mode": "read_only", "approval_policy": "unsupported"}, None),
        ({"permission_mode": "read_only"}, {"temperature": 0.5}),
    ],
)
def test_codex_preflight_rejects_unverified_configuration(
    authenticated, tmp_path, profile_settings, params
):
    payload = seed(authenticated, tmp_path, settings=profile_settings, params=params)
    client, headers = authenticated
    response = client.post("/api/runs", headers=headers, json=payload)
    assert response.status_code == 422
    expected = "harness_catalog_unverified" if params else "configuration_invalid"
    assert expected in response.text


@pytest.mark.parametrize(
    "catalog,fingerprint,expected",
    [
        (["replacement-model"], "current", "harness_model_unavailable"),
        ([], "current", "harness_model_unavailable"),
        (["replacement-model"], "old", "harness_catalog_unverified"),
        (["gpt-5.6-sol"], "current", "harness_catalog_unverified"),
    ],
)
def test_preflight_distinguishes_missing_model_from_unverified_catalog(
    authenticated, tmp_path, monkeypatch, catalog, fingerprint, expected
):
    from agents_ide.persistence.models import HarnessProfile

    monkeypatch.setattr("agents_ide.services.harness.fingerprint", lambda *args: "current")
    payload = seed(authenticated, tmp_path, params={"reasoning_effort": "max"})
    client, headers = authenticated
    with client.app.state.session_factory.begin() as session:
        profile = session.scalar(select(HarnessProfile))
        profile.catalog_fingerprint = fingerprint
        profile.catalog_fetched_at = 1  # TTL alone does not invalidate confirmed options.
        profile.catalog_models_json = json.dumps(catalog)
        profile.catalog_metadata_json = "{}"
    response = client.post("/api/runs", headers=headers, json=payload)
    assert response.status_code == 422, response.text
    errors = response.json()["details"]["errors"]
    assert {issue["code"] for issue in errors} == {expected}
    assert all(issue["details"]["model_id"] == "gpt-5.6-sol" for issue in errors)
    if expected == "harness_model_unavailable":
        assert all("отсутствует в проверенном каталоге" in issue["message"] for issue in errors)


@pytest.mark.parametrize("legacy", [False, True])
def test_runner_durable_thread_turn_role_isolation_and_process_cleanup(
    authenticated, settings, tmp_path, runtime_launch, legacy
):
    payload = seed(authenticated, tmp_path, legacy=legacy)
    client, headers = authenticated
    response = client.post("/api/runs", headers=headers, json=payload)
    assert response.status_code == 201, response.text
    factory = client.app.state.session_factory
    job = claim_next_job(factory, worker_id="review6b", lease_seconds=300)
    assert job
    runner = Runner(
        session_factory=factory,
        worker_id="review6b",
        generation=job.generation,
        data_dir=settings.data_dir,
        registry=ProcessRegistry(),
        secret_store=None,
    )
    result = runner.execute(response.json()["id"])
    assert result.final_state == "completed", result
    observation = client.get(f"/api/runs/{response.json()['id']}/snapshot").json()["observation"]
    assert all(n["context_tokens"] == 18 for n in observation["nodes"] if n["type"] == "AgentTask")
    calls, trace, _ = runtime_launch
    assert len(calls) == 1
    assert not runner.registry.by_run(response.json()["id"])
    with factory() as db:
        sessions = list(db.scalars(select(AgentSession).order_by(AgentSession.started_at)))
        assert len(sessions) == 3
        assert sessions[0].external_session_id == sessions[2].external_session_id
        assert sessions[0].external_session_id != sessions[1].external_session_id
        assert all(
            s.external_turn_id and s.finished_at and s.harness_kind == "codex" for s in sessions
        )
        assert all(
            json.loads(s.capabilities_json)["permission_mode"] == "read_only" for s in sessions
        )
        assert all(
            json.loads(s.capabilities_json)["server_version"] == "codex-cli 0.153.4-fixture"
            for s in sessions
        )
        assert all(p.state == "finished" for p in db.scalars(select(ProcessSupervision)))
        assert any(e.type == "agent.session_resumed" for e in db.scalars(select(RunEvent)))
    turns = [
        r["message"] for r in _read_records(trace) if r["message"].get("method") == "turn/start"
    ]
    assert len(turns) == 3
    assert all(turn["params"]["outputSchema"]["required"] == ["verdict"] for turn in turns)


def test_large_response_does_not_abort_active_turn(transport):
    adapter, trace = transport(large_response=1, final_only=1)
    result = adapter.run(_make_request())
    assert result.succeeded, result
    assert len(result.raw_text) == 11 * 1024 * 1024
    assert not any(r["message"].get("method") == "turn/interrupt" for r in _read_records(trace))


@pytest.mark.parametrize("failed", [False, True])
def test_probe_api_uses_temporary_workspace_and_caches_only_success(
    authenticated, runtime_launch, failed
):
    client, headers = authenticated
    calls, trace, scenarios = runtime_launch
    if failed:
        scenarios["FAKE_CODEX_CATALOG_ERROR"] = "1"
    profile = client.post(
        "/api/harness_profiles",
        headers=headers,
        json={"name": "probe", "harness_kind": "codex", "executable_path": sys.executable},
    ).json()
    response = client.post(f"/api/harness_profiles/{profile['id']}/test", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == ("failed" if failed else "ok")
    current = client.get(f"/api/harness_profiles/{profile['id']}", headers=headers).json()
    assert len(current["catalog_models"]) == (0 if failed else 3)
    assert len(calls) == 1
    assert not any(r["message"].get("method") == "turn/start" for r in _read_records(trace))


@pytest.mark.parametrize("control_kind", ["stop", "pause"])
def test_runner_pause_continues_session_and_stop_starts_fresh(
    authenticated, settings, tmp_path, runtime_launch, control_kind
):
    payload = seed(authenticated, tmp_path)
    client, headers = authenticated
    response = client.post("/api/runs", headers=headers, json=payload)
    assert response.status_code == 201, response.text
    run_id = response.json()["id"]
    factory = client.app.state.session_factory
    calls, trace, scenarios = runtime_launch
    scenarios["FAKE_CODEX_RESPONSE_DELAY_SECONDS"] = "20"

    def new_runner():
        job = claim_next_job(factory, worker_id="controls6b", lease_seconds=300)
        assert job
        return Runner(
            session_factory=factory,
            worker_id="controls6b",
            generation=job.generation,
            data_dir=settings.data_dir,
            registry=ProcessRegistry(),
            secret_store=None,
        )

    runner = new_runner()
    results, failures = [], []

    def execute():
        try:
            results.append(runner.execute(run_id))
        except Exception as exc:
            failures.append(exc)

    worker = threading.Thread(target=execute)
    worker.start()
    until = time.monotonic() + 10
    while time.monotonic() < until:
        if any(r["message"].get("method") == "turn/start" for r in _read_records(trace)):
            break
        time.sleep(0.02)
    else:
        pytest.fail("No turn dispatched")

    def control(kind):
        current = client.get(f"/api/runs/{run_id}", headers=headers).json()
        result = client.post(
            f"/api/runs/{run_id}/commands",
            headers=headers,
            json={
                "command_id": f"{kind}-{current['state_version']}",
                "command_type": kind,
                "expected_state_version": current["state_version"],
            },
        )
        assert result.status_code == 200, result.text

    control(control_kind)
    worker.join(timeout=15)
    assert not worker.is_alive() and not failures, failures
    assert results[0].final_state == ("stopped" if control_kind == "stop" else "paused")
    assert not runner.registry.by_run(run_id)
    pauses = 4 if control_kind == "pause" else 1
    for index in range(1, pauses):
        control("resume")
        runner = new_runner()
        worker = threading.Thread(target=execute)
        worker.start()
        until = time.monotonic() + 10
        while time.monotonic() < until:
            turns = [r for r in _read_records(trace) if r["message"].get("method") == "turn/start"]
            if len(turns) > index:
                break
            time.sleep(0.02)
        else:
            pytest.fail("Continuation was not dispatched")
        control("pause")
        worker.join(timeout=15)
        assert not worker.is_alive() and not failures, failures
        assert results[-1].final_state == "paused"
        assert not runner.registry.by_run(run_id)
    scenarios.clear()
    control("resume")
    resumed = new_runner().execute(run_id)
    assert resumed.final_state == "completed", resumed
    assert len(calls) == pauses + 1
    with factory() as db:
        sessions = list(db.scalars(select(AgentSession).order_by(AgentSession.started_at)))
        assert len(sessions) == pauses + 3
        if control_kind == "pause":
            assert len({s.external_session_id for s in sessions[: pauses + 1]}) == 1
        assert (sessions[0].external_session_id == sessions[1].external_session_id) == (
            control_kind == "pause"
        )
        assert all(p.state == "finished" for p in db.scalars(select(ProcessSupervision)))
    turns = [
        r["message"]["params"]
        for r in _read_records(trace)
        if r["message"].get("method") == "turn/start"
    ]
    prompts = [t["input"][0]["text"] for t in turns]
    assert ("Continue the interrupted task" in prompts[1]) == (control_kind == "pause")
    if control_kind == "stop":
        assert prompts[0] == prompts[1]
