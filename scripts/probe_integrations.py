"""Explicit stage-0 smoke against installed harnesses, using a disposable Git repo.

Run with the backend environment. This can make model requests. The output contains
only allowlisted metadata, never raw account/provider responses, prompts or tokens.
Unobserved capabilities remain unverified; protocol acceptance is not model success.
"""

import argparse
import contextlib
import json
import os
import platform
import queue
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from agents_ide.worker.processes import ProcessGroup

CAPABILITIES = (
    "transport",
    "list_models",
    "create_session",
    "model_result",
    "stream_events",
    "resume_session",
    "interrupt",
    "permission_request",
    "auth_failure",
    "transport_recovery",
    "process_recovery",
    "write_isolation",
)


def empty_report(version):
    return {
        "version": version,
        "capabilities": {key: "unverified" for key in CAPABILITIES},
        "observations": [],
        "transport_gate": "no-go",
        "autonomous_write_gate": "no-go",
    }


def observe(report, name, success, evidence):
    report["capabilities"][name] = "supported" if success else "unverified"
    report["observations"].append({"capability": name, **evidence})


class CodexRpc:
    def __init__(self, executable, workspace, env=None):
        self.group = ProcessGroup()
        self.process = self.group.popen_stdio(
            [executable, "app-server", "--listen", "stdio://"], workspace, env
        )
        self.messages = queue.Queue()
        self.events = []
        self.request_id = 0
        self.completed_turns = {}

        def read():
            for line in self.process.stdout:
                with contextlib.suppress(ValueError):
                    self.messages.put(json.loads(line))
            self.messages.put({"transport_closed": True})

        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()

    def send(self, method, params, request_id=None):
        message = {"method": method, "params": params}
        if request_id is not None:
            message["id"] = request_id
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def receive(self, deadline):
        message = self.messages.get(timeout=max(0.01, deadline - time.monotonic()))
        if message.get("transport_closed"):
            raise EOFError("transport closed")
        if "method" in message:
            event = {"method": message["method"]}
            params = message.get("params", {})
            if isinstance(params.get("turn"), dict):
                event["turn_status"] = params["turn"].get("status")
                event["error_present"] = bool(params["turn"].get("error"))
                if message["method"] == "turn/completed":
                    self.completed_turns[params["turn"]["id"]] = params["turn"]
            if isinstance(params.get("item"), dict):
                event["item_type"] = params["item"].get("type")
            if "delta" in params:
                event["delta_present"] = bool(params["delta"])
            if self.events and {k: v for k, v in self.events[-1].items() if k != "count"} == event:
                self.events[-1]["count"] = self.events[-1].get("count", 1) + 1
            else:
                self.events.append(event)
        return message

    def call(self, method, params, timeout=30):
        self.request_id += 1
        request_id = self.request_id
        self.send(method, params, request_id)
        deadline = time.monotonic() + timeout
        while True:
            message = self.receive(deadline)
            if message.get("id") == request_id and "method" not in message:
                return message

    def wait_turn(self, turn_id, timeout=45):
        deadline = time.monotonic() + timeout
        while True:
            if turn_id in self.completed_turns:
                return self.completed_turns[turn_id]
            message = self.receive(deadline)
            if message.get("method") == "turn/completed":
                turn = message.get("params", {}).get("turn", {})
                if turn.get("id") == turn_id:
                    return turn

    def close(self):
        self.group.close()
        if self.process.poll() is None:
            self.process.terminate()
        self.process.wait(timeout=10)
        self.reader.join(timeout=2)
        self.process.stdin.close()
        self.process.stdout.close()


def probe_codex(executable, workspace, model):
    version = subprocess.run(
        [executable, "--version"], capture_output=True, text=True, timeout=10
    ).stdout.strip()
    report = empty_report(version)
    rpc = CodexRpc(executable, workspace)
    try:
        result = rpc.call(
            "initialize", {"clientInfo": {"name": "agents_ide_probe", "version": "0.1.0"}}
        )
        observe(
            report,
            "transport",
            "result" in result,
            {"method": "initialize", "accepted": "result" in result},
        )
        rpc.send("initialized", {})
        result = rpc.call("model/list", {})
        models = result.get("result", {}).get("data", [])
        report["models"] = [item["id"] for item in models]
        observe(report, "list_models", bool(models), {"method": "model/list", "count": len(models)})
        if not model:
            model = next((item["id"] for item in models if item.get("isDefault")), None)
        if not model:
            return report
        report["model"] = model
        result = rpc.call(
            "thread/start",
            {
                "model": model,
                "cwd": str(workspace),
                "sandbox": "read-only",
                "approvalPolicy": "never",
            },
        )
        thread = result.get("result", {}).get("thread", {})
        observe(
            report,
            "create_session",
            bool(thread.get("id")),
            {"method": "thread/start", "id_present": bool(thread.get("id"))},
        )
        if not thread.get("id"):
            report["start_error_code"] = result.get("error", {}).get("code")
            return report
        thread_id = thread["id"]
        result = rpc.call(
            "turn/start",
            {
                "threadId": thread_id,
                "model": model,
                "input": [
                    {
                        "type": "text",
                        "text": "Reply exactly PROBE_OK. "
                        "Do not use tools, read files, or change anything.",
                    }
                ],
            },
        )
        turn_id = result.get("result", {}).get("turn", {}).get("id")
        if turn_id:
            turn = rpc.wait_turn(turn_id)
            observe(
                report,
                "model_result",
                turn.get("status") == "completed",
                {
                    "method": "turn/completed",
                    "status": turn.get("status"),
                    "error_present": bool(turn.get("error")),
                },
            )
            error_text = json.dumps(turn.get("error", {})).lower()
            if any(word in error_text for word in ("auth", "401", "unauthorized")):
                observe(report, "auth_failure", True, {"mapped_reason": "auth_required"})
        result = rpc.call("thread/resume", {"threadId": thread_id, "cwd": str(workspace)})
        observe(
            report,
            "resume_session",
            result.get("result", {}).get("thread", {}).get("id") == thread_id,
            {
                "method": "thread/resume",
                "same_id": result.get("result", {}).get("thread", {}).get("id") == thread_id,
            },
        )
        if report["capabilities"]["model_result"] == "supported":
            result = rpc.call(
                "turn/start",
                {
                    "threadId": thread_id,
                    "model": model,
                    "input": [
                        {"type": "text", "text": "Count from 1 to 1000 in text. Do not use tools."}
                    ],
                },
            )
            turn_id = result.get("result", {}).get("turn", {}).get("id")
            if turn_id:
                rpc.call("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
                turn = rpc.wait_turn(turn_id, timeout=15)
                observe(
                    report,
                    "interrupt",
                    turn.get("status") == "interrupted",
                    {"method": "turn/interrupt", "terminal_status": turn.get("status")},
                )
        result = rpc.call("thread/read", {"threadId": "probe-invalid-id", "includeTurns": True})
        report["invalid_session_error"] = {
            "present": "error" in result,
            "code": result.get("error", {}).get("code"),
        }
    except Exception as error:
        report["probe_error"] = type(error).__name__
    finally:
        report["events"] = rpc.events
        observe(report, "stream_events", bool(rpc.events), {"observed_count": len(rpc.events)})
        rpc.close()
    if report["capabilities"]["model_result"] == "supported":
        report["transport_gate"] = "go"
    return report


def probe_opencode(executable, workspace, model):
    version = subprocess.run(
        [executable, "--version"], capture_output=True, text=True, timeout=10
    ).stdout.strip()
    report = empty_report(version)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    password = secrets.token_urlsafe(32)
    group = ProcessGroup()
    env = {
        **os.environ,
        "OPENCODE_SERVER_PASSWORD": password,
        "OPENCODE_DISABLE_AUTOUPDATE": "true",
        "OPENCODE_DISABLE_SHARE": "true",
    }
    group.start(
        [executable, "serve", "--pure", "--hostname", "127.0.0.1", "--port", str(port)],
        workspace,
        env,
    )
    with httpx.Client(
        base_url=f"http://127.0.0.1:{port}",
        auth=("opencode", password),
        timeout=45,
        trust_env=False,
    ) as client:
        try:
            deadline = time.monotonic() + 30
            while True:
                try:
                    response = client.get("/global/health", timeout=1)
                    if response.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if time.monotonic() >= deadline:
                    raise TimeoutError("server startup")
                time.sleep(0.2)
            observe(
                report,
                "transport",
                response.json().get("healthy") is True,
                {"endpoint": "/global/health", "status_code": response.status_code},
            )
            unauthorized = client.get("/global/health", auth=None)
            observe(
                report,
                "auth_failure",
                unauthorized.status_code == 401,
                {
                    "endpoint": "/global/health",
                    "status_code": unauthorized.status_code,
                    "scope": "local_server",
                },
            )
            spec = client.get("/doc").json()
            report["protocol_paths"] = [
                path
                for path in spec.get("paths", {})
                if path
                in (
                    "/session",
                    "/session/{sessionID}/message",
                    "/event",
                    "/provider",
                    "/session/{sessionID}/abort",
                )
            ]
            providers = client.get("/provider").json()
            report["connected_provider_count"] = len(providers.get("connected", []))
            report["model"] = model
            provider_id, model_id = model.split("/", 1)
            available = next(
                (p for p in providers.get("all", []) if p.get("id") == provider_id), {}
            )
            observe(
                report,
                "list_models",
                model_id in available.get("models", {}),
                {
                    "endpoint": "/provider",
                    "selected_model_present": model_id in available.get("models", {}),
                },
            )
            response = client.post(
                "/session",
                json={
                    "title": "Agents IDE capability probe",
                    "permission": [{"permission": "*", "pattern": "*", "action": "deny"}],
                },
            )
            session_id = response.json().get("id")
            observe(
                report,
                "create_session",
                bool(session_id),
                {"endpoint": "/session", "status_code": response.status_code},
            )
            if not session_id:
                return report
            with client.stream("GET", "/event", timeout=5) as events:
                for line in events.iter_lines():
                    if line.startswith("data:"):
                        payload = json.loads(line[5:])
                        report["events"] = [{"type": payload.get("type")}]
                        observe(
                            report,
                            "stream_events",
                            payload.get("type") == "server.connected",
                            {"endpoint": "/event", "first_type": payload.get("type")},
                        )
                        break
            response = client.post(
                f"/session/{session_id}/message",
                json={
                    "model": {"providerID": provider_id, "modelID": model_id},
                    "parts": [
                        {
                            "type": "text",
                            "text": "Reply exactly PROBE_OK. "
                            "Do not use tools, read files or change anything.",
                        }
                    ],
                },
            )
            result = response.json()
            info = result.get("info", {})
            text_parts = [
                part.get("text", "")
                for part in result.get("parts", [])
                if part.get("type") == "text"
            ]
            observe(
                report,
                "model_result",
                response.status_code == 200 and bool(text_parts) and not info.get("error"),
                {
                    "endpoint": "/session/:id/message",
                    "status_code": response.status_code,
                    "text_present": bool(text_parts),
                    "error_type": info.get("error", {}).get("name"),
                },
            )
            same = client.get(f"/session/{session_id}").json().get("id") == session_id
            # Readback alone is intentionally not marked as successful continuation.
            report["session_readback_same_id"] = same
            if report["capabilities"]["model_result"] == "supported":
                response = client.post(
                    f"/session/{session_id}/prompt_async",
                    json={
                        "model": {"providerID": provider_id, "modelID": model_id},
                        "parts": [
                            {
                                "type": "text",
                                "text": "Count from 1 to 1000 in text. Do not use tools.",
                            }
                        ],
                    },
                )
                report["continuation_accepted"] = response.status_code == 204
                response = client.post(f"/session/{session_id}/abort")
                report["abort_accepted"] = response.status_code == 200 and response.json() is True
                states = client.get("/session/status").json()
                idle = session_id not in states or states[session_id].get("type") == "idle"
                observe(
                    report,
                    "interrupt",
                    report["abort_accepted"] and idle,
                    {"endpoint": "/session/:id/abort", "idle_after_abort": idle},
                )
            client.delete(f"/session/{session_id}")
        except Exception as error:
            report["probe_error"] = type(error).__name__
        finally:
            group.close()
    if report["capabilities"]["model_result"] == "supported":
        report["transport_gate"] = "go"
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--codex", default=shutil.which("codex"))
    parser.add_argument("--codex-model")
    parser.add_argument("--opencode", required=True)
    parser.add_argument("--opencode-model", default="minimax-coding-plan/MiniMax-M3")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output already exists; use a new probe filename")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="agents-ide-probe-") as temporary:
        workspace = Path(temporary)
        subprocess.run(["git", "init", str(workspace)], check=True, capture_output=True)
        report = {
            "observed_at": datetime.now(UTC).isoformat(),
            "platform": platform.platform(),
            "python": platform.python_version(),
            "fixture_version": 1,
            "codex": probe_codex(args.codex, workspace, args.codex_model),
        }
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        report["opencode"] = probe_opencode(args.opencode, workspace, args.opencode_model)
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Saved sanitized capability evidence: {args.output}")


if __name__ == "__main__":
    main()
