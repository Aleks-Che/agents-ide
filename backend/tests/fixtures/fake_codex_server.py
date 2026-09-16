"""Strict process fixture, checked against installed Codex 0.153.4 schemas.

No models or external services. Initialization, request IDs, event scoping,
async interrupt and server approval response shapes are enforced.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from pathlib import Path

from jsonschema import Draft7Validator

MODELS = ("gpt-5.6-sol", "gpt-5.5", "gpt-5.3-codex-spark")
SCHEMAS = json.loads(
    Path(__file__).with_name("codex-0.153.4-requests.json").read_text(encoding="utf-8")
)


class Handler:
    def __init__(self, stdin, stdout, stderr, cwd):
        self.stdin, self.stdout, self.stderr, self.cwd = stdin, stdout, stderr, cwd
        self.write_lock = threading.Lock()
        self.initialized = False
        self.acknowledged = False
        self.sessions = {}
        self.active = {}
        self.approvals = {}
        self.trace = os.environ.get("FAKE_CODEX_TRACE")
        self.store = os.environ.get("FAKE_CODEX_SESSIONS")
        if self.store and Path(self.store).exists():
            self.sessions = json.loads(Path(self.store).read_text())

    def send(self, message):
        with self.write_lock:
            self.stdout.write(json.dumps(message) + "\n")
            self.stdout.flush()

    def event(self, method, params):
        self.send({"method": method, "params": params})

    def record(self, message):
        if self.trace:
            with open(self.trace, "a", encoding="utf-8") as output:
                output.write(json.dumps({"message": message}) + "\n")

    def run(self):
        for line in self.stdin:
            message = json.loads(line)
            self.record(message)
            method, rid = message.get("method"), message.get("id")
            if method is None:
                if rid in self.approvals:
                    schema, event = self.approvals[rid]
                    Draft7Validator(SCHEMAS[schema]).validate(message.get("result"))
                    event.set()
                continue
            if method == "initialized":
                assert self.initialized and rid is None
                self.acknowledged = True
                continue
            assert isinstance(rid, (str, int)), "Methods require a JSON-RPC request id"
            try:
                Draft7Validator(SCHEMAS[method]).validate(message.get("params"))
                result = self.dispatch(method, message.get("params") or {})
                self.send({"id": rid, "result": result})
                if method == "turn/start":
                    threading.Thread(
                        target=self.turn,
                        args=(message["params"], result["turn"]["id"]),
                        daemon=True,
                    ).start()
            except (ValueError, KeyError) as exc:
                self.send({"id": rid, "error": {"code": -32600, "message": str(exc)}})

    def dispatch(self, method, params):
        if method == "initialize":
            if self.initialized or os.environ.get("FAKE_CODEX_INIT_ERROR"):
                raise ValueError("Initialize rejected")
            self.initialized = True
            return {"userAgent": "codex/0.153.4-fixture"}
        if not self.acknowledged:
            raise ValueError("Not initialized")
        if method == "model/list":
            if os.environ.get("FAKE_CODEX_CATALOG_ERROR"):
                raise ValueError("Catalog unavailable")
            cursor = params.get("cursor")
            return {
                "data": [{"id": m, "model": m} for m in (MODELS[1:] if cursor else MODELS[:1])],
                "nextCursor": None if cursor else "page2",
            }
        if method == "thread/start":
            tid = str(uuid.uuid4())
            self.sessions[tid] = {"id": tid, "cwd": params["cwd"], "turns": []}
            self.save()
            return {"thread": self.sessions[tid]}
        if method == "thread/resume":
            if os.environ.get("FAKE_CODEX_RESUME_ERROR"):
                raise ValueError("Temporary storage error")
            tid = params["threadId"]
            if tid not in self.sessions:
                raise ValueError("thread not found")
            return {"thread": self.sessions[tid]}
        if method == "turn/start":
            if os.environ.get("FAKE_CODEX_CLOSE_STREAM"):
                os._exit(0)
            tid = params["threadId"]
            if tid not in self.sessions:
                raise ValueError("thread not found")
            turn = str(uuid.uuid4())
            self.active[turn] = threading.Event()
            if os.environ.get("FAKE_CODEX_EARLY_EVENTS"):
                self.event(
                    "item/agentMessage/delta",
                    {"threadId": tid, "turnId": turn, "itemId": "early", "delta": "early"},
                )
            return {"turn": {"id": turn, "status": "inProgress", "items": [], "error": None}}
        if method == "turn/interrupt":
            self.active[params["turnId"]].set()
            return {}
        raise ValueError("Unknown method")

    def save(self):
        if self.store:
            Path(self.store).write_text(json.dumps(self.sessions))

    def turn(self, params, turn):
        tid = params["threadId"]
        scope = {"threadId": tid, "turnId": turn}
        if os.environ.get("FAKE_CODEX_STDERR_FLOOD"):
            self.stderr.write("private-debug\n" * 100000)
            self.stderr.flush()
        self.event("turn/started", {"threadId": tid, "turn": {"id": turn, "status": "inProgress"}})
        self.event(
            "item/agentMessage/delta",
            {**scope, "threadId": "foreign", "itemId": "foreign", "delta": "FOREIGN"},
        )
        self.event(
            "turn/completed", {"threadId": tid, "turn": {"id": "old-turn", "status": "completed"}}
        )
        if os.environ.get("FAKE_CODEX_PENDING_PERMISSION"):
            kind = os.environ.get("FAKE_CODEX_PERMISSION_KIND", "file")
            methods = {
                "file": "item/fileChange/requestApproval",
                "command": "item/commandExecution/requestApproval",
                "permissions": "item/permissions/requestApproval",
            }
            rid = "permission-1"
            event = threading.Event()
            self.approvals[rid] = (kind + "_approval", event)
            self.send(
                {"method": methods[kind], "id": rid, "params": {**scope, "itemId": "approval"}}
            )
            if not event.wait(5):
                raise ValueError("No valid approval response")
        prompt = params["input"][0]["text"]
        text = "hello from codex"
        if "force_decision=" in prompt:
            text = json.dumps({"verdict": "passed", "feedback": "fixture"})
        item = {"id": "answer", "type": "agentMessage", "text": text, "phase": "final_answer"}
        if not os.environ.get("FAKE_CODEX_FINAL_ONLY"):
            self.event("item/agentMessage/delta", {**scope, "itemId": "answer", "delta": text})
        self.event("item/completed", {**scope, "item": item})
        self.event(
            "item/completed",
            {
                **scope,
                "item": {
                    "id": "tool",
                    "type": "commandExecution",
                    "command": "echo fixture",
                    "status": "completed",
                },
            },
        )
        self.event(
            "thread/tokenUsage/updated",
            {
                **scope,
                "tokenUsage": {
                    "last": {
                        "totalTokens": 18,
                        "inputTokens": 11,
                        "outputTokens": 7,
                        "cachedInputTokens": 4,
                        "reasoningOutputTokens": 2,
                    },
                    "total": {"totalTokens": 99},
                },
            },
        )
        stopped = self.active[turn].wait(
            float(os.environ.get("FAKE_CODEX_RESPONSE_DELAY_SECONDS", "0"))
        )
        error = json.loads(os.environ.get("FAKE_CODEX_RESPONSE_ERROR_JSON", "null"))
        if os.environ.get("FAKE_CODEX_FORCE_PROVIDER_ERROR"):
            error = {"message": "private-provider-error", "codexErrorInfo": "other"}
        status = "interrupted" if stopped else "failed" if error else "completed"
        self.sessions[tid]["turns"].append({"id": turn, "status": status})
        self.save()
        self.event(
            "turn/completed",
            {"threadId": tid, "turn": {"id": turn, "status": status, "error": error}},
        )


if __name__ == "__main__":
    Handler(sys.stdin, sys.stdout, sys.stderr, os.getcwd()).run()
