"""Review regressions at HTTP, process, persistence and graph boundaries."""

import asyncio
import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import httpx
import pytest
from sqlalchemy import select
from test_stage4_review import make_run
from test_stage5_review import command, run_now, wait_for
from test_stage7_llm_commands_evidence import (
    FakeOpenAIHandler,
    _chat_body,
    _init_git,
    _local_launcher,
    _request,
    _spec,
)
from test_stage7_llm_commands_evidence import (
    llm_server as provider_fixture,
)

from agents_ide.adapters.base import ExternalOutcome
from agents_ide.adapters.llm_http import (
    HttpLLMAdapter,
    _parse_stream,
    _retry_after,
    probe_connection,
)
from agents_ide.engine import artifacts, context_sources
from agents_ide.engine.commands import execute_commands
from agents_ide.persistence.models import (
    ArtifactManifest,
    ProcessSupervision,
    Run,
    StepAttempt,
    StepExecution,
)

llm_server = provider_fixture


def chain(*nodes):
    all_nodes = [{"id": "s", "type": "Start"}, *nodes, {"id": "e", "type": "End"}]
    return {
        "nodes": all_nodes,
        "edges": [
            {"from": a["id"], "to": b["id"]} for a, b in zip(all_nodes, all_nodes[1:], strict=False)
        ],
    }


def check_node(*scripts, filter_value=None):
    config = {
        "commands": [
            {
                "id": f"cmd{i}",
                "program": sys.executable,
                "args": ["-c", script],
                "success_exit_codes": [0],
                "retry_safety": "safe",
            }
            for i, script in enumerate(scripts)
        ]
    }
    if filter_value is not None:
        config["command_filter"] = filter_value
    return {"id": "checks", "type": "Command", "config": config}


def collect_node(sources=None, **config):
    return {
        "id": "context",
        "type": "CollectContext",
        "config": {
            "sources": sources or [{"kind": "file", "path": "a.txt"}],
            "include_untracked": True,
            **config,
        },
    }


def verify_node():
    return {
        "id": "verify",
        "type": "LLMRequest",
        "config": {
            "prompt": "verify",
            "response_format": "json",
            "model_selection": {
                "kind": "direct",
                "model_id": "alpha",
                "provider_connection_id": "CONNECTION",
            },
        },
    }


@pytest.mark.parametrize("status", [408, 409, 425, 500, 502, 504])
def test_response_status_does_not_prove_no_effect(llm_server, status):
    FakeOpenAIHandler.routes["chat"] = {"status": status}
    result = HttpLLMAdapter().run(_request(llm_server[1]))
    assert result.outcome == ExternalOutcome.UNKNOWN and not result.no_effect


@pytest.mark.parametrize(
    "data",
    [
        'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n',
        "data: rubbish\n\ndata: [DONE]\n",
        'data: {"error":{"message":"failed"}}\n\ndata: [DONE]\n',
    ],
)
def test_incomplete_or_broken_stream_cannot_succeed(data):
    with pytest.raises(ValueError):
        _parse_stream(data)


def test_http_deadline_and_stop_cancel_silent_request(monkeypatch):
    original = httpx.AsyncClient
    started = threading.Event()
    closed = threading.Event()

    async def handler(request):
        started.set()
        try:
            await asyncio.sleep(10)
        finally:
            closed.set()
        return httpx.Response(200, json=_chat_body("ok"))

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw)
    )
    stop = threading.Event()
    request = replace(
        _request("http://127.0.0.1/v1"), stop_event=stop, deadline_at=time.time() + 0.2
    )
    now = time.monotonic()
    result = HttpLLMAdapter().run(request)
    assert result.outcome == ExternalOutcome.UNKNOWN and time.monotonic() - now < 2
    assert started.is_set() and closed.is_set()
    started.clear()
    stop.set()
    result = HttpLLMAdapter().run(request)
    assert result.no_effect and not started.is_set()


def test_write_timeout_never_retries_as_connection_failure(monkeypatch):
    original = httpx.AsyncClient

    def handler(request):
        raise httpx.WriteTimeout("partial request sent")

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw)
    )
    result = HttpLLMAdapter().run(_request("http://127.0.0.1/v1"))
    assert result.outcome == ExternalOutcome.UNKNOWN and not result.no_effect


def test_http_error_body_is_bounded_before_parse(llm_server):
    FakeOpenAIHandler.routes["chat"] = {"status": 500, "raw": "x" * 200000}
    result = HttpLLMAdapter(max_response_bytes=1024).run(_request(llm_server[1]))
    assert result.error.code == "response_too_large" and not result.no_effect


def test_probe_rejects_http_200_with_invalid_body(llm_server):
    FakeOpenAIHandler.routes["chat"] = {"status": 200, "raw": "not JSON"}
    assert not probe_connection({"base_url": llm_server[1]}).ok


def test_retry_after_supports_dates_and_has_no_early_cap():
    from datetime import UTC, datetime, timedelta
    from email.utils import format_datetime

    response = httpx.Response(429, headers={"Retry-After": "180"})
    assert _retry_after(response) == 180
    response = httpx.Response(
        429, headers={"Retry-After": format_datetime(datetime.now(UTC) + timedelta(seconds=90))}
    )
    assert 88 <= _retry_after(response) <= 90
    assert _retry_after(httpx.Response(429, headers={"Retry-After": "NaN"})) is None


def test_output_cap_is_shared_and_keeps_exact_prefix(tmp_path):
    result = execute_commands(
        [_spec(args=("-c", "import os; os.write(1,b'x'*2000); os.write(2,b'y'*2000)"), cap=1024)],
        workspace=tmp_path,
        launcher=_local_launcher(),
    )
    report = json.loads(result.raw_text)["commands"][0]
    assert len(report["stdout"].encode()) + len(report["stderr"].encode()) == 1024
    assert report["dropped_bytes"] == 2976 and result.decision == "inconclusive"


def test_explicit_os_environment_overrides_inherited_value(tmp_path, monkeypatch):
    monkeypatch.setenv("TEMP", "inherited")
    result = execute_commands(
        [_spec(args=("-c", "import os; print(os.environ['TEMP'])"), env={"TEMP": "configured"})],
        workspace=tmp_path,
        launcher=_local_launcher(),
    )
    assert json.loads(result.raw_text)["commands"][0]["stdout"].strip() == "configured"


def test_failed_unsafe_command_is_never_replayed(tmp_path):
    spec = _spec(
        args=(
            "-c",
            "from pathlib import Path; p=Path('calls'); "
            "p.write_text(p.read_text()+'x' if p.exists() else 'x'); raise SystemExit(1)",
        ),
        retry_safety="unsafe",
    )
    first = execute_commands([spec], workspace=tmp_path, launcher=_local_launcher())
    previous = {r["id"]: r for r in json.loads(first.raw_text)["commands"]}
    second = execute_commands(
        [spec], workspace=tmp_path, launcher=_local_launcher(), completed=previous
    )
    assert (tmp_path / "calls").read_text() == "x" and second.decision == "failed"


def test_stopping_last_unsafe_command_is_unknown(tmp_path):
    stop = threading.Event()
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(
            execute_commands,
            [
                _spec(
                    args=(
                        "-c",
                        "from pathlib import Path; import time; "
                        "Path('started').touch(); time.sleep(20)",
                    ),
                    retry_safety="unsafe",
                )
            ],
            workspace=tmp_path,
            launcher=_local_launcher(),
            stop_event=stop,
        )
        deadline = time.monotonic() + 5
        while not (tmp_path / "started").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert (tmp_path / "started").exists()
        stop.set()
        result = future.result(timeout=10)
    assert result.outcome == ExternalOutcome.UNKNOWN and not result.no_effect


def test_runner_commands_collect_manifest_and_current_report(authenticated, tmp_path, settings):
    graph = chain(
        check_node("from pathlib import Path; Path('a.txt').write_text('proof'); print('passed')"),
        collect_node(
            [{"kind": "file", "path": "a.txt"}, {"kind": "command_report", "command_id": "cmd0"}]
        ),
    )
    run, factory = make_run(authenticated, tmp_path, graph=graph, execution_mode="real")
    assert run_now(run, factory, settings).final_state == "completed"
    with factory() as session:
        ledger = list(
            session.scalars(
                select(ArtifactManifest).where(ArtifactManifest.schema_type == "command_ledger")
            )
        )
        assert len(ledger) == 2
        manifest = session.scalar(
            select(ArtifactManifest).where(ArtifactManifest.schema_type == "context_package")
        )
        body = context_sources.artifact_body(manifest)
        assert any(row["kind"] == "command_report" for row in body["files"]), body
        assert json.loads(manifest.files_json) and body["workspace_hash"]
        assert all(row.state == "finished" for row in session.scalars(select(ProcessSupervision)))


def test_empty_filter_does_not_launch_or_consume_call_budget(authenticated, tmp_path, settings):
    run, factory = make_run(
        authenticated,
        tmp_path,
        graph=chain(check_node("raise Exception('must not execute')", filter_value=[])),
        execution_mode="real",
    )
    assert run_now(run, factory, settings).final_state == "completed"
    with factory() as session:
        assert session.scalar(select(ProcessSupervision)) is None
        assert json.loads(session.get(Run, run["id"]).runtime_json)["external_calls"] == 0


def test_command_failure_after_preflight_is_saved_without_crashing(
    authenticated, tmp_path, settings, monkeypatch
):
    run, factory = make_run(
        authenticated, tmp_path, graph=chain(check_node("print('ok')")), execution_mode="real"
    )
    monkeypatch.setattr(
        "agents_ide.engine.commands.resolve_program",
        lambda *args: (_ for _ in ()).throw(
            __import__("agents_ide.errors", fromlist=["AppError"]).AppError(
                "configuration_invalid", "missing", 422
            )
        ),
    )
    assert run_now(run, factory, settings).final_state == "waiting_input"
    with factory() as session:
        attempt = session.scalar(select(StepAttempt))
        assert attempt.status == "failed" and attempt.error_code == "configuration_invalid"


def test_runner_stop_resume_retains_subcommand_ledger(authenticated, tmp_path, settings):
    node = check_node(
        "from pathlib import Path; p=Path('calls'); "
        "p.write_text(p.read_text()+'x' if p.exists() else 'x')",
        "from pathlib import Path; import time; Path('started').touch()\n"
        "while not Path('continue').exists(): time.sleep(0.02)",
    )
    node["config"]["commands"][0]["retry_safety"] = "unsafe"
    run, factory = make_run(authenticated, tmp_path, graph=chain(node), execution_mode="real")
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(run_now, run, factory, settings)
        wait_for(factory, lambda s: (tmp_path / "workspace" / "started").exists())
        assert command(authenticated, run, "stop").status_code == 200
        assert future.result(timeout=15).final_state == "stopped"
    (tmp_path / "workspace" / "continue").touch()
    assert command(authenticated, run, "resume").status_code == 200
    assert run_now(run, factory, settings).final_state == "completed"
    assert (tmp_path / "workspace" / "calls").read_text() == "x"
    with factory() as session:
        assert (
            session.scalar(
                select(StepExecution).where(StepExecution.node_id == "checks")
            ).attempt_count
            == 2
        )


def test_required_failure_overrides_model_passed(authenticated, tmp_path, settings, monkeypatch):
    from agents_ide.adapters.base import LLMResult

    def answer(self, request):
        assert request.context_package["evidence"]["required_failed"] == ["cmd0"]
        return LLMResult(ExternalOutcome.SUCCEEDED, '{"verdict":"passed"}', None, None)

    monkeypatch.setattr(HttpLLMAdapter, "run", answer)
    run, factory = make_run(
        authenticated,
        tmp_path,
        graph=chain(check_node("raise SystemExit(1)"), verify_node()),
        execution_mode="real",
    )
    assert run_now(run, factory, settings).final_state == "completed"
    with factory() as session:
        step = session.scalar(select(StepExecution).where(StepExecution.node_id == "verify"))
        assert step.decision == "false"
        assert json.loads(step.validated_result_json)["model_verdict"] == "passed"
        raw = json.loads(session.get(ArtifactManifest, step.raw_result_ref).body_json)
        assert raw["raw_text"] == '{"verdict":"passed"}'


def test_context_bounds_entire_package_and_summary(authenticated, tmp_path):
    workspace = tmp_path / "source"
    workspace.mkdir()
    (workspace / "a.txt").write_text('"' * 10000)
    _init_git(workspace)
    (workspace / "a.txt").write_text('"' * 12000)
    with authenticated[0].app.state.session_factory() as session:
        result = context_sources.collect_context(
            {
                "sources": [{"kind": "diff"}, {"kind": "file", "path": "a.txt"}],
                "max_total_bytes": 1024,
                "summary_max_bytes": 256,
            },
            workspace=workspace,
            session=session,
            run_id="unused",
            base_head_sha=None,
        )
    assert len(artifacts.encode(result.package).encode()) <= 1024
    assert len(artifacts.encode(result.summary).encode()) <= 256
    assert result.package["truncated"]


def test_context_diff_excludes_protected_paths_and_textconv(authenticated, tmp_path):
    workspace = tmp_path / "source"
    workspace.mkdir()
    (workspace / "a.txt").write_text("before")
    _init_git(workspace)
    (workspace / ".env").write_text("PRIVATE_VALUE=never-send")
    import subprocess

    subprocess.run(["git", "add", ".env"], cwd=workspace, check=True, capture_output=True)
    (workspace / "a.txt").write_text("after")
    with authenticated[0].app.state.session_factory() as session:
        result = context_sources.collect_context(
            {"sources": [{"kind": "diff"}]},
            workspace=workspace,
            session=session,
            run_id="unused",
            base_head_sha=None,
        )
    assert "never-send" not in artifacts.encode(result.package)
    assert any(row.get("path") == ".env" for row in result.omissions)


def test_resolve_does_not_allow_excluded_glob_path():
    result = context_sources.resolve_requests(
        {"sources": [{"kind": "glob", "path": "**/*.txt", "exclude": ["private/*"]}]},
        missing=[{"kind": "file", "path": "private/key.txt", "reason": "want it"}],
        artifact_exists=lambda _: False,
        commands={},
    )
    assert result["denied_requests"] and not result["allowed_requests"]


def test_read_checks_the_opened_handle_after_path_swap(tmp_path, monkeypatch):
    from pathlib import Path

    from agents_ide.security.workspace_read import read_workspace_file

    root = tmp_path / "root"
    root.mkdir()
    target = root / "visible.txt"
    target.write_text("safe")
    outside = tmp_path / "outside.txt"
    outside.write_text("private")
    original = Path.open
    monkeypatch.setattr(
        Path,
        "open",
        lambda path, *args, **kw: original(outside if path == target else path, *args, **kw),
    )
    with pytest.raises(ValueError, match="outside_workspace"):
        read_workspace_file(root, "visible.txt", 1024)


def test_later_file_change_invalidates_successful_command_report(
    authenticated, tmp_path, settings, monkeypatch
):
    from agents_ide.adapters.base import LLMResult

    def answer(self, request):
        assert request.context_package["evidence"]["required_incomplete"] == ["cmd0"]
        return LLMResult(ExternalOutcome.SUCCEEDED, '{"verdict":"passed"}', None, None)

    monkeypatch.setattr(HttpLLMAdapter, "run", answer)
    checks = check_node(
        "print('tested original tree')",
        "from pathlib import Path; Path('a.txt').write_text('changed after test')",
    )
    run, factory = make_run(
        authenticated, tmp_path, graph=chain(checks, verify_node()), execution_mode="real"
    )
    assert run_now(run, factory, settings).final_state == "completed"
    with factory() as session:
        step = session.scalar(select(StepExecution).where(StepExecution.node_id == "verify"))
        assert json.loads(step.validated_result_json)["verdict"] == "inconclusive"


@pytest.mark.parametrize(
    "requested,limit,expected",
    [
        ([{"kind": "command_report", "command_id": "cmd0", "reason": "missing"}], 2, "completed"),
        ([{"kind": "file", "path": ".env", "reason": "want"}], 2, "waiting_input"),
        (
            [{"kind": "command_report", "command_id": "cmd0", "reason": "missing"}],
            0,
            "waiting_input",
        ),
    ],
)
def test_dynamic_evidence_command_filter_and_denied_requests(
    authenticated, tmp_path, settings, monkeypatch, requested, limit, expected
):
    from agents_ide.adapters.base import LLMResult

    def answer(self, request):
        return LLMResult(
            ExternalOutcome.SUCCEEDED,
            json.dumps({"verdict": "inconclusive", "missing_evidence": requested}),
            None,
            None,
        )

    monkeypatch.setattr(HttpLLMAdapter, "run", answer)
    resolver = collect_node(mode="resolve_requests", max_command_replays=limit)
    checks = check_node(
        "from pathlib import Path; Path('ran').touch()",
        filter_value={"ref": "steps.context.latest.validated_result.command_ids"},
    )
    run, factory = make_run(
        authenticated, tmp_path, graph=chain(verify_node(), resolver, checks), execution_mode="real"
    )
    assert run_now(run, factory, settings).final_state == expected
    assert (tmp_path / "workspace" / "ran").exists() == (expected == "completed")


def test_old_cycle_required_checks_remain_incomplete(authenticated, tmp_path, settings):
    from test_stage5_review import runner_for

    run, factory = make_run(
        authenticated, tmp_path, graph=chain(check_node("print('pass')")), execution_mode="real"
    )
    runner, _ = runner_for(run, factory, settings)
    assert runner.execute(run["id"]).final_state == "completed"
    runner.runtime["cycle_id"] += 1
    with factory() as session:
        evidence = runner._evidence_package(session)
    assert evidence["required_incomplete"] == ["cmd0"]
    assert not evidence["command_reports"]


def test_preflight_pins_exact_program_in_snapshot(authenticated, tmp_path, settings):
    run, factory = make_run(
        authenticated, tmp_path, graph=chain(check_node("print('ok')")), execution_mode="real"
    )
    with factory() as session:
        saved = session.get(Run, run["id"])
        assert (
            json.loads(saved.snapshot_json)["dependencies"]["command_programs"]["checks"]["cmd0"]
            == sys.executable
        )
    assert run_now(run, factory, settings).final_state == "completed"


@pytest.mark.parametrize(
    "signal", [{"finish_reason": "error"}, {"error": {"code": 502, "message": "partial failure"}}]
)
def test_upstream_terminal_choice_error_overrides_partial_passed(llm_server, signal):
    # Scenario adapted from Claudexor harness-raw-api/src/parse.test.ts.
    body = {"choices": [{"message": {"content": '{"verdict":"passed"}'}, **signal}]}
    FakeOpenAIHandler.routes["chat"] = {"status": 200, "body": body}
    result = HttpLLMAdapter().run(_request(llm_server[1], response_format="json"))
    assert result.outcome == ExternalOutcome.UNKNOWN and not result.no_effect
    assert result.raw_text


def test_pause_completes_current_command_list_before_pausing(authenticated, tmp_path, settings):
    graph = chain(
        check_node(
            "from pathlib import Path; import time; Path('started').touch(); time.sleep(0.5)",
            "from pathlib import Path; Path('second').touch()",
        )
    )
    run, factory = make_run(authenticated, tmp_path, graph=graph, execution_mode="real")
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(run_now, run, factory, settings)
        wait_for(factory, lambda s: (tmp_path / "workspace" / "started").exists())
        assert command(authenticated, run, "pause").status_code == 200
        assert future.result(timeout=15).final_state == "paused"
    assert (tmp_path / "workspace" / "second").exists()


def test_stream_and_structured_options_are_accepted_by_graph_preflight(authenticated, tmp_path):
    node = verify_node()
    node["config"]["params"] = {"stream": True, "structured_output": True, "timeout_seconds": 5}
    run, factory = make_run(authenticated, tmp_path, graph=chain(node), execution_mode="real")
    with factory() as session:
        snapshot = json.loads(session.get(Run, run["id"]).snapshot_json)
        assert "llm_http_options" in snapshot["required_features"]


def test_connection_probe_does_not_change_pinned_configuration_revision(authenticated, llm_server):
    client, headers = authenticated
    provider = client.post(
        "/api/connections", headers=headers, json={"name": "probe", "base_url": llm_server[1]}
    ).json()
    tested = client.post(f"/api/connections/{provider['id']}/test", headers=headers)
    assert tested.status_code == 200 and tested.json()["status"] == "ok"
    current = client.get(f"/api/connections/{provider['id']}", headers=headers).json()
    assert current["version"] == provider["version"] and current["last_test_status"] == "ok"


@pytest.mark.parametrize(
    "node",
    [
        {
            "id": "later",
            "type": "AgentTask",
            "config": {
                "prompt": "work",
                "model_selection": {
                    "kind": "direct",
                    "model_id": "m",
                    "harness_profile_id": "PROFILE",
                },
            },
        },
    ],
)
def test_future_node_gates_return_valid_waiting_reason(authenticated, tmp_path, settings, node):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.txt").write_text("base")
    _init_git(workspace)
    run, factory = make_run(authenticated, tmp_path, graph=chain(node), execution_mode="real")
    assert run_now(run, factory, settings).final_state == "waiting_input"
    with factory() as session:
        reason = json.loads(session.get(Run, run["id"]).waiting_reason_json)
        # The run must surface a typed ``configuration_invalid`` waiting
        # reason without falling through to a generic 5xx. The exact phrase
        # of the inner reason moves with the harness adapters (opencode,
        # codex, future kinds); only the structural code is stable.
        assert reason["code"] == "configuration_invalid"
        assert reason["details"]["reason"] in {
            "configuration_invalid",
            "harness_adapter_unimplemented",
        }


def test_plan_control_cannot_complete_without_verification(authenticated, tmp_path, settings):
    node = {"id": "select_next", "type": "PlanControl", "config": {"operation": "select_next"}}
    run, factory = make_run(
        authenticated,
        tmp_path,
        graph=chain(node),
        execution_mode="real",
        inputs={"plan": "one fixed item"},
    )
    result = run_now(run, factory, settings)
    assert result.final_state == "waiting_input"
    assert result.waiting_reason.code == "missing_data"


def test_git_commit_requires_verification(authenticated, tmp_path, settings):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.txt").write_text("base")
    _init_git(workspace)
    node = {"id": "commit", "type": "GitCommit", "config": {"message": "stage8"}}
    run, factory = make_run(authenticated, tmp_path, graph=chain(node), execution_mode="real")
    result = run_now(run, factory, settings)
    assert result.final_state == "waiting_input"
    assert result.waiting_reason.details["reason"] == "git_verification_required"
    assert (
        subprocess.check_output(
            ["git", "-C", str(workspace), "rev-list", "--count", "HEAD"]
        ).strip()
        == b"1"
    )


def test_supervisor_rechecks_workspace_before_resuming_child(authenticated, tmp_path, settings):
    from test_stage5_review import runner_for

    from agents_ide.engine.commands import command_environment
    from agents_ide.errors import AppError
    from agents_ide.worker.processes import ProcessRegistry, ProcessSupervisor

    run, factory = make_run(
        authenticated, tmp_path, graph=chain(check_node("pass")), execution_mode="real"
    )
    runner, job = runner_for(run, factory, settings)
    workspace = tmp_path / "workspace"
    workspace.rename(tmp_path / "original")
    workspace.mkdir()
    supervisor = ProcessSupervisor(
        factory, ProcessRegistry(), run["id"], runner.worker_id, job.generation
    )
    with pytest.raises(AppError, match="Workspace changed"):
        supervisor.start_stdio(
            [sys.executable, "-c", "from pathlib import Path; Path('effect').touch()"],
            workspace,
            command_environment(),
        )
    assert not (workspace / "effect").exists()
