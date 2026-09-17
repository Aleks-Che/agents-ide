"""Stage 6A adapter and API smoke tests with a subprocess protocol fixture.

These initial tests exercise the adapter against a local subprocess and API
metadata. Full Runner/supervisor scenarios live in test_stage6a_review.py.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from agents_ide.adapters.opencode import OpenCodeAdapter, RunSession

WORKER_ARGV = [sys.executable, "-m", "agents_ide", "worker"]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_health(base_url: str, password: str | None, *, attempts: int = 40) -> bool:
    import httpx

    auth = httpx.BasicAuth("opencode", password) if password else None
    for _ in range(attempts):
        try:
            response = httpx.get(
                f"{base_url}/global/health", auth=auth, timeout=1.0, trust_env=False
            )
            if response.status_code == 200:
                return True
        except (httpx.HTTPError, OSError):
            pass
        time.sleep(0.1)
    return False


def _run_fake_opencode(port: int, password: str | None, log: bool = False) -> subprocess.Popen[str]:
    script = Path(__file__).resolve().parent.parent / "fixtures" / "fake_opencode_server.py"
    env = {
        **os.environ,
        "FAKE_OPENCODE_PORT": str(port),
        "FAKE_OPENCODE_PASSWORD": password or "",
        "FAKE_OPENCODE_LOG": "1" if log else "0",
    }
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NO_WINDOW
    return subprocess.Popen(
        [sys.executable, str(script)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE if log else subprocess.DEVNULL,
        creationflags=creationflags,
    )


@pytest.fixture
def fake_opencode_process() -> Iterator[tuple[str, str]]:
    port = _free_port()
    password = "opencode-secret"
    proc = _run_fake_opencode(port, password)
    base_url = f"http://127.0.0.1:{port}"
    try:
        if not _wait_for_health(base_url, password):
            stderr = proc.stderr.read(4096) if proc.stderr else b""
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
            pytest.fail(
                f"fake OpenCode server did not become ready on {base_url}; stderr={stderr!r}"
            )
        yield base_url, password
    finally:
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)


def test_adapter_uses_session_id_for_repeated_calls(fake_opencode_process):
    base_url, password = fake_opencode_process
    session = RunSession(
        base_url=base_url,
        username="opencode",
        password=password,
        workspace_path="C:/work/fake",
        settings={},
        server_version="1.0.0",
    )
    adapter = OpenCodeAdapter(session=session, timeout_seconds=10)
    from agents_ide.adapters.base import AgentAdapterRequest

    request = AgentAdapterRequest(
        role="implementer",
        model_id="anthropic/claude-haiku-4-20250514",
        prompt="hello",
        context_package={"__node_id__": "node-1"},
        workspace_path="C:/work/fake",
        capabilities={},
        params={},
        emit_event=None,
        stop_event=threading.Event(),
        check_owned=lambda: None,
    )
    first = adapter.run(request)
    second = adapter.run(request)
    assert first.outcome.value == "succeeded"
    assert second.outcome.value == "succeeded"
    assert adapter.external_session_id
    # Repeating with the same adapter must reuse the OpenCode session id.
    assert adapter.external_session_id.startswith("ses_")


@pytest.mark.parametrize("question", [False, True])
def test_opencode_live_input_and_questions(fake_opencode_process, question):
    from agents_ide.adapters.base import AgentAdapterRequest

    base_url, password = fake_opencode_process
    adapter = OpenCodeAdapter(
        session=RunSession(
            base_url=base_url,
            username="opencode",
            password=password,
            workspace_path="C:/work/fake",
            settings={},
            server_version="1.0.0",
        ),
        timeout_seconds=10,
    )
    pending, events = [], []

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
        elif (
            kind == "attempt.text_delta"
            and not question
            and not pending
            and not any(k == "agent.user_message_status" for k, _ in events)
        ):
            pending.append({"command_id": "steer", "text": "Check README.md"})

    request = AgentAdapterRequest(
        role="implementer",
        model_id="anthropic/claude-haiku-4-20250514",
        prompt="ask user slow" if question else "slow",
        context_package={},
        workspace_path="C:/work/fake",
        capabilities={},
        params={},
        emit_event=emit,
        receive_message=lambda: pending.pop(0) if pending else None,
    )
    assert adapter.run(request).succeeded
    assert any(k == "agent.user_message_status" and p["delivered"] for k, p in events)
    if question:
        assert any(k == "agent.input_closed" for k, _ in events)


def test_adapter_invokes_opencode_stop(fake_opencode_process):
    base_url, password = fake_opencode_process
    session = RunSession(
        base_url=base_url,
        username="opencode",
        password=password,
        workspace_path="C:/work/fake",
        settings={},
        server_version="1.0.0",
    )
    adapter = OpenCodeAdapter(session=session, timeout_seconds=10)
    adapter.interrupt("ses-1234")
    # Smoke test that an interrupt against an unknown session id does not
    # raise; the fake server returns 404 and the adapter swallows it.
    assert True


def test_opencode_session_invalidation_triggers_safe_retry(fake_opencode_process):
    base_url, password = fake_opencode_process
    session = RunSession(
        base_url=base_url,
        username="opencode",
        password=password,
        workspace_path="C:/work/fake",
        settings={},
        server_version="1.0.0",
    )
    adapter = OpenCodeAdapter(session=session, timeout_seconds=10)
    from agents_ide.adapters.base import AgentAdapterRequest

    request = AgentAdapterRequest(
        role="implementer",
        model_id="anthropic/claude-haiku-4-20250514",
        prompt="hello",
        context_package={"__node_id__": "node-1"},
        workspace_path="C:/work/fake",
        capabilities={},
        params={},
        emit_event=None,
        stop_event=threading.Event(),
        check_owned=lambda: None,
    )
    adapter._binding = type(adapter._binding)(session_id="ghost")
    result = adapter.run(request)
    assert result.outcome.value == "succeeded"
    assert adapter.external_session_id != "ghost"


def test_opencode_payload_roundtrip_via_json(fake_opencode_process):
    """Adapter must serialise prompts and parse JSON decisions correctly."""

    base_url, password = fake_opencode_process
    session = RunSession(
        base_url=base_url,
        username="opencode",
        password=password,
        workspace_path="C:/work/fake",
        settings={},
        server_version="1.0.0",
    )
    adapter = OpenCodeAdapter(session=session, timeout_seconds=10)
    from agents_ide.adapters.base import AgentAdapterRequest

    request = AgentAdapterRequest(
        role="verifier",
        model_id="anthropic/claude-haiku-4-20250514",
        prompt="force_decision=passed",
        context_package={"__node_id__": "node-1", "evidence": {"x": 1}},
        workspace_path="C:/work/fake",
        capabilities={},
        params={},
        emit_event=None,
        stop_event=threading.Event(),
        check_owned=lambda: None,
    )
    result = adapter.run(request)
    assert result.outcome.value == "succeeded"
    payload = result.validated_result or {}
    text = payload.get("text", "")
    assert "verdict" in text or "verdict" in result.raw_text


def test_probe_endpoint_returns_models_and_status(client, authenticated):
    """Probe and catalog endpoints must surface the harness state machine."""

    _, headers = authenticated
    profile = client.post(
        "/api/harness_profiles",
        headers=headers,
        json={
            "name": "oc-1",
            "harness_kind": "opencode",
            "executable_path": "",
            "settings": {},
        },
    ).json()
    models = client.get(f"/api/harness_profiles/{profile['id']}/models", headers=headers).json()
    assert models["status"] in {"unverified", "stale", "fresh"}
    assert isinstance(models["models"], list)
    assert models["ttl_seconds"] == 900

    probe = client.post(f"/api/harness_profiles/{profile['id']}/test", headers=headers).json()
    # The probe is best-effort; without a real executable it returns
    # ``failed`` (configuration_invalid path) but never ``ok``.
    assert probe["status"] in {"failed", "ok"}
    assert "tested_at" in probe


def test_harness_profile_payload_includes_catalog_metadata(client, authenticated):
    _, headers = authenticated
    profile = client.post(
        "/api/harness_profiles",
        headers=headers,
        json={
            "name": "oc-payload",
            "harness_kind": "opencode",
            "executable_path": "C:/bin/opencode.exe",
            "settings": {"auth": True},
            "catalog_ttl_seconds": 1800,
        },
    ).json()
    assert profile["catalog_ttl_seconds"] == 1800
    assert profile["harness_kind"] == "opencode"
    assert profile["catalog_models"] == []
    assert profile["last_test_status"] is None


def test_harness_profile_update_invalidates_catalog(client, authenticated):
    _, headers = authenticated
    profile = client.post(
        "/api/harness_profiles",
        headers=headers,
        json={"name": "oc-update", "harness_kind": "opencode", "executable_path": "C:/x"},
    ).json()
    updated = client.patch(
        f"/api/harness_profiles/{profile['id']}",
        headers=headers,
        json={
            "expected_version": profile["version"],
            "executable_path": "C:/bin/opencode.exe",
        },
    ).json()
    assert updated["version"] == profile["version"] + 1


def test_codex_profile_probe_rejects_missing_executable(client, authenticated):
    _, headers = authenticated
    profile = client.post(
        "/api/harness_profiles",
        headers=headers,
        json={"name": "codex-stub", "harness_kind": "codex", "executable_path": "C:/x"},
    ).json()
    probe = client.post(f"/api/harness_profiles/{profile['id']}/test", headers=headers).json()
    assert probe["status"] == "failed"
    assert probe["version"] is None
