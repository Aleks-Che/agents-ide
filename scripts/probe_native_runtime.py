"""Explicit native OpenCode acceptance against a synthetic loopback provider.

No account credentials, external model calls or project files are used. Exercises
the installed binary, production API/Runner, structured output, role isolation,
agent/LLM group fallback, single-agent dispatch and owned-process cleanup. This is
transport acceptance, not evidence about a paid model, write isolation or interrupted-tool recovery.
"""

import argparse
import contextlib
import json
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select

from agents_ide.adapters import opencode
from agents_ide.api.app import create_app
from agents_ide.config import Settings
from agents_ide.engine import opencode_runtime
from agents_ide.engine.queue import claim_next_job
from agents_ide.engine.runner import Runner
from agents_ide.errors import AppError
from agents_ide.persistence.models import (
    AgentSession,
    ProcessSupervision,
    StepAttempt,
    StepExecution,
)
from agents_ide.worker.processes import ProcessRegistry

MODEL = "acceptance/fixture"
SCHEMA = {
    "type": "object",
    "properties": {"verdict": {"const": "passed"}},
    "required": ["verdict"],
    "additionalProperties": False,
}


class Provider(BaseHTTPRequestHandler):
    calls = 0
    structured_calls = 0
    failures: list[str] = []
    tool_names: list[str] = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        type(self).calls += 1
        type(self).tool_names = [
            t.get("function", {}).get("name") for t in request.get("tools", [])
        ]
        if not self.path.endswith("/chat/completions") or self.calls > 12:
            type(self).failures.append("unexpected_request_or_call_limit")
            self.send_error(400)
            return
        if request.get("model") == "reject":
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            body = b'{"error":{"message":"Synthetic unavailable candidate"}}'
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        structured = next(
            (
                t["function"]["name"]
                for t in request.get("tools", [])
                if t.get("function", {}).get("name") == "StructuredOutput"
            ),
            None,
        )
        if structured:
            type(self).structured_calls += 1
        value = json.dumps({"verdict": "passed"})
        delta = (
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_acceptance",
                        "type": "function",
                        "function": {"name": structured, "arguments": value},
                    }
                ]
            }
            if structured
            else {"content": value}
        )
        finish = "tool_calls" if structured else "stop"
        common = {
            "id": "chatcmpl-acceptance",
            "model": "fixture",
            "created": int(time.time()),
        }
        if request.get("stream"):
            chunks = [
                {
                    **common,
                    "object": "chat.completion.chunk",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", **delta},
                            "finish_reason": None,
                        }
                    ],
                },
                {
                    **common,
                    "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15,
                    },
                },
            ]
            body = (
                "".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n"
            ).encode()
            content_type = "text/event-stream"
        else:
            body = json.dumps(
                {
                    **common,
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", **delta},
                            "finish_reason": finish,
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15,
                    },
                }
            ).encode()
            content_type = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        with contextlib.suppress(OSError):
            self.wfile.write(body)


def probe(executable: Path, *, zen_model: str | None = None, controls: bool = False) -> dict:
    selected_model = zen_model or MODEL
    handler = type(
        "IsolatedProvider",
        (Provider,),
        {"calls": 0, "structured_calls": 0, "failures": []},
    )
    provider = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=provider.serve_forever, daemon=True)
    thread.start()
    original = opencode_runtime.server_environment
    try:
        with tempfile.TemporaryDirectory(prefix="agents-ide-native-acceptance-") as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()

            def environment(password, username="opencode", *, permission_mode="no_tools"):
                env = original(password, username, permission_mode=permission_mode)
                env.update(
                    {
                        key: str(root / key.lower())
                        for key in (
                            "XDG_CONFIG_HOME",
                            "XDG_DATA_HOME",
                            "XDG_CACHE_HOME",
                        )
                    }
                )
                env["OPENCODE_DISABLE_MODELS_FETCH"] = "true"
                config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
                config.update(
                    enabled_providers=["acceptance"],
                    provider={
                        "acceptance": {
                            "npm": "@ai-sdk/openai-compatible",
                            "name": "Local acceptance",
                            "options": {
                                "baseURL": f"http://127.0.0.1:{provider.server_port}/v1",
                                "apiKey": "synthetic-unused",
                            },
                            "models": {
                                "fixture": {
                                    "name": "Fixture",
                                    "limit": {"context": 32768, "output": 4096},
                                }
                            },
                        }
                    },
                )
                if zen_model:
                    # Only the real native Zen provider may use its free tier.
                    # Pin auxiliary title/summary calls to the same free model.
                    config.update(
                        enabled_providers=["opencode"],
                        model=zen_model,
                        small_model=zen_model,
                        provider={"opencode": {"whitelist": [zen_model.split("/", 1)[1]]}},
                    )
                env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config)
                return env

            settings = Settings(data_dir=root / "data", port=18769, frontend_dir=root / "ui")
            app = create_app(settings)
            registry = ProcessRegistry()
            # A probe must be bounded even when production steps have no time limit.
            deadline = time.monotonic() + (300 if zen_model else 150)
            original_start = opencode_runtime.OpenCodeRuntime.start
            terminals = []
            busy = threading.Event()
            original_archive = opencode.archive_native

            def archive(emit, harness, kind, payload, session_id):
                if (
                    kind == "session.status"
                    and payload.get("properties", {}).get("status", {}).get("type") == "busy"
                ):
                    busy.set()
                if kind == "message.completed":
                    info = payload.get("info", {})
                    terminals.append(
                        {
                            "keys": list(info),
                            "finish": info.get("finish"),
                            "structured_output": info.get("structured_output"),
                            "structured": info.get("structured"),
                        }
                    )
                return original_archive(emit, harness, kind, payload, session_id)

            def start(**kwargs):
                kwargs["start_timeout"] = 30
                return original_start(**kwargs)

            with (
                patch.object(opencode_runtime, "server_environment", environment),
                patch.object(opencode_runtime.OpenCodeRuntime, "start", start),
                patch.object(opencode, "archive_native", archive),
                TestClient(app, base_url=settings.origin) as client,
            ):
                paired = client.post(
                    "/api/auth/pair",
                    json={"code": (settings.data_dir / "runtime/pair-code").read_text()},
                    headers={"Origin": settings.origin},
                )
                paired.raise_for_status()
                headers = {
                    "Origin": settings.origin,
                    "X-CSRF-Token": paired.json()["csrf_token"],
                }

                def post(path, body):
                    response = client.post("/api/" + path, json=body, headers=headers)
                    assert response.is_success, (path, response.json())
                    return response.json()

                project = post(
                    "projects",
                    {"name": "Native acceptance", "workspace_path": str(workspace)},
                )
                profile = post(
                    "harness_profiles",
                    {
                        "name": "OpenCode acceptance",
                        "harness_kind": "opencode",
                        "executable_path": str(executable),
                        "settings": {"permission_mode": "native"},
                    },
                )
                catalog = post(f"harness_profiles/{profile['id']}/test", {})
                assert catalog["status"] == "ok", catalog
                template = post("templates", {"name": "Native acceptance"})
                group = post(
                    "model_groups/agent",
                    {
                        "name": "heavy",
                        "members": [
                            {"harness_profile_id": profile["id"], "model_id": "acceptance/missing"},
                            {"harness_profile_id": profile["id"], "model_id": selected_model},
                        ],
                    },
                )
                connection = post(
                    "connections",
                    {
                        "name": "Local acceptance LLM",
                        "base_url": f"http://127.0.0.1:{provider.server_port}/v1",
                        "manual_models": ["reject", "fixture"],
                    },
                )
                llm_group = post(
                    "model_groups/llm",
                    {
                        "name": "flash",
                        "members": [
                            {"provider_connection_id": connection["id"], "model_id": model}
                            for model in ("reject", "fixture")
                        ],
                    },
                )
                nodes = [
                    {"id": "start", "type": "Start"},
                    *[
                        {
                            "id": role,
                            "type": "AgentTask",
                            "config": {
                                "role": role,
                                "prompt": (
                                    "This is a protocol acceptance test. "
                                    'Return exactly {"verdict":"passed"} via StructuredOutput. '
                                    "Do not inspect files, use other tools or add commentary."
                                ),
                                "response_format": "json",
                                "output_schema": SCHEMA,
                                "model_selection": {
                                    "kind": "direct",
                                    "harness_profile_id": profile["id"],
                                    "model_id": selected_model,
                                },
                            },
                        }
                        for role in ("implementer", "reviewer")
                    ],
                    {"id": "end", "type": "End"},
                ]
                if not controls:
                    nodes[1]["config"]["model_selection"] = {
                        "kind": "group",
                        "group_id": group["id"],
                    }
                nodes.insert(
                    2,
                    {
                        "id": "llm",
                        "type": "LLMRequest",
                        "config": {
                            "role": "planner",
                            "prompt": "Return the required verdict.",
                            "response_format": "json",
                            "output_schema": SCHEMA,
                            "model_selection": {"kind": "group", "group_id": llm_group["id"]},
                        },
                    },
                )
                version = post(
                    f"templates/{template['id']}/versions",
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
                    f"versions/{version['id']}/bindings",
                    {"project_id": project["id"], "name": "Native acceptance"},
                )

                def execute_owned(run_id):
                    job = claim_next_job(
                        app.state.session_factory, worker_id="acceptance", lease_seconds=300
                    )
                    assert job and job.run_id == run_id
                    runner = Runner(
                        session_factory=app.state.session_factory,
                        worker_id="acceptance",
                        generation=job.generation,
                        data_dir=settings.data_dir,
                        secret_store=app.state.secrets,
                        registry=registry,
                    )
                    original_check = runner._check_owned

                    def check():
                        original_check()
                        if time.monotonic() >= deadline:
                            raise AppError(
                                "probe_deadline", "Native acceptance deadline exceeded", 409
                            )

                    runner._check_owned = check
                    return runner.execute(run_id)

                def control(run_id, kind):
                    current = client.get(f"/api/runs/{run_id}").json()
                    return post(
                        f"runs/{run_id}/commands",
                        {
                            "command_id": kind,
                            "command_type": kind,
                            "expected_state_version": current["state_version"],
                        },
                    )

                states = []
                waiting_reasons = []
                control_results = []
                for mode in ("pause", "stop") if controls else ("pipeline", "single"):
                    run = post(
                        "runs",
                        {
                            "project_id": project["id"],
                            "binding_id": binding["id"],
                            "execution_mode": "real",
                            "idempotency_key": mode,
                            "message": "Synthetic native acceptance",
                            **(
                                {
                                    "single_agent": {
                                        "node_id": "implementer",
                                        "role": "implementer",
                                    }
                                }
                                if mode != "pipeline"
                                else {}
                            ),
                        },
                    )
                    done = threading.Event()
                    controller_errors = []
                    busy.clear()

                    def interrupt(
                        run_id=run["id"], kind=mode, finished=done, errors=controller_errors
                    ):
                        while not busy.wait(0.1):
                            if finished.is_set():
                                return
                        if finished.wait(1):
                            return
                        try:
                            control(run_id, kind)
                        except Exception as exc:
                            errors.append(type(exc).__name__)

                    controller = None
                    if mode in {"pause", "stop"}:
                        controller = threading.Thread(target=interrupt, daemon=True)
                        controller.start()
                    try:
                        result = execute_owned(run["id"])
                    except AppError as exc:
                        if exc.code != "probe_deadline":
                            raise
                        states.append("probe_deadline")
                        waiting_reasons.append(exc.code)
                        break
                    finally:
                        done.set()
                        if controller:
                            controller.join(timeout=5)
                    if mode in {"pause", "stop"}:
                        expected = "paused" if mode == "pause" else "stopped"
                        observed = {
                            "command": mode,
                            "initial_state": str(result.final_state),
                            "controller_errors": controller_errors,
                        }
                        control_results.append(observed)
                        if str(result.final_state) == expected and not controller_errors:
                            control(run["id"], "resume")
                            result = execute_owned(run["id"])
                            observed["resumed_state"] = str(result.final_state)
                            with app.state.session_factory() as session:
                                rows = list(
                                    session.scalars(
                                        select(AgentSession)
                                        .join(
                                            StepAttempt, StepAttempt.id == AgentSession.attempt_id
                                        )
                                        .join(
                                            StepExecution,
                                            StepExecution.id == StepAttempt.execution_id,
                                        )
                                        .where(StepExecution.run_id == run["id"])
                                        .order_by(AgentSession.started_at)
                                    )
                                )
                                observed["sessions"] = len(rows)
                                observed["session_reused"] = bool(rows) and (
                                    rows[0].external_session_id == rows[-1].external_session_id
                                )
                    states.append(str(result.final_state))
                    if result.waiting_reason:
                        waiting_reasons.append(result.waiting_reason.code)
                    if str(result.final_state) != "completed":
                        break
                with app.state.session_factory() as session:
                    sessions = list(session.scalars(select(AgentSession)))
                    processes = list(session.scalars(select(ProcessSupervision)))
                    attempts = list(session.scalars(select(StepAttempt)))

                    def fallback_succeeded(group_id):
                        return any(
                            a.status == "succeeded"
                            and json.loads(a.selection_json).get("group_id") == group_id
                            and json.loads(a.selection_json).get("reason") == "fallback"
                            for a in attempts
                        )

                    return {
                        "provider": "real native OpenCode Zen + synthetic HTTP LLM"
                        if zen_model
                        else "synthetic loopback Chat Completions",
                        "native_version": catalog["version"],
                        "native_credentials_used": False,
                        # Native internal model requests are not independently counted.
                        "external_model_calls": None if zen_model else 0,
                        "native_structured_results": sum(
                            terminal["structured"] is not None
                            or terminal["structured_output"] is not None
                            for terminal in terminals
                        ),
                        "local_provider_calls": handler.calls,
                        "structured_calls": handler.structured_calls,
                        "pipeline_state": states[0] if not controls else "not_run",
                        "single_agent_state": states[1]
                        if not controls and len(states) > 1
                        else "not_run",
                        "waiting_reasons": waiting_reasons,
                        "controls": control_results,
                        "distinct_sessions": len({s.external_session_id for s in sessions}),
                        "attempts": len(attempts),
                        "all_processes_stopped": bool(processes)
                        and all(p.state == "finished" for p in processes),
                        "provider_failures": handler.failures,
                        "agent_group_fallback": fallback_succeeded(group["id"]),
                        "llm_group_fallback": fallback_succeeded(llm_group["id"]),
                        "attempt_errors": sorted({a.error_code for a in attempts if a.error_code}),
                        "structured_result_fields": sorted(
                            {
                                key
                                for terminal in terminals
                                for key in ("structured", "structured_output")
                                if terminal[key] is not None
                            }
                        ),
                    }
    finally:
        provider.shutdown()
        provider.server_close()
        thread.join(timeout=2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opencode", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists() or not args.opencode.is_absolute() or not args.opencode.is_file():
        parser.error("Use an existing absolute native executable and a new report path")
    report = probe(args.opencode)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report))
    passed = (
        report["pipeline_state"] == report["single_agent_state"] == "completed"
        and report["all_processes_stopped"]
        and not report["provider_failures"]
        and report["agent_group_fallback"]
        and report["llm_group_fallback"]
        and report["distinct_sessions"] == 3
        and report["structured_calls"] == 3
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
