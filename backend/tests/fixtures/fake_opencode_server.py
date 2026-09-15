"""Protocol fixture based on OpenCode 1.18.30 /doc, never a model client."""

from __future__ import annotations

import base64
import contextlib
import json
import os
import queue
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4


class Handler(BaseHTTPRequestHandler):
    sessions = {}
    subscribers = []
    aborts = set()
    password = ""
    trace_path = None
    persistence_path = None
    lock = threading.Lock()
    response_status = 200
    response_error = None
    response_size = 0
    delay = 0
    close_stream = False
    pending_permission = False
    requests = []
    catalog = [
        {
            "id": "anthropic",
            "models": {
                "claude-sonnet-4-20250514": {},
                "claude-haiku-4-20250514": {},
            },
        }
    ]

    def log_message(self, *args):
        pass

    def trace(self, kind, body=None):
        row = {
            "kind": kind,
            "body": body,
            "path": self.path,
            "directory": self.directory,
        }
        with self.lock:
            self.requests.append(row)
            if self.trace_path:
                with open(self.trace_path, "a", encoding="utf-8") as output:
                    output.write(json.dumps(row) + "\n")

    def _send_json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        with contextlib.suppress(OSError):
            self.wfile.write(body)

    def authorized(self):
        expected = "Basic " + base64.b64encode(f"opencode:{self.password}".encode()).decode()
        if self.password and self.headers.get("Authorization") != expected:
            self._send_json(401, {"error": "unauthorized"})
            return False
        return True

    def publish(self, event):
        for subscriber in self.subscribers[:]:
            subscriber.put(event)

    def do_GET(self):  # noqa: N802
        parsed = urlsplit(self.path)
        self.directory = parse_qs(parsed.query).get(
            "directory", [self.headers.get("x-opencode-directory")]
        )[0]
        self.path = parsed.path
        if not self.authorized():
            return
        if self.path == "/event":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            subscriber = queue.Queue()
            self.subscribers.append(subscriber)
            subscriber.put({"type": "server.connected", "properties": {}})
            try:
                while True:
                    try:
                        event = subscriber.get(timeout=0.1)
                        if event is None:
                            return
                        self.wfile.write(b"data: " + json.dumps(event).encode() + b"\n\n")
                    except queue.Empty:
                        self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
            except OSError:
                pass
            finally:
                self.subscribers.remove(subscriber)
            return
        if self.path == "/global/health":
            self._send_json(200, {"healthy": True, "version": "1.18.30-fixture"})
        elif self.path == "/path":
            self._send_json(200, {"directory": self.directory or str(Path.cwd())})
        elif self.path == "/config/providers":
            self._send_json(200, {"providers": self.catalog, "default": {}})
        elif self.path == "/config":
            self._send_json(200, {"mcp": {}, "permission": {"*": "deny"}})
        elif self.path == "/session/status":
            self._send_json(200, {})
        elif self.path.startswith("/session/"):
            data = self.sessions.get(self.path.split("/")[2])
            self._send_json(200 if data else 404, data or {"name": "NotFoundError"})
        else:
            self._send_json(404, {"name": "NotFoundError"})

    def do_POST(self):  # noqa: N802
        parsed = urlsplit(self.path)
        self.directory = parse_qs(parsed.query).get(
            "directory", [self.headers.get("x-opencode-directory")]
        )[0]
        self.path = parsed.path
        if not self.authorized():
            return
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        self.trace("post", body)
        if self.path == "/session":
            sid = "ses_" + uuid4().hex
            data = {
                "id": sid,
                "title": body.get("title"),
                "permission": body.get("permission"),
                "directory": self.directory,
            }
            self.sessions[sid] = data
            if self.persistence_path:
                Path(self.persistence_path).write_text(json.dumps(self.sessions), encoding="utf-8")
            self._send_json(200, data)
            return
        if self.path.endswith("/abort"):
            self.aborts.add(self.path.split("/")[2])
            self._send_json(200, True)
            return
        if self.path.startswith("/permission/"):
            self._send_json(200, True)
            return
        if not self.path.endswith("/message"):
            self._send_json(404, {})
            return
        sid = self.path.split("/")[2]
        if sid not in self.sessions:
            self._send_json(404, {"name": "NotFoundError"})
            return
        model = body.get("model")
        if (
            not isinstance(model, dict)
            or set(model) != {"providerID", "modelID"}
            or any(
                set(part) != {"type", "text"} or not isinstance(part["text"], str)
                for part in body.get("parts", [])
            )
        ):
            self._send_json(400, {"name": "InvalidRequestError"})
            return
        if model["modelID"] == "unavailable":
            self._send_json(401, {"name": "ProviderAuthError"})
            return
        prompt = body["parts"][0]["text"]
        self.publish(
            {
                "type": "message.part.delta",
                "properties": {"sessionID": "ses_foreign", "field": "text", "delta": "FOREIGN"},
            }
        )
        self.publish(
            {
                "type": "message.part.delta",
                "properties": {"sessionID": sid, "field": "text", "delta": "hello"},
            }
        )
        if self.pending_permission or prompt == "permission":
            self.publish(
                {
                    "type": "permission.asked",
                    "properties": {"sessionID": sid, "id": "per_test", "permission": "bash"},
                }
            )
        if self.close_stream:
            self.publish(None)
        deadline = time.monotonic() + (2 if "slow" in prompt else self.delay or 0.08)
        while time.monotonic() < deadline and sid not in self.aborts:
            time.sleep(0.02)
        text = "hello from opencode" + "x" * self.response_size
        for verdict in ("passed", "failed", "inconclusive"):
            if f"force_decision={verdict}" in prompt:
                text = json.dumps({"verdict": verdict, "feedback": "fixture"})
        info = {
            "id": "msg_" + uuid4().hex,
            "sessionID": sid,
            "role": "assistant",
            "finish": "stop",
            "tokens": {"input": 10, "output": 5, "reasoning": 0, "cache": {"read": 0, "write": 0}},
            "cost": 0,
        }
        if self.response_error or sid in self.aborts:
            info["error"] = self.response_error or {"name": "MessageAbortedError"}
        self._send_json(
            self.response_status, {"info": info, "parts": [{"type": "text", "text": text}]}
        )


def main():
    args = sys.argv[1:]
    port = (
        int(args[args.index("--port") + 1])
        if "--port" in args
        else int(os.environ["FAKE_OPENCODE_PORT"])
    )
    Handler.password = os.environ.get(
        "OPENCODE_SERVER_PASSWORD", os.environ.get("FAKE_OPENCODE_PASSWORD", "")
    )
    Handler.trace_path = os.environ.get("FIXTURE_TRACE")
    Handler.persistence_path = os.environ.get("FIXTURE_SESSIONS")
    if Handler.persistence_path and Path(Handler.persistence_path).exists():
        Handler.sessions = json.loads(Path(Handler.persistence_path).read_text())
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
