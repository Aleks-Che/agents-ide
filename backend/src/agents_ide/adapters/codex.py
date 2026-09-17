"""Codex App Server transport (schema: codex-cli 0.153.4).

One reader and one dispatch owner per connection; writes remain available for
interrupts. Native identifiers are opaque, not synthetic thread_/turn_ prefixes.
"""

from __future__ import annotations

import contextlib
import json
import queue
import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agents_ide.adapters.base import (
    AdapterError,
    AgentAdapter,
    AgentAdapterRequest,
    AgentResult,
    ExternalOutcome,
)
from agents_ide.adapters.model_catalog import CatalogModels, codex_metadata, parameters_for
from agents_ide.adapters.native_events import archive_native
from agents_ide.errors import AppError

_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


class CodexConfigurationError(AppError):
    def __init__(self, message: str) -> None:
        super().__init__("configuration_invalid", message, status=422)


@dataclass(frozen=True)
class CodexCapabilities:
    transport: str = "stdio"
    stream_events: bool = True
    resume_thread: bool = True
    native_interrupt: bool = True
    permissions: bool = False
    structured_output: bool = False
    list_models: bool = True
    autonomous_write: bool = False


def capabilities_for_kind() -> CodexCapabilities:
    return CodexCapabilities()


def validate_thread_id(value: Any) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("Invalid Codex identifier")
    return value


validate_turn_id = validate_thread_id


@dataclass
class CodexStream:
    process: subprocess.Popen[str]
    queue: queue.Queue[dict[str, Any] | None]
    lock: Any = field(default_factory=threading.RLock)
    closed: bool = False
    initialized: bool = False
    group: Any = None
    _write_lock: Any = field(default_factory=threading.Lock)
    _readers: list[threading.Thread] = field(default_factory=list)
    _failed: threading.Event = field(default_factory=threading.Event)
    _sequence: int = 0

    @classmethod
    def from_process(cls, process: subprocess.Popen[str]) -> CodexStream:
        if process.stdout is None or process.stdin is None:
            raise OSError("Codex stdio is not piped")
        stream = cls(process, queue.Queue(maxsize=128))

        def enqueue(message: dict[str, Any] | None) -> None:
            # Backpressure instead of failing a healthy agent on a burst of events.
            while not stream.closed:
                try:
                    stream.queue.put(message, timeout=0.1)
                    return
                except queue.Full:
                    continue

        def read_stdout() -> None:
            assert process.stdout is not None
            try:
                while not stream.closed:
                    line = process.stdout.readline()
                    if not line:
                        enqueue(None)
                        return
                    message = json.loads(line)
                    if not isinstance(message, dict):
                        raise ValueError("Invalid Codex envelope")
                    enqueue(message)
            except (OSError, ValueError, queue.Full):
                stream._failed.set()

        def drain_stderr() -> None:
            if process.stderr is not None:
                with contextlib.suppress(OSError, ValueError):
                    while process.stderr.read(4096):
                        pass  # Never retain credentials or unbounded diagnostics.

        for fn in (read_stdout, drain_stderr):
            reader = threading.Thread(target=fn, daemon=True)
            stream._readers.append(reader)
            reader.start()
        return stream

    @classmethod
    def open(cls, argv: list[str], cwd: str, env: dict[str, str]) -> CodexStream:
        from agents_ide.worker.processes import ProcessGroup

        group = ProcessGroup()
        try:
            process = group.popen_stdio(argv, Path(cwd), env)
            stream = cls.from_process(process)
            stream.group = group
            return stream
        except BaseException:
            group.close()
            raise

    def is_alive(self) -> bool:
        return not self.closed and not self._failed.is_set() and self.process.poll() is None

    def next_id(self) -> int:
        with self._write_lock:
            self._sequence += 1
            return self._sequence

    def send(self, message: dict[str, Any]) -> None:
        encoded = json.dumps(message, ensure_ascii=False) + "\n"
        with self._write_lock:
            if not self.is_alive() or self.process.stdin is None:
                raise OSError("Codex transport closed")
            try:
                self.process.stdin.write(encoded)
                self.process.stdin.flush()
            except (OSError, ValueError) as exc:
                raise OSError("Codex transport closed") from exc

    def receive(self, timeout: float) -> dict[str, Any]:
        if self._failed.is_set():
            raise OSError("Invalid or excessive Codex output")
        message = self.queue.get(timeout=timeout)
        if message is None:
            raise OSError("Codex transport closed")
        return message

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        # Stop the tree before closing buffered pipes (another writer may block).
        if self.group is not None:
            self.group.close()
        if self.process.poll() is None:
            with contextlib.suppress(OSError):
                self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        for reader in self._readers:
            reader.join(timeout=1)
        for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
            if pipe is not None:
                with contextlib.suppress(OSError, ValueError):
                    pipe.close()


@dataclass(frozen=True)
class RunSession:
    workspace_path: str
    settings: dict[str, Any]
    server_version: str | None


@dataclass(frozen=True)
class SessionBinding:
    thread_id: str = ""
    resume_count: int = 0


def _decline(stream: CodexStream, message: dict[str, Any]) -> bool:
    """Answer server requests with the matching response schema; never approve."""
    if "id" not in message or "method" not in message:
        return False
    method = message["method"]
    if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
        result: dict[str, Any] = {"decision": "decline"}
    elif method == "item/permissions/requestApproval":
        result = {"permissions": {}, "scope": "turn"}
    elif method == "item/tool/requestUserInput":
        result = {"answers": {}}
    elif method == "mcpServer/elicitation/request":
        result = {"action": "decline", "content": None, "_meta": None}
    else:
        stream.send(
            {
                "id": message["id"],
                "error": {"code": -32601, "message": "Unsupported server request"},
            }
        )
        return True
    stream.send({"id": message["id"], "result": result})
    return True


class CodexAdapter(AgentAdapter):
    name = "codex"

    def __init__(
        self,
        *,
        stream: CodexStream,
        session: RunSession,
        request_timeout: float = 30.0,
        turn_timeout: float | None = None,
    ) -> None:
        self._stream = stream
        self.session = session
        self.server_version = session.server_version
        self.request_timeout = request_timeout
        self.turn_timeout = turn_timeout
        self._binding = SessionBinding()
        self._turn_id: str | None = None

    @property
    def external_session_id(self) -> str | None:
        return self._binding.thread_id or None

    def _request_response(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float,
        check: Callable[[], None] | None = None,
        notifications: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        with self._stream.lock:
            if check:
                check()
            request_id = self._stream.next_id()
            self._stream.send({"id": request_id, "method": method, "params": params})
            deadline = time.monotonic() + timeout
            while True:
                if check:
                    check()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise OSError("Codex request deadline expired")
                try:
                    message = self._stream.receive(min(0.1, remaining))
                except queue.Empty:
                    continue
                # Server request IDs may collide with client IDs.
                if "method" not in message and message.get("id") == request_id:
                    return message
                if notifications is not None:
                    if message.get("method") not in {
                        "item/tool/requestUserInput",
                        "item/commandExecution/requestApproval",
                        "item/fileChange/requestApproval",
                    } and _decline(self._stream, message):
                        message["_answered"] = True
                    notifications.append(message)
                    if len(notifications) > 128:
                        raise OSError("Too many pre-response notifications")
                else:
                    _decline(self._stream, message)

    def _send_notification(self, method: str, params: dict[str, Any]) -> None:
        self._stream.send({"method": method, "params": params})

    def initialize(
        self, *, check: Callable[[], None] | None = None, timeout: float | None = None
    ) -> None:
        with self._stream.lock:
            if self._stream.initialized:
                return
            response = self._request_response(
                "initialize",
                {"clientInfo": {"name": "agents_ide", "version": "0.1.0"}},
                timeout=timeout or self.request_timeout,
                check=check,
            )
            if not isinstance(response.get("result"), dict) or response.get("error"):
                raise OSError("Codex initialize rejected")
            self._send_notification("initialized", {})
            self._stream.initialized = True

    def list_models(self, *, check: Callable[[], None] | None = None) -> tuple[str, ...]:
        self.initialize(check=check)
        models: dict[str, dict[str, Any]] = {}
        cursor = None
        seen: set[str] = set()
        for _ in range(100):
            response = self._request_response(
                "model/list",
                {"cursor": cursor, "limit": 100},
                timeout=self.request_timeout,
                check=check,
            )
            result = response.get("result")
            if (
                response.get("error")
                or not isinstance(result, dict)
                or not isinstance(result.get("data"), list)
            ):
                raise OSError("Codex model catalog rejected")
            for item in result["data"]:
                if (
                    not isinstance(item, dict)
                    or not isinstance(item.get("id"), str)
                    or not 1 <= len(item["id"]) <= 256
                ):
                    raise OSError("Invalid Codex model catalog")
                metadata = codex_metadata(item)
                if item["id"] in models and metadata != models[item["id"]]:
                    raise OSError("Conflicting Codex model metadata")
                models[item["id"]] = metadata
            cursor = result.get("nextCursor")
            if cursor is None:
                return CatalogModels(models)
            if not isinstance(cursor, str) or cursor in seen:
                raise OSError("Invalid Codex catalog cursor")
            seen.add(cursor)
        raise OSError("Codex catalog exceeds page limit")

    def interrupt(self, session_id: str) -> None:
        if session_id != self.external_session_id or self._turn_id is None:
            return
        with contextlib.suppress(OSError):
            self._stream.send(
                {
                    "id": self._stream.next_id(),
                    "method": "turn/interrupt",
                    "params": {"threadId": session_id, "turnId": self._turn_id},
                }
            )

    def _options(self, request: AgentAdapterRequest) -> dict[str, Any]:
        settings = self.session.settings
        from agents_ide.domain.harness_settings import approval_policy
        from agents_ide.engine.codex_runtime import validate_settings

        try:
            validate_settings(settings)
            parameters_for(
                "codex", request.model_id, request.params, settings.get("model_metadata", {})
            )
        except AppError as exc:
            raise CodexConfigurationError(exc.message) from None
        options: dict[str, Any] = {
            "model": request.model_id,
            "cwd": self.session.workspace_path,
            "sandbox": {
                "read_only": "read-only",
                "workspace_write": "workspace-write",
                "full_access": "danger-full-access",
            }[settings["permission_mode"]],
            "approvalPolicy": approval_policy(settings),
        }
        if settings.get("isolated_config"):
            options.pop("sandbox")
            options["config"] = settings["isolated_config"]
            options["ephemeral"] = True
        return options

    def run(self, request: AgentAdapterRequest) -> AgentResult:
        with self._stream.lock:
            return self._run(request)

    def _run(self, request: AgentAdapterRequest) -> AgentResult:
        started = time.monotonic()
        deadline = request.deadline_at or (
            time.time() + self.turn_timeout if self.turn_timeout is not None else float("inf")
        )
        sent = False
        terminal = False
        self._turn_id = None

        def check() -> None:
            if request.check_owned:
                request.check_owned()
            if (request.stop_event and request.stop_event.is_set()) or time.time() >= deadline:
                raise InterruptedError

        def emit(event_type: str, **payload: Any) -> None:
            if request.emit_event:
                request.emit_event(event_type, {"session_id": self.external_session_id, **payload})

        def failure(
            code: str, outcome: ExternalOutcome = ExternalOutcome.UNKNOWN, *, safe: bool = False
        ) -> AgentResult:
            return AgentResult(
                outcome,
                "",
                None,
                None,
                error=AdapterError(code, code, "safe" if safe else "unknown"),
                no_effect=safe,
                elapsed_seconds=time.monotonic() - started,
            )

        try:
            check()
            options = self._options(request)
            self.initialize(check=check)
            tid = request.resume_session_id or self.external_session_id
            resumed = bool(tid)
            if tid:
                validate_thread_id(tid)
                response = self._request_response(
                    "thread/resume",
                    {**options, "threadId": tid},
                    timeout=self.request_timeout,
                    check=check,
                )
                error = response.get("error")
                if error:
                    # Only a definite missing thread permits a fresh session.
                    error_message = (
                        str(error.get("message", "")).lower() if isinstance(error, dict) else ""
                    )
                    if isinstance(error, dict) and (
                        error.get("code") == "thread_not_found"
                        or (
                            error.get("code") == -32600
                            and (
                                "thread not found" in error_message
                                or "no rollout found" in error_message
                            )
                        )
                    ):
                        emit("agent.session_invalidated", reason="thread_not_found")
                        tid = None
                    else:
                        return failure(
                            "session_resume_failed", ExternalOutcome.UNAVAILABLE, safe=True
                        )
            if not tid:
                resumed = False
                response = self._request_response(
                    "thread/start", options, timeout=self.request_timeout, check=check
                )
                if response.get("error"):
                    return failure("thread_start_failed", ExternalOutcome.UNAVAILABLE, safe=True)
            thread = response["result"]["thread"]
            if self.session.settings.get("isolated_config"):
                from agents_ide.security.codex_policy import PROFILE

                actual = response["result"]
                if (
                    actual.get("activePermissionProfile", {}).get("id") != PROFILE
                    or actual.get("approvalPolicy") != "never"
                    or actual.get("sandbox", {}).get("networkAccess") is not False
                ):
                    return failure(
                        "sandbox_policy_mismatch", ExternalOutcome.UNAVAILABLE, safe=True
                    )
            returned_id = validate_thread_id(thread["id"])
            if tid and returned_id != tid:
                return failure("session_identity_mismatch")
            if any(t.get("status") == "inProgress" for t in thread.get("turns", [])):
                return failure("session_has_active_turn")
            self._binding = SessionBinding(
                returned_id, self._binding.resume_count + 1 if resumed else 0
            )
            archive_native(
                request.emit_event,
                "codex",
                "thread/resumed" if resumed else "thread/started",
                thread,
                returned_id,
            )
            emit(
                "agent.session_resumed" if resumed else "agent.session_created",
                server_version=self.session.server_version,
                resume_count=self._binding.resume_count,
                permission_mode=self.session.settings["permission_mode"],
            )
            check()
            turn_params: dict[str, Any] = {
                "threadId": returned_id,
                "model": request.model_id,
                "cwd": self.session.workspace_path,
                "approvalPolicy": options["approvalPolicy"],
                "sandboxPolicy": {
                    "type": {
                        "read_only": "readOnly",
                        "workspace_write": "workspaceWrite",
                        "full_access": "dangerFullAccess",
                    }[self.session.settings["permission_mode"]],
                    **(
                        {"writableRoots": [self.session.workspace_path], "networkAccess": False}
                        if self.session.settings["permission_mode"] == "workspace_write"
                        else {}
                    ),
                },
                "input": [
                    {"type": "text", "text": request.prompt},
                    {
                        "type": "text",
                        "text": "Context and evidence (JSON):\n"
                        + json.dumps(request.context_package, ensure_ascii=False),
                    },
                ],
            }
            if request.feedback:
                turn_params["input"].append(
                    {"type": "text", "text": "Feedback:\n" + request.feedback}
                )
            if self.session.settings.get("isolated_config"):
                turn_params.pop("sandboxPolicy")
            if isinstance(request.capabilities.get("output_schema"), dict):
                turn_params["outputSchema"] = request.capabilities["output_schema"]
            turn_params.update(
                parameters_for(
                    "codex",
                    request.model_id,
                    request.params,
                    self.session.settings.get("model_metadata", {}),
                )
            )
            pending: list[dict[str, Any]] = []
            sent = True  # Any loss during dispatch is ambiguous until proven rejected.
            response = self._request_response(
                "turn/start",
                turn_params,
                timeout=self.request_timeout,
                check=check,
                notifications=pending,
            )
            if response.get("error"):
                error = response["error"]
                if isinstance(error, dict) and error.get("code") in {-32600, -32601, -32602}:
                    return failure(
                        "configuration_invalid", ExternalOutcome.CONFIRMED_FAILURE, safe=True
                    )
                return failure("turn_start_failed")
            self._turn_id = validate_turn_id(response["result"]["turn"]["id"])
            emit("agent.turn_started", turn_id=self._turn_id)
            emit("attempt.progress", message_id=self._turn_id, role="assistant")
            texts: dict[str, str] = {}
            phases: dict[str, str] = {}
            tools: dict[str, dict[str, Any]] = {}
            tokens: int | None = None
            denied = False
            questions: dict[str, tuple[Any, list[dict[str, Any]]]] = {}
            approvals: dict[str, dict[str, Any]] = {}
            next_input_poll = 0.0
            while True:
                check()
                if request.receive_message and time.monotonic() >= next_input_poll:
                    next_input_poll = time.monotonic() + 0.4
                    incoming = request.receive_message()
                    if incoming:
                        delivered, reason = False, None
                        try:
                            question_id = incoming.get("question_id")
                            permission_id = incoming.get("permission_id")
                            if permission_id:
                                if permission_id not in approvals or incoming.get(
                                    "permission_reply"
                                ) not in {"once", "reject"}:
                                    raise ValueError("permission_expired")
                                native = approvals.pop(permission_id)
                                decision = (
                                    "accept"
                                    if incoming["permission_reply"] == "once"
                                    else "decline"
                                )
                                self._stream.send(
                                    {"id": native["id"], "result": {"decision": decision}}
                                )
                                emit(
                                    "agent.permission_resolved",
                                    permission_id=permission_id,
                                    decision=decision,
                                )
                                emit(
                                    "agent.input_closed", question_id=f"permission:{permission_id}"
                                )
                                delivered = True
                            elif question_id:
                                from agents_ide.adapters.interaction import question_answers

                                if question_id not in questions:
                                    raise ValueError("question_expired")
                                native_id, question_list = questions[question_id]
                                answers = question_answers(incoming, question_list)
                                self._stream.send(
                                    {
                                        "id": native_id,
                                        "result": {
                                            "answers": {
                                                item["id"]: {"answers": answer}
                                                for item, answer in zip(
                                                    question_list, answers, strict=True
                                                )
                                            }
                                        },
                                    }
                                )
                                questions.pop(question_id)
                                emit("agent.input_closed", question_id=question_id)
                                delivered = True
                            else:
                                reply = self._request_response(
                                    "turn/steer",
                                    {
                                        "threadId": returned_id,
                                        "expectedTurnId": self._turn_id,
                                        "input": [{"type": "text", "text": incoming["text"]}],
                                    },
                                    timeout=min(5, self.request_timeout),
                                    check=check,
                                    notifications=pending,
                                )
                                delivered = "error" not in reply
                                reason = None if delivered else "agent_rejected_message"
                        except (OSError, ValueError):
                            reason = "delivery_unconfirmed"
                        emit(
                            "agent.user_message_status",
                            command_id=incoming["command_id"],
                            delivered=delivered,
                            reason=reason,
                        )
                try:
                    message = pending.pop(0) if pending else self._stream.receive(0.1)
                except queue.Empty:
                    continue
                method = message.get("method")
                if not isinstance(method, str):
                    continue
                params = message.get("params") or {}
                if not isinstance(params, dict):
                    raise ValueError("Invalid notification")
                native_turn = params.get("turnId", (params.get("turn") or {}).get("id"))
                matching = params.get("threadId") == returned_id and native_turn == self._turn_id
                if matching or (params.get("threadId") == returned_id and native_turn is None):
                    archive_native(request.emit_event, "codex", method, message, returned_id)
                if "id" in message:
                    if (
                        matching
                        and method
                        in {
                            "item/commandExecution/requestApproval",
                            "item/fileChange/requestApproval",
                        }
                        and request.receive_message
                        and options["approvalPolicy"] == "on-request"
                    ):
                        permission_id = str(message["id"])
                        approvals[permission_id] = message
                        emit(
                            "agent.permission_requested", permission_id=permission_id, method=method
                        )
                        emit(
                            "agent.input_requested",
                            kind="permission",
                            question_id=f"permission:{permission_id}",
                            permission_id=permission_id,
                            permission=method,
                            patterns=[
                                params.get("command") or params.get("reason") or "Изменение файлов"
                            ],
                            metadata=params,
                            questions=[],
                        )
                        continue
                    if (
                        matching
                        and method == "item/tool/requestUserInput"
                        and request.receive_message
                    ):
                        from agents_ide.adapters.interaction import questions_for_ui

                        question_id = str(message["id"])
                        question_list = questions_for_ui(params.get("questions"))
                        questions[question_id] = (message["id"], question_list)
                        emit(
                            "agent.input_requested",
                            question_id=question_id,
                            questions=question_list,
                        )
                        continue
                    if not message.get("_answered"):
                        _decline(self._stream, message)
                    if matching:
                        denied = True
                        emit(
                            "agent.permission_requested",
                            permission_id=str(message["id"]),
                            method=method,
                        )
                        emit(
                            "agent.permission_resolved",
                            permission_id=str(message["id"]),
                            decision="decline",
                        )
                    continue
                if not matching:
                    continue
                if method == "item/agentMessage/delta":
                    key = validate_thread_id(params.get("itemId"))
                    delta = params.get("delta")
                    if not isinstance(delta, str):
                        raise ValueError("Invalid text delta")
                    texts[key] = texts.get(key, "") + delta
                    emit(
                        "attempt.text_delta", text=delta, message_id=self._turn_id, role="assistant"
                    )
                elif method == "item/completed":
                    item = params.get("item") or {}
                    key = validate_thread_id(item.get("id"))
                    if item.get("type") == "agentMessage":
                        if not isinstance(item.get("text"), str):
                            raise ValueError("Invalid agent message")
                        texts[key] = item["text"]  # authoritative; never duplicate deltas
                        phases[key] = str(item.get("phase") or "")
                    elif item.get("type") not in {"userMessage", "reasoning", "plan"}:
                        tools[key] = item
                        emit("agent.tool_call", item=item)
                elif method == "thread/tokenUsage/updated":
                    value = params.get("tokenUsage", {}).get("last", {}).get("totalTokens")
                    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                        tokens = value
                    emit("budget.updated", tokens_used=tokens, cost=None, source_quality="native")
                elif method == "item/started":
                    item = params.get("item") or {}
                    if item.get("type") not in {"userMessage", "agentMessage", "reasoning", "plan"}:
                        emit("agent.tool_call", item=item, phase="started")
                elif method in {"turn/plan/updated", "turn/diff/updated"}:
                    emit("agent.plan_updated", native_type=method, details=params)
                elif method.endswith("/delta"):
                    emit("agent.output_delta", native_type=method, details=params)
                elif method == "turn/completed":
                    turn = params["turn"]
                    terminal = turn.get("status") in {"completed", "failed", "interrupted"}
                    if denied:
                        return failure("permission_denied", ExternalOutcome.PERMISSION_DENIED)
                    if turn.get("status") == "interrupted":
                        emit("agent.session_aborted", message_id=self._turn_id)
                        return failure("interrupted")
                    if turn.get("error") or turn.get("status") != "completed":
                        if not texts and not tools:
                            try:
                                rejection = json.loads(turn.get("error", {}).get("message", ""))
                            except (ValueError, AttributeError, TypeError):
                                rejection = {}
                            if (
                                isinstance(rejection, dict)
                                and rejection.get("status") == 400
                                and rejection.get("error", {}).get("code") == "invalid_json_schema"
                                and rejection.get("error", {}).get("param") == "text.format.schema"
                            ):
                                return failure(
                                    "configuration_invalid",
                                    ExternalOutcome.CONFIRMED_FAILURE,
                                    safe=True,
                                )
                        return failure("provider_result_unknown")
                    finals = [
                        text for key, text in texts.items() if phases.get(key) == "final_answer"
                    ]
                    text = "\n".join(finals or texts.values())
                    return AgentResult(
                        ExternalOutcome.SUCCEEDED,
                        text,
                        {"text": text},
                        None,
                        tool_calls=tuple(tools.values()),
                        elapsed_seconds=time.monotonic() - started,
                        tokens_used=tokens,
                        cost_estimated=None,
                        budget_quality="unknown",
                    )
        except CodexConfigurationError:
            return failure("configuration_invalid", ExternalOutcome.CONFIRMED_FAILURE, safe=True)
        except InterruptedError:
            return failure(
                "interrupted",
                ExternalOutcome.UNKNOWN if sent else ExternalOutcome.RETRYABLE_FAILURE,
                safe=not sent,
            )
        except OSError:
            return failure(
                "transport_closed",
                ExternalOutcome.UNKNOWN if sent else ExternalOutcome.RETRYABLE_FAILURE,
                safe=not sent,
            )
        except (ValueError, KeyError, TypeError, AttributeError):
            return failure(
                "server_transport_error",
                ExternalOutcome.UNKNOWN if sent else ExternalOutcome.CONFIRMED_FAILURE,
                safe=not sent,
            )
        finally:
            # Never leave an ambiguous turn running in a connection reused by
            # the next role. Normal completion needs no interrupt.
            if sent and not terminal:
                self.interrupt(self.external_session_id or "")
                # Give native cancellation a bounded chance; tree termination
                # still follows and does not claim that external effects vanished.
                until = time.monotonic() + 0.5
                while self._turn_id and time.monotonic() < until:
                    try:
                        event = self._stream.receive(0.05)
                    except queue.Empty:
                        continue
                    except OSError:
                        break
                    params = event.get("params") or {}
                    if (
                        event.get("method") == "turn/completed"
                        and params.get("threadId") == self.external_session_id
                        and (params.get("turn") or {}).get("id") == self._turn_id
                    ):
                        archive_native(
                            request.emit_event,
                            "codex",
                            "turn/completed",
                            event,
                            self.external_session_id or "",
                        )
                        break
                self._stream.close()
