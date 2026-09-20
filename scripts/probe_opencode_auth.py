"""Probe native OpenCode provider 401 with a loopback-only, rejecting provider."""

import argparse
import json
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agents_ide.adapters.base import AgentAdapterRequest
from agents_ide.adapters.opencode import OpenCodeAdapter
from agents_ide.engine import opencode_runtime
from agents_ide.worker.processes import process_state


def probe(executable: str) -> dict[str, object]:
    class Handler(BaseHTTPRequestHandler):
        calls = 0

        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            Handler.calls += 1
            body = json.dumps(
                {
                    "error": {
                        "type": "authentication_error",
                        "code": "invalid_api_key",
                        "message": "Synthetic key rejected",
                    }
                }
            ).encode()
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original = opencode_runtime.server_environment
    try:
        with tempfile.TemporaryDirectory(prefix="agents-ide-provider-auth-") as directory:
            root = Path(directory)

            def environment(password, username="opencode", *, permission_mode="no_tools"):
                env = original(password, username, permission_mode=permission_mode)
                env.update(
                    XDG_CONFIG_HOME=str(root / "config"),
                    XDG_DATA_HOME=str(root / "data"),
                    OPENCODE_DISABLE_MODELS_FETCH="true",
                )
                config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
                config.update(
                    enabled_providers=["openai"],
                    provider={
                        "openai": {
                            "options": {
                                "baseURL": f"http://127.0.0.1:{server.server_port}/v1",
                                "apiKey": "synthetic-unused-key",
                            }
                        }
                    },
                )
                env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config)
                return env

            opencode_runtime.server_environment = environment
            runtime = opencode_runtime.OpenCodeRuntime.start(
                executable=executable, workspace_path=root
            )
            try:
                adapter = OpenCodeAdapter(session=runtime.session())
                result = adapter.run(
                    AgentAdapterRequest(
                        role="probe",
                        model_id="openai/gpt-4.1",
                        prompt="Reply OK. No tools.",
                        context_package={},
                        workspace_path=str(root),
                        capabilities={},
                        params={},
                    )
                )
                processes = [
                    (process.pid, process.create_time()) for process in runtime.group.members()
                ]
                report = {
                    "version": runtime.server_version,
                    "provider": "local HTTP fixture returning 401",
                    "native_credentials_used": False,
                    "local_provider_calls": Handler.calls,
                    "external_provider_calls": 0,
                    "outcome": str(result.outcome),
                    "error_code": result.error.code if result.error else None,
                    "retry_safety": result.error.retry_safety if result.error else None,
                    "no_effect": result.no_effect,
                }
            finally:
                runtime.close()
            report["all_processes_stopped"] = all(
                process_state(pid, created) == "dead" for pid, created in processes
            )
            return report
    finally:
        opencode_runtime.server_environment = original
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output already exists")
    report = probe(args.executable)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report))
    return (
        0
        if report["error_code"] == "provider_unauthorized"
        and report["no_effect"]
        and report["all_processes_stopped"]
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
