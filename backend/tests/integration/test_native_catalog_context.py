"""Remaining-plan regressions: native metadata, raw envelopes, pinned reads."""

import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_stage6b_codex import _adapter, _make_request, codex_stream, codex_trace  # noqa: F401
from test_stage9a_harness import _setup_with_harness, create_simulated

from agents_ide.adapters.model_catalog import CatalogModels, codex_metadata, parameters_for
from agents_ide.adapters.native_events import archive_native
from agents_ide.engine import codex_runtime
from agents_ide.errors import AppError
from agents_ide.security.native_credentials import fingerprint
from agents_ide.services.planning_context import capture_context
from agents_ide.worker.processes import ProcessGroup, wait_stopped


def test_catalog_exposes_native_effort_and_invalidates_on_external_auth_change(
    authenticated, tmp_path, monkeypatch
):
    client, headers = authenticated
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    auth = tmp_path / "auth.json"
    auth.write_text('{"access_token":"first-synthetic"}')
    monkeypatch.setattr(
        codex_runtime,
        "probe_executable",
        lambda *args: (
            "codex-cli 0.153.4",
            CatalogModels(
                {"model": {"source": "native_catalog", "reasoning_efforts": ["low", "high"]}}
            ),
        ),
    )
    profile = client.post(
        "/api/harness_profiles",
        headers=headers,
        json={
            "name": "Native metadata",
            "harness_kind": "codex",
            "executable_path": str(tmp_path / "codex.exe"),
        },
    ).json()
    base = f"/api/harness_profiles/{profile['id']}"
    result = client.post(base + "/test", headers=headers)
    assert result.status_code == 200, result.text
    catalog = client.get(base + "/models").json()
    assert catalog["status"] == "fresh", catalog
    assert catalog["models"][0]["reasoning_efforts"] == ["low", "high"]
    current = client.get(base).json()
    assert current["model_capabilities"]["model"]["source"] == "native_catalog"
    before = fingerprint("codex", profile["executable_path"])
    auth.write_text('{"access_token":"other-synthetic"}')
    assert fingerprint("codex", profile["executable_path"]) != before
    changed = client.get(base).json()
    assert changed["version"] == current["version"] + 1
    assert changed["model_capabilities"] == {}
    assert client.get(base + "/models").json()["status"] == "unverified"
    assert "synthetic" not in json.dumps(changed)


def test_auth_change_during_probe_does_not_certify_stale_metadata(
    authenticated, tmp_path, monkeypatch
):
    client, headers = authenticated
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))

    def probe(*args):
        (tmp_path / "auth.json").write_text("changed")
        return "codex-cli 0.153.4", CatalogModels({"model": {"reasoning_efforts": ["low"]}})

    monkeypatch.setattr(codex_runtime, "probe_executable", probe)
    profile = client.post(
        "/api/harness_profiles",
        headers=headers,
        json={
            "name": "Racing credentials",
            "harness_kind": "codex",
            "executable_path": str(tmp_path / "codex.exe"),
        },
    ).json()
    result = client.post(f"/api/harness_profiles/{profile['id']}/test", headers=headers)
    assert result.status_code == 409, result.text
    assert result.json()["code"] == "version_conflict"


@pytest.mark.parametrize("params", [{"reasoning_effort": "max"}, {"temperature": 0.1}])
def test_unsupported_native_params_never_clamp(params):
    with pytest.raises(AppError):
        parameters_for("codex", "model", params, {"model": {"reasoning_efforts": ["low"]}})


@pytest.mark.parametrize("bad", ["HIGH!", "very high", "x" * 33, 4, ["high"]])
def test_malformed_live_ladder_is_not_partially_accepted(bad):
    # Adapted from Claudexor effort.test.ts: a malformed live ladder is not narrowed.
    with pytest.raises(OSError):
        codex_metadata(
            {"supportedReasoningEfforts": [{"reasoningEffort": "low"}, {"reasoningEffort": bad}]}
        )


def test_native_ladder_keeps_order_and_rejects_invalid_default():
    source = {
        "supportedReasoningEfforts": [{"reasoningEffort": v} for v in ("low", "high", "hyperdrive")]
    }
    assert codex_metadata(source)["reasoning_efforts"] == ["low", "high", "hyperdrive"]
    with pytest.raises(OSError):
        codex_metadata({**source, "defaultReasoningEffort": "missing"})


@pytest.mark.parametrize("error_code", [267, 5])
def test_sandbox_warmup_retries_only_known_prelaunch_failure(tmp_path, monkeypatch, error_code):
    monkeypatch.setenv("SYSTEMROOT", str(tmp_path))
    calls = []

    def response(method, params, **kwargs):
        calls.append((method, params))
        if len(calls) == 1:
            return {
                "error": {
                    "code": -32603,
                    "message": "exec failed: windows sandbox: CreateProcessWithLogonW failed: "
                    + str(error_code),
                }
            }
        return {"result": {"exitCode": 0, "stdout": "AGENTS_IDE_SANDBOX_READY\n"}}

    adapter = SimpleNamespace(_request_response=response)
    if error_code == 267:
        assert codex_runtime.verify_read_sandbox(adapter, tmp_path, lambda: None)
        assert len(calls) == 2 and calls[0] == calls[1]
    else:
        with pytest.raises(AppError):
            codex_runtime.verify_read_sandbox(adapter, tmp_path, lambda: None)
        assert len(calls) == 1
    assert all(method == "command/exec" for method, _ in calls)


def test_raw_archive_keeps_unknown_envelope_and_scrubs_credentials():
    events = []
    archive_native(
        lambda kind, body: events.append((kind, body)),
        "codex",
        "future/notification",
        {
            "nested": {"access_token": "synthetic", "x": 23},
            "_answered": True,
            "text": "Authorization: Bearer synthetic-credential",
        },
        "thread",
    )
    kind, body = events[0]
    assert kind == "agent.native_event"
    assert body["native_type"] == "future/notification"
    assert body["payload"]["nested"] == {"access_token": "[REDACTED]", "x": 23}
    assert "synthetic" not in json.dumps(body)
    assert "_answered" not in body["payload"]


def test_codex_effort_on_wire_and_scoped_native_archive(codex_stream):  # noqa: F811
    stream, _, trace = codex_stream
    adapter = _adapter(stream)
    adapter.session.settings["model_metadata"] = {"gpt-5.6-sol": {"reasoning_efforts": ["high"]}}
    events = []
    result = adapter.run(
        replace(
            _make_request(),
            params={"reasoning_effort": "high"},
            emit_event=lambda kind, body: events.append((kind, body)),
        )
    )
    assert result.succeeded
    records = [json.loads(line)["message"] for line in trace.read_text().splitlines()]
    turn = next(row for row in records if row.get("method") == "turn/start")
    assert turn["params"]["effort"] == "high"
    raw = [body for kind, body in events if kind == "agent.native_event"]
    assert any(e["native_type"] == "turn/completed" for e in raw)
    assert all(e["session_id"] == adapter.external_session_id for e in raw)


def test_council_files_are_pinned_and_secrets_rejected(authenticated, tmp_path):
    client, headers = authenticated
    project, _, _, _, payload = _setup_with_harness(
        client, headers, tmp_path, kind="opencode", settings={"permission_mode": "no_tools"}
    )
    root = Path(project["workspace"]["normalized_path"])
    (root / "notes.md").write_text("pinned context", encoding="utf-8")
    payload["context_paths"] = ["notes.md"]
    job = create_simulated(client, payload)
    from agents_ide.persistence.models import PlanningJob

    with client.app.state.session_factory() as session:
        stored = session.get(PlanningJob, job["id"])
        snapshot = json.loads(stored.context_snapshot_json)
        assert snapshot["manifest"][0]["path"] == "notes.md"
        assert json.loads(snapshot["context_section"])["files"][0]["content"] == "pinned context"
    (root / "notes.md").write_text("changed later", encoding="utf-8")
    assert "changed later" not in json.dumps(snapshot)


@pytest.mark.parametrize("path", [".env", "../outside.txt", ".git/config", "missing.txt"])
def test_context_paths_fail_closed(tmp_path, path):
    session = SimpleNamespace(scalars=lambda query: [])
    project = SimpleNamespace(workspace_normalized_path=str(tmp_path))
    with pytest.raises(AppError) as error:
        capture_context(session, project, "", [path])
    assert error.value.code in {"planning_context_denied", "planning_context_unavailable"}


def test_context_capture_refuses_existing_writer(tmp_path):
    session = SimpleNamespace(scalars=lambda query: [SimpleNamespace(workspace_json=None)])
    project = SimpleNamespace(workspace_normalized_path=str(tmp_path))
    with pytest.raises(AppError, match="Run"):
        capture_context(session, project, "", ["notes.md"])


def test_context_rejects_hard_link_to_protected_content(tmp_path):
    secret = tmp_path / ".env"
    secret.write_text("TOKEN=synthetic-secret", encoding="utf-8")
    (tmp_path / "notes.md").hardlink_to(secret)
    session = SimpleNamespace(scalars=lambda query: [])
    project = SimpleNamespace(workspace_normalized_path=str(tmp_path))
    with pytest.raises(AppError) as error:
        capture_context(session, project, "", ["notes.md"])
    assert error.value.code == "planning_context_unavailable"


@pytest.mark.windows
@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object duplicate handle")
def test_explicit_job_shutdown_does_not_depend_on_last_handle(tmp_path):
    import win32api
    import win32con

    group = ProcessGroup()
    duplicate = None
    try:
        child = group.start(
            [sys.executable, "-c", "import time; time.sleep(120)"], tmp_path, dict(os.environ)
        )
        current = win32api.GetCurrentProcess()
        duplicate = win32api.DuplicateHandle(
            current, group.job, current, 0, False, win32con.DUPLICATE_SAME_ACCESS
        )
        group.close()
        assert wait_stopped([child], 5)
    finally:
        if duplicate is not None:
            duplicate.Close()
        group.close()
