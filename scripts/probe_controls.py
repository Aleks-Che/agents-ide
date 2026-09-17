"""Explicit Windows harness permission/auth/recovery probes. May consume model quota.

Uses only temporary Git workspaces and synthetic content. Never grants an approval.
It saves only allowlisted metadata; authentication configuration is not copied.
"""

import argparse
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from agents_ide.worker.processes import ProcessGroup
from probe_integrations import CodexRpc


def initialize(rpc):
    result = rpc.call(
        "initialize", {"clientInfo": {"name": "agents_ide_probe", "version": "0.1.0"}}
    )
    if "error" in result:
        raise RuntimeError("initialization rejected")
    rpc.send("initialized", {})


def start_turn(rpc, thread_id, model, prompt):
    result = rpc.call(
        "turn/start",
        {
            "threadId": thread_id,
            "model": model,
            "input": [{"type": "text", "text": prompt}],
        },
    )
    return result.get("result", {}).get("turn", {}).get("id")


def codex_controls(executable, workspace, model):
    report = {
        "model": model,
        "permission": "unverified",
        "auth_failure": "unverified",
        "completed_session_recovery": "unverified",
        "active_turn_recovery": "unverified",
        "interrupt": "unverified",
        "write_isolation": "unverified",
    }
    rpc = CodexRpc(executable, workspace)
    thread_id = None
    try:
        initialize(rpc)
        bad = rpc.call("thread/read", {"threadId": "probe-invalid-id", "includeTurns": True})
        report["invalid_session_error"] = bad.get("error", {}).get("code")
        started = rpc.call(
            "thread/start",
            {
                "model": model,
                "cwd": str(workspace),
                "sandbox": "read-only",
                "approvalPolicy": "on-request",
            },
        )
        thread_id = started.get("result", {}).get("thread", {}).get("id")
        if not thread_id:
            return report
        turn_id = start_turn(
            rpc,
            thread_id,
            model,
            "This is a disposable integration test. Run a command to write probe.txt containing x "
            "in this repository. Request permission escalation because the sandbox is read-only. "
            "Do not access other paths or run other commands. If permission is declined, stop.",
        )
        deadline = time.monotonic() + 50
        while time.monotonic() < deadline:
            message = rpc.receive(deadline)
            if "id" in message and "method" in message:
                method = message["method"]
                if (
                    method == "item/commandExecution/requestApproval"
                    or method == "item/fileChange/requestApproval"
                ):
                    reply = {"decision": "decline"}
                    report["permission"] = "supported"
                    report["approval_method"] = method
                    report["reply"] = "decline"
                else:
                    # Do not guess an approval schema for unknown server requests.
                    report["unhandled_request_method"] = method
                    break
                rpc.process.stdin.write(json.dumps({"id": message["id"], "result": reply}) + "\n")
                rpc.process.stdin.flush()
            if message.get("method") == "turn/completed":
                report["permission_turn_status"] = (
                    message.get("params", {}).get("turn", {}).get("status")
                )
                break
        report["file_written_after_decline"] = (workspace / "probe.txt").exists()
        result = rpc.call("thread/read", {"threadId": thread_id, "includeTurns": True})
        report["persisted_turn_count"] = len(
            result.get("result", {}).get("thread", {}).get("turns", [])
        )
    except Exception as error:
        report["permission_probe_error"] = type(error).__name__
    finally:
        rpc.close()
    if thread_id:
        rpc = CodexRpc(executable, workspace)
        try:
            initialize(rpc)
            resumed = rpc.call("thread/resume", {"threadId": thread_id, "cwd": str(workspace)})
            same = resumed.get("result", {}).get("thread", {}).get("id") == thread_id
            turns = resumed.get("result", {}).get("thread", {}).get("turns", [])
            report["resumed_same_id"] = same
            report["recovered_turn_count"] = len(turns)
            if same and turns and report.get("permission_turn_status") == "completed":
                report["completed_session_recovery"] = "supported"
            # Interrupt after observable progress, not just after request acceptance.
            turn_id = start_turn(
                rpc,
                thread_id,
                model,
                "Count from 1 to 1000 in text, with no tools or file access.",
            )
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                event = rpc.receive(deadline)
                if event.get("method") == "item/agentMessage/delta":
                    break
            reply = rpc.call(
                "turn/interrupt", {"threadId": thread_id, "turnId": turn_id}, timeout=15
            )
            report["interrupt_acknowledged"] = "result" in reply
            turn = rpc.wait_turn(turn_id, timeout=15)
            report["interrupted_status"] = turn.get("status")
            if turn.get("status") == "interrupted":
                report["interrupt"] = "supported"
        except Exception as error:
            report["recovery_probe_error"] = type(error).__name__
        finally:
            rpc.close()
    unauth_home = workspace / "unauthenticated-home"
    unauth_home.mkdir()
    rpc = CodexRpc(executable, workspace, {**os.environ, "CODEX_HOME": str(unauth_home)})
    try:
        initialize(rpc)
        started = rpc.call(
            "thread/start",
            {
                "model": model,
                "cwd": str(workspace),
                "sandbox": "read-only",
                "approvalPolicy": "never",
            },
        )
        error = started.get("error")
        if not error:
            thread = started.get("result", {}).get("thread", {}).get("id")
            result = rpc.call(
                "turn/start",
                {
                    "threadId": thread,
                    "model": model,
                    "input": [{"type": "text", "text": "Reply PROBE_OK without tools."}],
                },
            )
            error = result.get("error")
            turn_id = result.get("result", {}).get("turn", {}).get("id")
            if turn_id:
                error = rpc.wait_turn(turn_id, timeout=30).get("error")
        raw = json.dumps(error).lower()
        report["auth_error_present"] = bool(error)
        if any(word in raw for word in ("auth", "401", "unauthorized", "login", "api key")):
            report["auth_failure"] = "supported"
            report["auth_mapped_reason"] = "auth_required"
    except Exception as error:
        report["auth_probe_error"] = type(error).__name__
    finally:
        rpc.close()
    return report


def opencode_server(executable, workspace, password):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    group = ProcessGroup()
    group.start(
        [executable, "serve", "--pure", "--hostname", "127.0.0.1", "--port", str(port)],
        workspace,
        {
            **os.environ,
            "OPENCODE_SERVER_PASSWORD": password,
            "OPENCODE_DISABLE_SHARE": "true",
            "OPENCODE_DISABLE_AUTOUPDATE": "true",
        },
    )
    client = httpx.Client(
        base_url=f"http://127.0.0.1:{port}",
        auth=("opencode", password),
        timeout=45,
        trust_env=False,
    )
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                if client.get("/global/health", timeout=1).status_code == 200:
                    return group, client
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        raise TimeoutError("server startup")
    except BaseException:
        client.close()
        group.close()
        raise


def opencode_controls(executable, workspace, model):
    import secrets

    report = {
        "model": model,
        "permission": "unverified",
        "resume_session": "unverified",
        "completed_session_recovery": "unverified",
        "active_turn_recovery": "unverified",
        "write_isolation": "unverified",
    }
    password = secrets.token_urlsafe(32)
    group, client = opencode_server(executable, workspace, password)
    provider, model_id = model.split("/", 1)
    model_config = {"providerID": provider, "modelID": model_id}
    session = None
    try:
        spec = client.get("/doc").json()
        report["permission_paths"] = [
            path for path in spec.get("paths", {}) if "permission" in path
        ]
        session = client.post(
            "/session",
            json={
                "title": "Permission probe",
                "permission": [
                    {"permission": "*", "pattern": "*", "action": "deny"},
                    {"permission": "bash", "pattern": "*", "action": "ask"},
                ],
            },
        ).json()["id"]
        client.post(
            f"/session/{session}/prompt_async",
            json={
                "model": model_config,
                "parts": [
                    {
                        "type": "text",
                        "text": "Run the bash command echo PROBE_PERMISSION. "
                        "Do not access files or run other commands. "
                        "If permission is rejected, stop.",
                    }
                ],
            },
        )
        deadline = time.monotonic() + 50
        while time.monotonic() < deadline:
            pending = client.get("/permission").json()
            if isinstance(pending, list):
                request = next((p for p in pending if p.get("sessionID") == session), None)
                if request:
                    reply = client.post(
                        f"/permission/{request['id']}/reply", json={"reply": "reject"}
                    )
                    report["permission"] = "supported" if reply.status_code == 200 else "unverified"
                    report["permission_kind"] = request.get("permission")
                    report["reply_status"] = reply.status_code
                    break
            time.sleep(0.3)
        client.post(f"/session/{session}/abort")
        client.delete(f"/session/{session}")
        session = client.post(
            "/session",
            json={
                "title": "Recovery probe",
                "permission": [
                    {"permission": "*", "pattern": "*", "action": "deny"},
                ],
            },
        ).json()["id"]
        reply = client.post(
            f"/session/{session}/message",
            json={
                "model": model_config,
                "parts": [{"type": "text", "text": "Reply PROBE_ONE without tools."}],
            },
        ).json()
        report["first_response_ok"] = bool(reply.get("parts")) and not reply.get("info", {}).get(
            "error"
        )
    except Exception as error:
        report["permission_probe_error"] = type(error).__name__
    finally:
        client.close()
        group.close()
    if session:
        group, client = opencode_server(executable, workspace, password)
        try:
            report["same_id_after_restart"] = (
                client.get(f"/session/{session}").json().get("id") == session
            )
            history = client.get(f"/session/{session}/message").json()
            report["history_message_count"] = len(history) if isinstance(history, list) else 0
            if report["same_id_after_restart"] and report["history_message_count"]:
                report["completed_session_recovery"] = "supported"
            reply = client.post(
                f"/session/{session}/message",
                json={
                    "model": model_config,
                    "parts": [{"type": "text", "text": "Reply PROBE_TWO without tools."}],
                },
            ).json()
            report["second_response_ok"] = bool(reply.get("parts")) and not reply.get(
                "info", {}
            ).get("error")
            if report["second_response_ok"] and report["same_id_after_restart"]:
                report["resume_session"] = "supported"
            client.delete(f"/session/{session}")
        except Exception as error:
            report["recovery_probe_error"] = type(error).__name__
        finally:
            client.close()
            group.close()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opencode", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Use a new output file")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="agents-ide-controls-") as temp:
        workspace = Path(temp)
        subprocess.run(["git", "init", str(workspace)], check=True, capture_output=True)
        report = {
            "fixture_version": 1,
            "observed_at": datetime.now(UTC).isoformat(),
            "codex": codex_controls(shutil.which("codex"), workspace, "gpt-5.6-sol"),
        }
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        report["opencode"] = opencode_controls(
            args.opencode, workspace, "minimax-coding-plan/MiniMax-M3"
        )
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(f"Saved control observations: {args.output}")


if __name__ == "__main__":
    main()
