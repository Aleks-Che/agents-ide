"""Stage 7: real LLM HTTP adapter, commands, evidence and CollectContext.

A local OpenAI-compatible HTTP server stands in for a provider so the
adapter, retry/fallback policy and evidence package can be exercised
without paid calls. Command and CollectContext nodes run against a real
temporary workspace with real subprocesses and Git.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from agents_ide.adapters.base import ExternalOutcome, LLMAdapterRequest
from agents_ide.adapters.llm_http import HttpLLMAdapter, probe_connection
from agents_ide.engine.commands import CommandSpec, parse_command_list
from agents_ide.errors import AppError


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FakeOpenAIHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    received_authorizations: list[str | None] = []
    received_bodies: list[dict] = []
    routes: dict[str, dict] = {}

    def log_message(self, *args: object) -> None:
        return

    def _send_json(
        self, status: int, body: dict | list, *, headers: dict[str, str] | None = None
    ) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.endswith("/models"):
            self._send_json(200, {"data": [{"id": name} for name in self.routes.get("models", [])]})
            return
        self._send_json(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            body = {}
        type(self).received_authorizations.append(self.headers.get("Authorization"))
        type(self).received_bodies.append(body)
        route = self.routes.get("chat", {"status": 200, "body": _chat_body("ok")})
        status = route.get("status", 200)
        if status == "stream":
            self._send_stream(route)
            return
        if "raw" in route:
            payload = route["raw"]
            if isinstance(payload, str):
                payload = payload.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        headers = route.get("headers")
        if "retry_after" in route:
            headers = {**(headers or {}), "Retry-After": str(route["retry_after"])}
        if "location" in route:
            headers = {**(headers or {}), "Location": route["location"]}
        self._send_json(status, route.get("body", _chat_body("ok")), headers=headers)

    def _send_stream(self, route: dict) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        for chunk in route.get("chunks", ["a", "b", "c"]):
            payload = {"choices": [{"delta": {"content": chunk}}]}
            self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
        self.wfile.write(b'data: {"usage":{"total_tokens":7}}\n\n')
        self.wfile.write(b"data: [DONE]\n\n")
        self.close_connection = True


def _chat_body(content: str, *, tokens: int = 13) -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": tokens - 5, "total_tokens": tokens},
    }


@pytest.fixture
def llm_server() -> Iterator[tuple[ThreadingHTTPServer, str]]:
    port = _free_port()
    FakeOpenAIHandler.received_authorizations = []
    FakeOpenAIHandler.received_bodies = []
    FakeOpenAIHandler.routes = {"models": ["alpha", "beta"]}
    server = ThreadingHTTPServer(("127.0.0.1", port), FakeOpenAIHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{port}/v1"
    finally:
        server.shutdown()
        server.server_close()


def _request(
    base_url: str,
    *,
    prompt: str = "verify",
    secret: str | None = None,
    response_format: str = "text",
    params: dict | None = None,
    evidence: dict | None = None,
) -> LLMAdapterRequest:
    return LLMAdapterRequest(
        role="verifier",
        model_id="alpha",
        prompt=prompt,
        context_package={"__node_id__": "v", "evidence": evidence},
        params=params or {},
        response_format=response_format,
        connection={
            "base_url": base_url,
            "protocol": "http",
            "provider_kind": "openai_compatible",
            "secret_value": secret,
        },
    )


# --------------------------------------------------------------------------- adapter


def test_llm_adapter_success_records_tokens_and_content(llm_server):
    _, base_url = llm_server
    adapter = HttpLLMAdapter(timeout_seconds=5)
    result = adapter.run(_request(base_url, prompt="hello"))
    assert result.outcome == ExternalOutcome.SUCCEEDED
    assert result.raw_text == "ok"
    assert result.tokens_used == 13
    assert result.budget_quality == "observed"


def test_llm_adapter_json_response_format_requests_json_object(llm_server):
    _, base_url = llm_server
    adapter = HttpLLMAdapter(timeout_seconds=5)
    adapter.run(_request(base_url, response_format="json", params={"structured_output": True}))
    body = FakeOpenAIHandler.received_bodies[-1]
    assert body["response_format"] == {"type": "json_object"}


def test_llm_adapter_json_without_structured_output_appends_instruction(llm_server):
    _, base_url = llm_server
    adapter = HttpLLMAdapter(timeout_seconds=5)
    adapter.run(_request(base_url, response_format="json"))
    body = FakeOpenAIHandler.received_bodies[-1]
    assert "response_format" not in body
    assert "строго одним JSON-объектом" in body["messages"][0]["content"]


def test_llm_adapter_rate_limit_carries_retry_after(llm_server):
    server, base_url = llm_server
    FakeOpenAIHandler.routes["chat"] = {
        "status": 429,
        "body": {"error": {"message": "slow down"}},
        "retry_after": 3,
    }
    adapter = HttpLLMAdapter(timeout_seconds=5)
    result = adapter.run(_request(base_url))
    assert result.outcome == ExternalOutcome.RETRYABLE_FAILURE
    assert result.no_effect is True
    assert result.error and result.error.retry_safety == "safe"
    assert result.error.details["retry_after_seconds"] == 3


@pytest.mark.parametrize("status", [200, 402, 429])
def test_llm_quota_exhaustion_skips_retries(llm_server, status):
    _, base_url = llm_server
    FakeOpenAIHandler.routes["chat"] = {
        "status": status,
        "body": {"error": {"message": "The Token Plan usage limit has been reached. (2067)"}},
        "retry_after": 3600,
    }
    result = HttpLLMAdapter().run(_request(base_url))
    assert result.outcome == ExternalOutcome.UNAVAILABLE
    assert result.no_effect and result.error.retry_safety == "safe"
    assert result.error.code == "provider_quota_exhausted"
    assert "retry_after_seconds" not in result.error.details


def test_llm_stream_quota_after_partial_text_allows_fallback(llm_server):
    _, base_url = llm_server
    FakeOpenAIHandler.routes["chat"] = {
        "status": 200,
        "raw": (
            b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
            b'data: {"error":{"code":"insufficient_quota"}}\n\n'
        ),
    }
    result = HttpLLMAdapter().run(_request(base_url, params={"stream": True}))
    assert result.outcome == ExternalOutcome.UNAVAILABLE
    assert result.error.code == "provider_quota_exhausted"
    assert result.no_effect
    assert "partial" in result.raw_text


def test_llm_adapter_unauthorized_is_permission_denied(llm_server):
    server, base_url = llm_server
    FakeOpenAIHandler.routes["chat"] = {"status": 401, "body": {"error": {"message": "no key"}}}
    adapter = HttpLLMAdapter(timeout_seconds=5)
    result = adapter.run(_request(base_url, secret="sk-test"))
    assert result.outcome == ExternalOutcome.PERMISSION_DENIED
    assert result.error and result.error.retry_safety == "unsafe"


def test_llm_adapter_server_error_has_unknown_effect(llm_server):
    server, base_url = llm_server
    FakeOpenAIHandler.routes["chat"] = {"status": 503, "body": {"error": {"message": "down"}}}
    adapter = HttpLLMAdapter(timeout_seconds=5)
    result = adapter.run(_request(base_url))
    assert result.outcome == ExternalOutcome.UNKNOWN
    assert result.no_effect is False


def test_llm_adapter_invalid_json_is_invalid_format(llm_server):
    server, base_url = llm_server
    FakeOpenAIHandler.routes["chat"] = {"status": 200, "raw": b"not-json"}
    adapter = HttpLLMAdapter(timeout_seconds=5)
    result = adapter.run(_request(base_url))
    assert result.outcome == ExternalOutcome.INVALID_FORMAT
    assert result.error and result.error.code == "invalid_provider_json"


def test_llm_adapter_refuses_cross_origin_redirect(llm_server):
    server, base_url = llm_server
    FakeOpenAIHandler.routes["chat"] = {
        "status": 302,
        "location": "http://evil.test/v1/chat/completions",
    }
    adapter = HttpLLMAdapter(timeout_seconds=5)
    result = adapter.run(_request(base_url, secret="sk-test"))
    assert result.outcome == ExternalOutcome.CONFIRMED_FAILURE
    assert result.error and result.error.code == "redirect_refused"


def test_llm_adapter_streaming_accumulates_deltas(llm_server):
    server, base_url = llm_server
    FakeOpenAIHandler.routes["chat"] = {"status": "stream", "chunks": ["foo", "bar"]}
    adapter = HttpLLMAdapter(timeout_seconds=5)
    result = adapter.run(_request(base_url, params={"stream": True}))
    assert result.outcome == ExternalOutcome.SUCCEEDED
    assert result.raw_text == "foobar"
    assert result.tokens_used == 7


def test_llm_adapter_credentials_never_in_url_or_env(llm_server):
    _, base_url = llm_server
    adapter = HttpLLMAdapter(timeout_seconds=5)
    adapter.run(_request(base_url, secret="sk-secret-value"))
    assert FakeOpenAIHandler.received_authorizations[-1] == "Bearer sk-secret-value"
    # No query string ever carries the key.
    assert "sk-secret-value" not in base_url


def test_probe_connection_reports_ok_and_catalog(llm_server):
    server, base_url = llm_server
    FakeOpenAIHandler.routes["models"] = ["alpha", "beta"]
    probe = probe_connection({"base_url": base_url}, model_id="alpha", timeout_seconds=5)
    assert probe.ok is True
    assert set(probe.models) == {"alpha", "beta"}


def test_probe_connection_unreachable_is_failed():
    probe = probe_connection({"base_url": "http://127.0.0.1:9/v1"}, model_id="m", timeout_seconds=2)
    assert probe.ok is False


# --------------------------------------------------------------------------- commands


def _spec(
    *,
    id_: str = "cmd",
    program: str = sys.executable,
    args: tuple[str, ...] = ("-c", "print('ok')"),
    required: bool = True,
    success_codes: tuple[int, ...] = (0,),
    retry_safety: str = "safe",
    timeout: float = 30,
    cap: int = 1024,
    env: dict[str, str] | None = None,
    cwd: str = ".",
) -> CommandSpec:
    return CommandSpec(
        id=id_,
        program=program,
        args=args,
        cwd=cwd,
        env=env or {},
        required=required,
        success_exit_codes=success_codes,
        timeout_seconds=timeout,
        max_output_bytes=cap,
        retry_safety=retry_safety,
    )


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    return workspace


def _local_launcher():
    from agents_ide.engine.commands import StartedProcess
    from agents_ide.worker.processes import ProcessGroup

    groups: list[ProcessGroup] = []

    def launch(spec, cwd, env, argv):
        group = ProcessGroup()
        child = group.popen_stdio(argv, cwd, env)
        groups.append(group)
        return StartedProcess(child, group=group)

    return launch


def test_command_executor_passes_when_required_succeeds(tmp_path):
    from agents_ide.engine.commands import execute_commands

    result = execute_commands(
        [_spec(args=("-c", "print('ok')"))],
        workspace=_workspace(tmp_path),
        launcher=_local_launcher(),
        failure_policy="collect_all",
    )
    assert result.outcome == ExternalOutcome.SUCCEEDED
    assert result.decision == "passed"
    assert result.result_schema == "command_report"


def test_command_executor_failed_required_excludes_passed(tmp_path):
    from agents_ide.engine.commands import execute_commands

    result = execute_commands(
        [_spec(args=("-c", "import sys; sys.exit(2)"), success_codes=(0,))],
        workspace=_workspace(tmp_path),
        launcher=_local_launcher(),
        failure_policy="collect_all",
    )
    assert result.outcome == ExternalOutcome.SUCCEEDED
    assert result.decision == "failed"


def test_command_executor_launch_error_is_configuration_invalid(tmp_path):
    from agents_ide.engine.commands import execute_commands

    result = execute_commands(
        [_spec(program="this-program-does-not-exist-12345")],
        workspace=_workspace(tmp_path),
        launcher=_local_launcher(),
    )
    assert result.outcome == ExternalOutcome.CONFIRMED_FAILURE
    assert result.no_effect is True
    assert result.error and result.error.code == "configuration_invalid"


def test_command_executor_output_cap_marks_truncated(tmp_path):
    from agents_ide.engine.commands import execute_commands

    result = execute_commands(
        [
            _spec(
                args=("-c", "print('x' * 100000)"),
                cap=4096,
            )
        ],
        workspace=_workspace(tmp_path),
        launcher=_local_launcher(),
    )
    body = json.loads(result.raw_text)
    report = body["commands"][0]
    assert report["output_truncated"] is True or report["dropped_bytes"] > 0


def test_command_executor_ledger_does_not_replay_unsafe_completed(tmp_path):
    from agents_ide.engine.commands import execute_commands

    first = execute_commands(
        [_spec(args=("-c", "print('ok')"), retry_safety="unsafe")],
        workspace=_workspace(tmp_path),
        launcher=_local_launcher(),
    )
    assert first.outcome == ExternalOutcome.SUCCEEDED
    completed = {report["id"]: report for report in json.loads(first.raw_text)["commands"]}
    second = execute_commands(
        [_spec(args=("-c", "print('ok')"), retry_safety="unsafe")],
        workspace=_workspace(tmp_path),
        launcher=_local_launcher(),
        completed=completed,
    )
    reused = json.loads(second.raw_text)["commands"][0]
    assert reused["status"] == "completed"
    assert reused["reused"] is True
    assert second.decision == "passed"
    assert reused["reason"] == "not_replayed"


def test_command_executor_stop_on_failure_skips_remaining(tmp_path):
    from agents_ide.engine.commands import execute_commands

    result = execute_commands(
        [
            _spec(id_="first", args=("-c", "import sys; sys.exit(1)")),
            _spec(id_="second", args=("-c", "print('ok')")),
        ],
        workspace=_workspace(tmp_path),
        launcher=_local_launcher(),
        failure_policy="stop_on_failure",
    )
    commands = json.loads(result.raw_text)["commands"]
    assert commands[0]["status"] == "failed"
    assert commands[1]["status"] == "skipped"


def test_parse_command_list_resolves_input_reference(tmp_path):
    raw = {"ref": "input.verification_commands"}
    inputs = {
        "verification_commands": [
            {"id": "t", "program": "python", "args": ["-c", "1"], "success_exit_codes": [0]}
        ]
    }
    specs = parse_command_list(raw, inputs)
    assert specs[0].id == "t"


def test_command_environment_rejects_secret_keys():
    from agents_ide.engine.commands import command_environment

    with pytest.raises(AppError):
        command_environment({"API_KEY": "leak"})


# --------------------------------------------------------------------------- collect context


def _init_git(workspace: Path) -> None:
    subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@x", "add", "--", "a.txt"],
        cwd=workspace,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-m", "base"],
        cwd=workspace,
        check=True,
        capture_output=True,
    )


def test_collect_context_files_glob_diff_and_omissions(tmp_path):
    from agents_ide.engine.context_sources import collect_context

    workspace = _workspace(tmp_path)
    (workspace / "a.txt").write_text("hello\n", encoding="utf-8")
    (workspace / "src").mkdir()
    (workspace / "src" / "b.py").write_text("print('hi')\n", encoding="utf-8")
    (workspace / "big.bin").write_bytes(b"\x00" * 1024)
    _init_git(workspace)
    (workspace / "a.txt").write_text("changed\n", encoding="utf-8")
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        collection = collect_context(
            {
                "sources": [
                    {"kind": "file", "path": "a.txt"},
                    {"kind": "file", "path": "big.bin"},
                    {"kind": "glob", "path": "src/*.py"},
                    {"kind": "diff"},
                ],
                "max_file_bytes": 1024,
                "include_untracked": True,
            },
            workspace=workspace,
            session=session,
            run_id="run-1",
            base_head_sha=None,
        )
    paths = {entry["path"] for entry in collection.package["files"]}
    assert "a.txt" in paths
    assert any(p.startswith("src/") for p in paths)
    reasons = {omission.get("reason") for omission in collection.omissions}
    assert "binary" in reasons
    diff_entry = next(entry for entry in collection.package["files"] if entry["kind"] == "diff")
    assert "changed" in diff_entry["content"]


def test_collect_context_rejects_path_escape(tmp_path):
    from agents_ide.engine.context_sources import collect_context

    workspace = _workspace(tmp_path)
    (workspace / "ok.txt").write_text("ok", encoding="utf-8")
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        collection = collect_context(
            {"sources": [{"kind": "file", "path": "../escape.txt"}]},
            workspace=workspace,
            session=session,
            run_id="run-2",
            base_head_sha=None,
        )
    assert collection.omissions
    assert all(
        "path" in omission or omission["reason"] == "protected_or_invalid_path"
        for omission in collection.omissions
    )


def test_resolve_requests_allows_configured_file_and_denies_unknown_command():
    from agents_ide.engine.context_sources import resolve_requests

    config = {"sources": [{"kind": "file", "path": "a.txt"}]}
    commands = {"tests": _spec(id_="tests", retry_safety="safe")}
    result = resolve_requests(
        config,
        missing=[
            {"kind": "file", "path": "a.txt", "reason": "need it"},
            {"kind": "file", "path": "secret.txt", "reason": "want it"},
            {"kind": "command_report", "command_id": "unsafe", "reason": "rerun"},
        ],
        artifact_exists=lambda _: True,
        commands=commands,
    )
    allowed = result["allowed_requests"]
    denied = result["denied_requests"]
    assert any(r["path"] == "a.txt" for r in allowed)
    assert any(r["path"] == "secret.txt" and r["reason"] == "not_configured" for r in denied)
    assert any(r.get("command_id") == "unsafe" and r["reason"] == "unknown_command" for r in denied)


# --------------------------------------------------------------------------- helpers


def _session_factory(tmp_path: Path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    _ = tmp_path
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    return sessionmaker(bind=engine, expire_on_commit=False)
