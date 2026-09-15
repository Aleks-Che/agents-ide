"""Unit tests for the OpenCode HTTP/SSE adapter.

The tests use a ThreadingHTTPServer to emulate an OpenCode server with
controllable responses. They exercise the adapter contract and the
translations into the shared ExternalOutcome taxonomy, not a real
OpenCode process.
"""

from __future__ import annotations

import base64
import json
import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from agents_ide.adapters.base import AgentAdapterRequest, ExternalOutcome
from agents_ide.adapters.opencode import (
    OpenCodeAdapter,
    RunSession,
    _validate_loopback_url,
    capabilities_for_kind,
    decode_attachment,
    fetch_opencode_version,
    iter_sse_events,
    list_opencode_models,
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class FakeOpenCodeHandler(BaseHTTPRequestHandler):
    """Minimal OpenCode-compatible HTTP server for the adapter tests."""

    server_version = "FakeOpenCode/1.0"
    sessions: dict[str, dict[str, object]] = {}
    catalog: list[dict[str, object]] = []
    password: str | None = None

    def log_message(self, *args: object) -> None:  # noqa: D401
        return

    def _send_json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth_ok(self) -> bool:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:].encode("ascii")).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return False
        if ":" not in decoded:
            return False
        user, password = decoded.split(":", 1)
        if user != "opencode":
            return False
        return not (self.password is not None and password != self.password)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        self.directory = parse_qs(parsed.query).get(
            "directory", [self.headers.get("x-opencode-directory")]
        )[0]
        self.path = parsed.path
        if self.path == "/event":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            try:
                self.wfile.write(b'data: {"type":"server.connected"}\n\n')
                self.wfile.flush()
                for _ in range(50):
                    import time

                    time.sleep(0.1)
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            return
        if self.path.startswith("/session/"):
            data = self.sessions.get(self.path.rsplit("/", 1)[-1])
            self._send_json(200 if data else 404, data or {"error": "not_found"})
            return
        path = self.path.rstrip("/") or "/"
        if path == "/global/health":
            if not self._auth_ok():
                self._send_json(401, {"error": "unauthorized"})
                return
            self._send_json(200, {"healthy": True, "version": "1.0.0"})
            return
        if path == "/config/providers":
            if not self._auth_ok():
                self._send_json(401, {"error": "unauthorized"})
                return
            self._send_json(
                200,
                {
                    "providers": self.catalog,
                    "default": {"anthropic": "claude-sonnet-4-20250514"},
                },
            )
            return
        self._send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        self.directory = parse_qs(parsed.query).get(
            "directory", [self.headers.get("x-opencode-directory")]
        )[0]
        self.path = parsed.path
        if not self._auth_ok():
            self._send_json(401, {"error": "unauthorized"})
            return
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            self._send_json(400, {"error": "invalid_json"})
            return
        if self.path == "/session":
            session_id = f"ses-{len(self.sessions) + 1:04d}"
            self.sessions[session_id] = {
                "id": session_id,
                "title": body.get("title", ""),
                "permission": body.get("permission"),
                "directory": self.directory,
            }
            self._send_json(
                200,
                {
                    "id": session_id,
                    "title": body.get("title", ""),
                    "permission": body.get("permission"),
                    "directory": self.directory,
                },
            )
            return
        prefix = "/session/"
        if self.path.startswith(prefix):
            tail = self.path[len(prefix) :]
            parts = tail.split("/", 1)
            if len(parts) == 2 and parts[1] == "message":
                session_id = parts[0]
                session = self.sessions.get(session_id)
                if session is None:
                    self._send_json(404, {"error": "session_not_found"})
                    return
                # The fake returns a "hello world" text payload unless a
                # ``__force_outcome`` is smuggled in the prompt.
                prompt = ""
                for part in body.get("parts", []) or []:
                    if isinstance(part, dict) and part.get("type") == "text":
                        content = part.get("text")
                        if isinstance(content, str):
                            prompt += content
                text = "hello from opencode"
                if "force_decision=failed" in prompt:
                    text = json.dumps({"verdict": "failed", "feedback": "no"})
                if "force_decision=inconclusive" in prompt:
                    text = json.dumps({"verdict": "inconclusive", "feedback": "unknown"})
                if "force_decision=passed" in prompt:
                    text = json.dumps({"verdict": "passed", "feedback": "ok"})
                self._send_json(
                    200,
                    {
                        "info": {
                            "id": f"msg-{session_id}-1",
                            "sessionID": session_id,
                            "role": "assistant",
                            "finish": "stop",
                        },
                        "parts": [
                            {
                                "type": "text",
                                "text": text,
                            }
                        ],
                    },
                )
                return
        self._send_json(404, {"error": "not_found"})


@pytest.fixture
def fake_opencode() -> Iterator[tuple[str, str]]:
    port = _free_port()
    FakeOpenCodeHandler.catalog = [
        {
            "id": "anthropic",
            "models": {
                "claude-sonnet-4-20250514": {},
                "claude-haiku-4-20250514": {},
            },
        }
    ]
    FakeOpenCodeHandler.sessions = {}
    FakeOpenCodeHandler.password = "secret"
    server = ThreadingHTTPServer(("127.0.0.1", port), FakeOpenCodeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", "secret"
    finally:
        server.shutdown()
        server.server_close()


def _request(
    base_url: str,
    password: str | None,
    prompt: str,
    model_id: str = "anthropic/claude-sonnet-4-20250514",
) -> AgentAdapterRequest:
    return AgentAdapterRequest(
        role="implementer",
        model_id=model_id,
        prompt=prompt,
        context_package={"__node_id__": "node-1", "evidence": {"hello": "world"}},
        workspace_path="C:/work/agents",
        capabilities={},
        params={},
        emit_event=None,
        stop_event=threading.Event(),
        check_owned=lambda: None,
    )


def _session(base_url: str, password: str | None = "secret") -> RunSession:
    return RunSession(
        base_url=base_url,
        username="opencode",
        password=password,
        workspace_path="C:/work/agents",
        settings={},
        server_version="1.0.0",
    )


def test_validate_loopback_url_accepts_loopback_and_rejects_remote():
    assert _validate_loopback_url("http://127.0.0.1:4096").startswith("http://127.0.0.1")
    from agents_ide.adapters.opencode import OpenCodeConfigurationError

    with pytest.raises(OpenCodeConfigurationError):
        _validate_loopback_url("https://example.com")
    with pytest.raises(OpenCodeConfigurationError):
        _validate_loopback_url("http://192.168.1.10:4096")


def test_capabilities_row():
    caps = capabilities_for_kind()
    assert caps.list_models and caps.resume_session and caps.native_interrupt


def test_fetch_version_and_list_models(fake_opencode):
    base_url, _ = fake_opencode
    version = fetch_opencode_version(base_url, password="secret")
    assert version == "1.0.0"
    models = list_opencode_models(base_url, password="secret")
    assert "anthropic/claude-sonnet-4-20250514" in models
    assert "anthropic/claude-haiku-4-20250514" in models


def test_authentication_required(fake_opencode):
    base_url, _ = fake_opencode
    adapter = OpenCodeAdapter(session=_session(base_url, password="secret"))
    request = _request(base_url, "secret", "force_decision=passed")
    result = adapter.run(request)
    assert result.outcome == ExternalOutcome.SUCCEEDED, result
    assert adapter.external_session_id
    # A new adapter with the wrong password must fail at session creation.
    rejected = OpenCodeAdapter(session=_session(base_url, password="wrong"))
    failure = rejected.run(_request(base_url, "wrong", "force_decision=passed"))
    assert failure.outcome in {
        ExternalOutcome.PERMISSION_DENIED,
        ExternalOutcome.UNAVAILABLE,
        ExternalOutcome.RETRYABLE_FAILURE,
        ExternalOutcome.CONFIRMED_FAILURE,
    }
    assert failure.error and failure.error.code in {
        "server_unauthorized",
        "session_create_failed",
    }


def test_session_lost_returns_safe_retry(fake_opencode):
    base_url, _ = fake_opencode
    adapter = OpenCodeAdapter(session=_session(base_url, password="secret"))
    first = adapter.run(_request(base_url, "secret", "force_decision=passed"))
    assert first.outcome == ExternalOutcome.SUCCEEDED
    # Force the session id to a missing one to simulate a dropped session.
    adapter._binding = type(adapter._binding)(session_id="ghost")
    result = adapter.run(_request(base_url, "secret", "force_decision=passed"))
    assert result.outcome == ExternalOutcome.SUCCEEDED
    assert adapter.external_session_id != "ghost"


def test_run_returns_text_and_decision(fake_opencode):
    base_url, _ = fake_opencode
    adapter = OpenCodeAdapter(session=_session(base_url, password="secret"))
    result = adapter.run(_request(base_url, "secret", "force_decision=passed"))
    assert result.outcome == ExternalOutcome.SUCCEEDED
    assert "verdict" in result.raw_text
    # Decision must come from the parts, not be guessed by the engine.
    assert result.decision is None or result.decision in {"passed", "failed", "unknown"}


def test_run_rejects_missing_model_id(fake_opencode):
    base_url, _ = fake_opencode
    adapter = OpenCodeAdapter(session=_session(base_url, password="secret"))
    request = AgentAdapterRequest(
        role="implementer",
        model_id="",
        prompt="force_decision=passed",
        context_package={"__node_id__": "node-1"},
        workspace_path="C:/work/agents",
        capabilities={},
        params={},
        emit_event=None,
        stop_event=threading.Event(),
        check_owned=lambda: None,
    )
    result = adapter.run(request)
    assert result.outcome == ExternalOutcome.CONFIRMED_FAILURE
    assert result.error and result.error.code == "configuration_invalid"


def test_run_propagates_stop_event(fake_opencode):
    base_url, _ = fake_opencode
    adapter = OpenCodeAdapter(session=_session(base_url, password="secret"))
    request = _request(base_url, "secret", "force_decision=passed")
    request.stop_event.set()
    result = adapter.run(request)
    assert result.outcome == ExternalOutcome.RETRYABLE_FAILURE
    assert result.no_effect


def test_iter_sse_events_decodes_payload():
    payload = b'data: {"type":"server.connected","payload":{}}\n\n'
    payload += b'data: {"type":"message.updated","info":{"id":"m1"}}\n\n'
    events = list(iter_sse_events(payload))
    types = [event["type"] for event in events]
    assert types == ["server.connected", "message.updated"]


def test_decode_attachment_roundtrip():
    raw = b"binary-blob"
    encoded = base64.b64encode(raw).decode("ascii")
    assert decode_attachment(encoded) == raw
    assert decode_attachment("not-base64") is None
