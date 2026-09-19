"""Stage 6B: Codex App Server stdio/JSON-RPC adapter and probe regressions."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from agents_ide.adapters.base import AgentAdapterRequest, ExternalOutcome
from agents_ide.adapters.codex import (
    CodexAdapter,
    CodexStream,
    RunSession,
    capabilities_for_kind,
    validate_thread_id,
)
from agents_ide.engine.codex_runtime import (
    codex_environment,
    fetch_codex_version,
    validate_settings,
)
from agents_ide.errors import AppError

FIXTURE = Path(__file__).parents[1] / "fixtures" / "fake_codex_server.py"
spec = importlib.util.spec_from_file_location("stage6b_codex_fixture", FIXTURE)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def _spawn_process(
    trace_path: Path | None = None,
    *,
    pending_permission: bool = False,
    close_stream: bool = False,
    response_delay_seconds: float = 0.0,
    force_provider_error: bool = False,
    response_error: dict | None = None,
) -> subprocess.Popen[str]:
    env = os.environ.copy()
    env.pop("FAKE_CODEX_TRACE", None)
    env.pop("FAKE_CODEX_PENDING_PERMISSION", None)
    env.pop("FAKE_CODEX_CLOSE_STREAM", None)
    env.pop("FAKE_CODEX_RESPONSE_DELAY_SECONDS", None)
    env.pop("FAKE_CODEX_FORCE_PROVIDER_ERROR", None)
    env.pop("FAKE_CODEX_RESPONSE_ERROR_JSON", None)
    if trace_path is not None:
        env["FAKE_CODEX_TRACE"] = str(trace_path)
    if pending_permission:
        env["FAKE_CODEX_PENDING_PERMISSION"] = "1"
    if close_stream:
        env["FAKE_CODEX_CLOSE_STREAM"] = "1"
    if response_delay_seconds:
        env["FAKE_CODEX_RESPONSE_DELAY_SECONDS"] = str(response_delay_seconds)
    if force_provider_error:
        env["FAKE_CODEX_FORCE_PROVIDER_ERROR"] = "1"
    if response_error is not None:
        env["FAKE_CODEX_RESPONSE_ERROR_JSON"] = json.dumps(response_error)
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(
        [sys.executable, str(FIXTURE)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        creationflags=creationflags,
    )


def _read_records(trace_path: Path) -> list[dict]:
    if not trace_path.exists():
        return []
    text = trace_path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line]


def _wait_until(predicate, *, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


@pytest.fixture
def codex_trace(tmp_path) -> Path:
    return tmp_path / "trace.jsonl"


@pytest.fixture
def codex_stream(codex_trace, tmp_path):
    process = _spawn_process(trace_path=codex_trace)
    try:
        stream = CodexStream.from_process(process)
        yield stream, process, codex_trace
    finally:
        stream.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


def _make_request(
    *,
    prompt: str = "hello",
    context: dict | None = None,
    model: str = "gpt-5.6-sol",
) -> AgentAdapterRequest:
    return AgentAdapterRequest(
        role="implementer",
        model_id=model,
        prompt=prompt,
        context_package=context or {"__node_id__": "node-1", "evidence": {"x": 1}},
        workspace_path=str(Path.cwd()),
        capabilities={},
        params={},
        emit_event=None,
        stop_event=threading.Event(),
        check_owned=lambda: None,
    )


def _adapter(
    stream: CodexStream, *, request_timeout: float = 10.0, turn_timeout: float = 30.0
) -> CodexAdapter:
    session = RunSession(
        workspace_path=str(Path.cwd()),
        settings={"permission_mode": "read_only"},
        server_version="0.153.4-fixture",
    )
    return CodexAdapter(
        stream=stream,
        session=session,
        request_timeout=request_timeout,
        turn_timeout=turn_timeout,
    )


def _initialize(stream: CodexStream) -> CodexAdapter:
    adapter = _adapter(stream)
    adapter.initialize()
    return adapter


def test_capabilities_for_kind_are_read_only():
    caps = capabilities_for_kind()
    assert caps.autonomous_write is False
    assert caps.permissions is False
    assert caps.structured_output is False
    assert caps.list_models is True


@pytest.mark.parametrize("question", [False, True])
def test_live_input_uses_scoped_native_protocol(codex_stream, question):
    stream, _, trace = codex_stream
    adapter = _adapter(stream)
    pending = []
    events = []

    def emit(kind, payload):
        events.append((kind, payload))
        if kind == "agent.input_requested":
            pending.append(
                {
                    "command_id": "reply",
                    "text": "README.md",
                    "question_id": payload["question_id"],
                    "answers": [["README.md"]],
                }
            )
        elif kind == "agent.turn_started" and not question:
            pending.append({"command_id": "steer", "text": "Check README.md"})

    request = replace(
        _make_request(prompt="ask user" if question else "hello"),
        emit_event=emit,
        receive_message=lambda: pending.pop(0) if pending else None,
    )
    assert adapter.run(request).succeeded
    assert any(
        kind == "agent.user_message_status" and payload["delivered"] for kind, payload in events
    )
    records = [item["message"] for item in _read_records(trace)]
    if question:
        assert next(m for m in records if m.get("id") == "question-1")["result"] == {
            "answers": {"choice": {"answers": ["README.md"]}}
        }
    else:
        steer = next(m for m in records if m.get("method") == "turn/steer")
        assert steer["params"]["input"] == [{"type": "text", "text": "Check README.md"}]


def test_thread_id_validation_rejects_garbage():
    with pytest.raises(ValueError):
        validate_thread_id("../not_thread")
    with pytest.raises(ValueError):
        validate_thread_id(None)
    assert validate_thread_id("thread_abc123") == "thread_abc123"


def test_validate_settings_blocks_unverified_policies_and_overrides():
    validate_settings({"permission_mode": "workspace_write", "auto_approve": True})
    with pytest.raises(AppError):
        validate_settings({"permission_mode": "unknown"})
    with pytest.raises(AppError):
        validate_settings({"permission_mode": "read_only", "serve_args": ["--listen", "tcp://"]})
    with pytest.raises(AppError):
        validate_settings({"permission_mode": "read_only", "env": {"OPENAI_API_KEY": "x"}})
    # execution=False lifts the permission_mode check but keeps overrides
    # forbidden.
    with pytest.raises(AppError):
        validate_settings({"permission_mode": "read_only", "serve_args": ["x"]}, execution=False)


def test_codex_environment_keeps_provider_keys_out(monkeypatch):
    for key in (
        "OPENAI_API_KEY",
        "HTTP_PROXY",
        "CODEX_HOME",
    ):
        monkeypatch.setenv(key, "do-not-inherit" if key != "CODEX_HOME" else "C:/codex")
    env = codex_environment()
    assert env.get("CODEX_HOME") == "C:/codex"
    assert "OPENAI_API_KEY" not in env
    assert "HTTP_PROXY" not in env


def test_fetch_codex_version_extracts_codex_cli_prefix():
    version = fetch_codex_version(sys.executable)
    assert version is None


def test_adapter_runs_initialization_and_list_models(codex_stream):
    stream, _, _trace = codex_stream
    adapter = _initialize(stream)
    response = adapter._request_response("model/list", {"cursor": "page2"}, timeout=10.0)
    data = (response.get("result") or {}).get("data")
    assert isinstance(data, list)
    assert {item["id"] for item in data if isinstance(item, dict)} >= {
        "gpt-5.5",
        "gpt-5.3-codex-spark",
    }


def test_adapter_completes_turn_and_emits_session_created(codex_stream):
    stream, _, _trace = codex_stream
    adapter = _adapter(stream)
    events: list[tuple[str, dict]] = []
    request = AgentAdapterRequest(
        role="implementer",
        model_id="gpt-5.6-sol",
        prompt="hello",
        context_package={"x": 1},
        workspace_path=str(Path.cwd()),
        capabilities={},
        params={},
        emit_event=lambda kind, payload: events.append((kind, payload)),
        stop_event=threading.Event(),
        check_owned=lambda: None,
    )
    result = adapter.run(request)
    assert result.outcome == ExternalOutcome.SUCCEEDED, result
    assert "hello from codex" in result.raw_text
    kinds = [kind for kind, _ in events]
    assert "agent.session_created" in kinds
    assert "attempt.text_delta" in kinds
    assert adapter.external_session_id and len(adapter.external_session_id) == 36


def test_adapter_reuses_thread_for_repeated_calls(codex_stream):
    stream, _, _trace = codex_stream
    adapter = _adapter(stream)
    request = _make_request()
    assert adapter.run(request).succeeded
    first = adapter.external_session_id
    assert adapter.run(request).succeeded
    assert adapter.external_session_id == first


def test_adapter_emits_session_resumed_after_explicit_resume(codex_stream):
    stream, _, _trace = codex_stream
    adapter = _adapter(stream)
    request = _make_request()
    assert adapter.run(request).succeeded
    thread_id = adapter.external_session_id
    assert thread_id
    other = _adapter(stream)
    events: list[str] = []
    resumed_request = replace(
        request,
        resume_session_id=thread_id,
        emit_event=lambda kind, _payload: events.append(kind),
    )
    assert other.run(resumed_request).succeeded
    assert other.external_session_id == thread_id
    assert "agent.session_resumed" in events


def _spawn_with_options(
    tmp_path: Path, **options
) -> tuple[CodexStream, subprocess.Popen[str], Path]:
    trace = tmp_path / "trace.jsonl"
    process = _spawn_process(trace_path=trace, **options)
    stream = CodexStream.from_process(process)
    return stream, process, trace


@pytest.fixture
def codex_stream_with_options(tmp_path):
    entries: list[tuple[CodexStream, subprocess.Popen[str], Path]] = []

    def _factory(**options):
        trace = tmp_path / "trace.jsonl"
        process = _spawn_process(trace_path=trace, **options)
        stream = CodexStream.from_process(process)
        entry = (stream, process, trace)
        entries.append(entry)
        return entry

    yield _factory
    for stream, process, _trace in entries:
        stream.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


def test_adapter_rejects_permission_and_logs_decline(codex_stream_with_options):
    stream, _process, trace = codex_stream_with_options(
        pending_permission=True, response_delay_seconds=0.2
    )
    adapter = _adapter(stream)
    events: list[str] = []
    request = AgentAdapterRequest(
        role="implementer",
        model_id="gpt-5.6-sol",
        prompt="trigger_permission",
        context_package={"x": 1},
        workspace_path=str(Path.cwd()),
        capabilities={},
        params={},
        emit_event=lambda kind, _p: events.append(kind),
        stop_event=threading.Event(),
        check_owned=lambda: None,
    )
    result = adapter.run(request)
    assert result.outcome == ExternalOutcome.PERMISSION_DENIED, result
    assert "agent.permission_requested" in events
    assert "agent.permission_resolved" in events
    # The decline reply is observable in the fixture records.
    records = _read_records(trace)
    declines = [
        rec["message"]
        for rec in records
        if isinstance(rec, dict)
        and (rec["message"].get("result") or {}).get("decision") == "decline"
    ]
    assert declines


@pytest.mark.parametrize(
    "error",
    [
        {"message": "private-provider-error", "codexErrorInfo": "other"},
        {"message": "Connection prematurely closed", "codexErrorInfo": "httpConnectionFailed"},
        {"message": "Invalid API key", "codexErrorInfo": "unauthorized"},
        {"message": "Context full", "codexErrorInfo": "contextWindowExceeded"},
        {"message": "Token budget exceeded", "codexErrorInfo": "usageLimitExceeded"},
    ],
)
def test_provider_error_in_turn_status_permits_handoff(codex_stream_with_options, error):
    stream, _, _ = codex_stream_with_options(response_error=error)
    adapter = _adapter(stream)
    request = _make_request()
    result = adapter.run(request)
    assert result.outcome == ExternalOutcome.UNAVAILABLE
    assert result.can_handoff
    assert not result.no_effect
    assert "private-provider-error" not in repr(result)


def test_long_turn_completes_beyond_request_timeout(codex_stream_with_options):
    stream, _, _ = codex_stream_with_options(response_delay_seconds=1.0)
    adapter = _adapter(stream, request_timeout=0.5)
    request = _make_request()
    result = adapter.run(request)
    assert result.outcome == ExternalOutcome.SUCCEEDED


def test_invalid_thread_id_on_resume_falls_back_to_new_thread(codex_stream):
    stream, _, _trace = codex_stream
    adapter = _adapter(stream)
    request = _make_request()
    request = replace(request, resume_session_id="thread_does_not_exist")
    result = adapter.run(request)
    assert result.outcome == ExternalOutcome.SUCCEEDED
    assert adapter.external_session_id and len(adapter.external_session_id) == 36


def test_probe_lists_models_through_real_transport(tmp_path):
    trace = tmp_path / "trace.jsonl"
    process = _spawn_process(trace_path=trace)
    try:
        stream = CodexStream.from_process(process)
        try:
            adapter = _initialize(stream)
            response = adapter._request_response("model/list", {}, timeout=10.0)
            data = (response.get("result") or {}).get("data")
            ids = [item["id"] for item in data if isinstance(item, dict)]
            assert "gpt-5.6-sol" in ids
        finally:
            stream.close()
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


@pytest.mark.parametrize(
    "mode,sandbox,policy_type",
    [
        ("read_only", "read-only", "readOnly"),
        ("workspace_write", "workspace-write", "workspaceWrite"),
        ("full_access", "danger-full-access", "dangerFullAccess"),
    ],
)
@pytest.mark.parametrize("automatic", [True, False])
def test_selected_access_and_approval_reach_thread_and_turn(
    codex_stream, mode, sandbox, policy_type, automatic
):
    stream, _, trace = codex_stream
    adapter = _adapter(stream)
    adapter.session = replace(
        adapter.session, settings={"permission_mode": mode, "auto_approve": automatic}
    )
    assert adapter.run(_make_request()).succeeded
    rows = [r["message"] for r in _read_records(trace)]
    start = next(r["params"] for r in rows if r.get("method") == "thread/start")
    turn = next(r["params"] for r in rows if r.get("method") == "turn/start")
    assert start["sandbox"] == sandbox
    assert turn["sandboxPolicy"]["type"] == policy_type
    assert (
        start["approvalPolicy"]
        == turn["approvalPolicy"]
        == ("never" if automatic else "on-request")
    )
    if mode == "workspace_write":
        assert turn["sandboxPolicy"]["networkAccess"] is False
        assert turn["sandboxPolicy"]["writableRoots"] == [str(Path.cwd())]


def test_codex_manual_approval_is_sent_to_live_chat(codex_stream_with_options):
    stream, _, trace = codex_stream_with_options(pending_permission=True)
    adapter = _adapter(stream)
    adapter.session = replace(
        adapter.session, settings={"permission_mode": "workspace_write", "auto_approve": False}
    )
    pending, events = [], []

    def emit(kind, payload):
        events.append(kind)
        if kind == "agent.input_requested":
            pending.append(
                {
                    "command_id": "approve",
                    "permission_id": payload["permission_id"],
                    "permission_reply": "once",
                    "text": "approve",
                }
            )

    result = adapter.run(
        replace(
            _make_request(),
            emit_event=emit,
            receive_message=lambda: pending.pop(0) if pending else None,
        )
    )
    assert result.succeeded, result
    assert "agent.input_closed" in events
    assert any(r["message"].get("result") == {"decision": "accept"} for r in _read_records(trace))
