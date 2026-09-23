"""OpenCode HTTP/SSE transport; protocol fields come from server /doc.

Native mode preserves OpenCode's own permission policy and relays approvals.
Context-only sessions deny tools. Catalog membership is not proof of provider access.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import math
import re
import threading
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from jsonschema import Draft202012Validator

from agents_ide.adapters.base import (
    AdapterError,
    AgentAdapter,
    AgentAdapterRequest,
    AgentResult,
    ExternalOutcome,
)
from agents_ide.adapters.history import OMITTED_HISTORY_EVENTS
from agents_ide.adapters.model_catalog import CatalogModels, parameters_for
from agents_ide.adapters.native_events import archive_native
from agents_ide.errors import AppError

MAX_RESPONSE_BYTES = 10 * 1024 * 1024
HEALTH_TIMEOUT_SECONDS = OPENCODE_HEALTH_TIMEOUT = 2.0
STARTUP_TIMEOUT_SECONDS = 30.0
TOOL_OUTPUT_SAMPLE_SECONDS = 1.0
TEXT_SNAPSHOT_SAMPLE_SECONDS = 2.0
DENY_TOOLS = [{"permission": "*", "pattern": "*", "action": "deny"}]
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


class OpenCodeConfigurationError(AppError):
    def __init__(self, message: str) -> None:
        super().__init__("configuration_invalid", message, status=422)


class ResponseLimit(ValueError):
    pass


def _message_tokens(info: dict[str, Any]) -> int | None:
    usage = info.get("tokens")
    if not isinstance(usage, dict):
        return None
    cache = usage.get("cache")
    if not isinstance(cache, dict):
        return None
    metrics = [usage.get(key) for key in ("input", "output", "reasoning")]
    metrics.extend(cache.get(key) for key in ("read", "write"))
    return (
        sum(value for value in metrics if isinstance(value, int))
        if all(type(value) is int and value >= 0 for value in metrics)
        else None
    )


def _validate_loopback_url(value: str) -> str:
    try:
        p = urlsplit(value)
        if (
            p.scheme != "http"
            or p.hostname not in {"127.0.0.1", "::1", "localhost"}
            or not p.port
            or p.username is not None
            or p.password is not None
            or p.query
            or p.fragment
            or p.path not in {"", "/"}
        ):
            raise ValueError
    except ValueError:
        raise OpenCodeConfigurationError(
            "OpenCode requires a loopback HTTP origin and port"
        ) from None
    return value.rstrip("/")


def validate_id(value: Any) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("Invalid native identifier")
    return value


def bounded_request(
    client: httpx.Client, method: str, path: str, *, limit: int | None = None, **kwargs: Any
) -> tuple[httpx.Response, bytes]:
    with client.stream(method, path, **kwargs) as response:
        body = bytearray()
        for chunk in response.iter_bytes():
            if limit is not None and len(body) + len(chunk) > limit:
                raise ResponseLimit("OpenCode response exceeds limit")
            body.extend(chunk)
        return response, bytes(body)


def sse_events(chunks: Iterable[bytes], *, limit: int | None = None) -> Iterator[dict[str, Any]]:
    """Incremental UTF-8 SSE parsing; optional caps are only for explicit probes."""
    buffer = bytearray()
    data: list[bytes] = []
    size = 0
    for chunk in chunks:
        buffer.extend(chunk)
        while b"\n" in buffer:
            line, _, rest = buffer.partition(b"\n")
            buffer = bytearray(rest)
            line = line.rstrip(b"\r")
            size += len(line)
            if limit is not None and size > limit:
                raise ResponseLimit("OpenCode event exceeds limit")
            if not line:
                if data:
                    value = json.loads(b"\n".join(data))
                    if isinstance(value, dict) and isinstance(value.get("type"), str):
                        yield value
                data, size = [], 0
            elif line.startswith(b"data:"):
                data.append(bytes(line[5:].removeprefix(b" ")))
        if limit is not None and size + len(buffer) > limit:
            raise ResponseLimit("OpenCode event exceeds limit")


def iter_sse_events(body: bytes) -> Iterator[dict[str, Any]]:
    return sse_events([body])


@dataclass(frozen=True)
class OpenCodeCapabilities:
    transport: str = "http"
    stream_events: bool = True
    resume_session: bool = True
    native_interrupt: bool = True
    permissions: bool = False  # Interactive permission bridge is not implemented.
    structured_output: bool = False
    list_models: bool = True
    autonomous_write: bool = False


def capabilities_for_kind() -> OpenCodeCapabilities:
    return OpenCodeCapabilities()


@dataclass(frozen=True)
class SessionBinding:
    session_id: str
    resume_count: int = 0


@dataclass(frozen=True)
class RunSession:
    base_url: str
    username: str
    password: str | None
    workspace_path: str
    settings: dict[str, Any]
    server_version: str | None


class OpenCodeAdapter(AgentAdapter):
    name = "opencode"

    def __init__(
        self,
        *,
        session: RunSession,
        startup_timeout: float | None = None,
        timeout_seconds: float | None = None,
        max_response_bytes: int | None = None,
        health_timeout: float = HEALTH_TIMEOUT_SECONDS,
    ) -> None:
        self.base_url = _validate_loopback_url(session.base_url)
        self.session = session
        self.server_version = session.server_version
        self.timeout_seconds = timeout_seconds
        self.startup_timeout = startup_timeout
        self.health_timeout = health_timeout
        self.max_response_bytes = max_response_bytes
        self._binding = SessionBinding("")
        self._dispatch_lock = threading.Lock()

    @property
    def external_session_id(self) -> str | None:
        return self._binding.session_id or None

    def client(self, *, timeout: float | None = None) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            auth=httpx.BasicAuth(self.session.username, self.session.password)
            if self.session.password is not None
            else None,
            params={"directory": self.session.workspace_path},
            timeout=httpx.Timeout(timeout if timeout is not None else self.timeout_seconds),
            trust_env=False,
            follow_redirects=False,
        )

    def interrupt(self, session_id: str) -> None:
        try:
            with self.client(timeout=2) as client:
                bounded_request(client, "POST", f"/session/{validate_id(session_id)}/abort")
        except (httpx.HTTPError, OSError, ValueError):
            pass

    def run(self, request: AgentAdapterRequest) -> AgentResult:
        with self._dispatch_lock:
            return self._run(request)

    def _run(self, request: AgentAdapterRequest) -> AgentResult:
        record_tool_history = getattr(request, "record_tool_history", False)
        started = time.monotonic()
        sent = False
        done, ready, permission = threading.Event(), threading.Event(), threading.Event()
        observed_output = threading.Event()
        activity_message_ids: set[str] = set()
        message_roles: dict[str, str] = {}
        tool_progress: dict[tuple[str, str], dict[str, Any]] = {}
        tool_archived_at: dict[tuple[str, str], float] = {}
        text_archived_at: dict[tuple[str, str], float] = {}
        errors: list[Exception] = []
        threads: list[threading.Thread] = []
        questions: dict[str, list[dict[str, Any]]] = {}
        approvals: set[str] = set()
        child_sessions: set[str] = set()
        malformed_output = threading.Event()
        text_tail = ""
        deadline = request.deadline_at or (
            time.time() + self.timeout_seconds if self.timeout_seconds is not None else float("inf")
        )

        def fail(
            code: str,
            outcome: ExternalOutcome,
            *,
            safe: bool = False,
            interruption_confirmed: bool = False,
        ) -> AgentResult:
            can_handoff = sent and code not in {
                "interrupted",
                "permission_denied",
                "configuration_invalid",
                "session_resume_unavailable",
            }
            details: dict[str, Any] = (
                {"interruption_confirmed": True} if interruption_confirmed else {}
            )
            if code == "event_stream_lost" and errors:
                error = errors[0]
                details = {
                    "reason": "event_size_limit"
                    if isinstance(error, ResponseLimit)
                    else "event_stream_timeout"
                    if isinstance(error, httpx.TimeoutException)
                    else "event_stream_error",
                    "exception_type": type(error).__name__,
                }
            return AgentResult(
                ExternalOutcome.UNAVAILABLE if can_handoff else outcome,
                text_tail,
                None,
                None,
                tool_calls=tuple(tool_progress.values())[-20:],
                error=AdapterError(code, code, "safe" if safe else "unknown", details),
                no_effect=safe,
                can_handoff=can_handoff,
                elapsed_seconds=time.monotonic() - started,
            )

        def emit(kind: str, payload: dict[str, Any]) -> None:
            if not record_tool_history and kind in OMITTED_HISTORY_EVENTS:
                return
            if request.emit_event:
                request.emit_event(kind, payload)

        def emit_context_usage(info: dict[str, Any]) -> None:
            tokens = _message_tokens(info)
            # New assistant messages start with zero counters before the model replies.
            if info.get("role") == "assistant" and tokens is not None and tokens > 0:
                emit(
                    "budget.updated",
                    {"context_tokens": tokens, "source_quality": "native"},
                )

        def check() -> None:
            if request.check_owned:
                request.check_owned()
            if (request.stop_event and request.stop_event.is_set()) or time.time() >= deadline:
                raise InterruptedError

        def observe_output(message_id: Any) -> None:
            if isinstance(message_id, str):
                activity_message_ids.add(message_id)
            else:
                observed_output.set()

        def text_delta(delta: str) -> None:
            nonlocal text_tail
            if malformed_output.is_set():
                return
            emit("attempt.text_delta", {"text": delta, "session_id": resume})
            text_tail = (text_tail + delta)[-2048:]
            if text_tail.count("]<]minimax[>[<tool_call>") >= 8:
                malformed_output.set()
                self.interrupt(str(resume))

        try:
            check()
            body = self._build_message(request)
            with self.client(timeout=self.startup_timeout) as client:
                # Resume only the exact engine-selected session. Check before sending a message;
                # a 404 returned after dispatch is not proof of zero model/tool execution.
                resume_required = getattr(request, "resume_required", False)
                resume = request.resume_session_id or (
                    None if resume_required else self.external_session_id
                )
                if resume_required and not resume:
                    return fail(
                        "session_resume_unavailable", ExternalOutcome.UNAVAILABLE, safe=True
                    )
                if resume:
                    response, raw = bounded_request(
                        client, "GET", f"/session/{validate_id(resume)}"
                    )
                    if response.status_code == 404:
                        emit(
                            "agent.session_invalidated",
                            {"session_id": resume, "reason": "not_found"},
                        )
                        self._binding = SessionBinding("")
                        if resume_required:
                            return fail(
                                "session_resume_unavailable", ExternalOutcome.UNAVAILABLE, safe=True
                            )
                        resume = None
                    elif response.status_code != 200:
                        return self._http_failure(response.status_code, dispatched=False)
                    else:
                        data = json.loads(raw)
                        self._validate_session(data, resume)
                if not resume:
                    check()
                    response, raw = bounded_request(
                        client,
                        "POST",
                        "/session",
                        json={
                            "title": f"agents-ide/{request.role}"[:128],
                            "permission": self._permissions(),
                        },
                    )
                    if response.status_code != 200:
                        return self._http_failure(response.status_code, dispatched=False)
                    data = json.loads(raw)
                    resume = validate_id(data.get("id"))
                    self._validate_session(data, resume)
                    self._binding = SessionBinding(resume)
                    event = "agent.session_created"
                else:
                    self._binding = SessionBinding(resume, self._binding.resume_count + 1)
                    event = "agent.session_resumed"
                # The callback is a fenced durable write, before any dependent model request.
                emit(
                    event,
                    {
                        "session_id": resume,
                        "server_version": self.server_version,
                        "resume_count": self._binding.resume_count,
                        "permission_mode": self.session.settings.get("permission_mode", "no_tools"),
                    },
                )

            def handle_event(event: dict[str, Any]) -> None:
                props = event.get("properties", {})
                if not isinstance(props, dict):
                    return
                part = props.get("part", {})
                info = props.get("info", {})
                sid = (
                    props.get("sessionID")
                    or (part.get("sessionID") if isinstance(part, dict) else None)
                    or (info.get("sessionID") if isinstance(info, dict) else None)
                )
                kind = event["type"]
                # A native task tool waits for its child session. Its prompts
                # must reach the user too, but unrelated sessions and child
                # answer/tool streams must not enter the parent's history.
                if sid != self.external_session_id and (
                    kind
                    not in {
                        "permission.asked",
                        "permission.replied",
                        "question.asked",
                        "question.replied",
                        "question.rejected",
                    }
                    or not self._owns_child_session(sid, str(resume), child_sessions)
                ):
                    return

                def emit_interaction(kind: str, payload: dict[str, Any]) -> None:
                    # The engine uses session_id as its continuation identity.
                    # Keep the root binding even when a subagent needs a reply.
                    emit(
                        kind,
                        {
                            **payload,
                            "session_id": resume,
                            **({"source_session_id": sid} if sid != resume else {}),
                        },
                    )

                tool_update = None
                tool_key = None
                tool_changed = True
                if (
                    kind == "message.part.updated"
                    and isinstance(part, dict)
                    and part.get("type") == "tool"
                ):
                    observe_output(part.get("messageID"))
                    state = part.get("state", {})
                    inputs = state.get("input", {})
                    summary = state.get("title") or next(
                        (inputs[k] for k in ("command", "filePath", "pattern") if inputs.get(k)),
                        "",
                    )
                    call_id = part.get("callID") or part.get("id")
                    tool_update = {
                        "session_id": sid,
                        "call_id": call_id,
                        "tool": part.get("tool"),
                        "status": state.get("status"),
                        "summary": str(summary)[:500],
                    }
                    if isinstance(call_id, str):
                        tool_key = (str(part.get("messageID", "")), call_id)
                        now = time.monotonic()
                        tool_changed = tool_progress.get(tool_key) != tool_update
                        # OpenCode repeats the entire growing output for each chunk.
                        # Sample intermediate snapshots before the durable callback;
                        # always preserve state changes and the full terminal result.
                        if (
                            not tool_changed
                            and state.get("status") in {"pending", "running"}
                            and now - tool_archived_at[tool_key] < TOOL_OUTPUT_SAMPLE_SECONDS
                        ):
                            return
                        tool_progress[tool_key] = tool_update
                archive = True
                if (
                    kind == "message.part.updated"
                    and isinstance(part, dict)
                    and part.get("type") in {"text", "reasoning"}
                    and len(str(part.get("text", ""))) > 4096
                    and (part.get("time") or {}).get("end") is None
                ):
                    # Large growing snapshots can exceed the event buffer's block
                    # limit on every token. Sample that redundant archive, while
                    # forwarding every normalized delta and the completed snapshot.
                    text_key = (str(part.get("messageID", "")), str(part.get("id", "")))
                    now = time.monotonic()
                    archive = now - text_archived_at.get(text_key, float("-inf")) >= (
                        TEXT_SNAPSHOT_SAMPLE_SECONDS
                    )
                    if archive:
                        text_archived_at[text_key] = now
                if archive:
                    archive_native(
                        request.emit_event if record_tool_history else None,
                        "opencode",
                        kind,
                        event,
                        str(resume),
                    )
                if tool_key is not None:
                    tool_archived_at[tool_key] = time.monotonic()
                if kind == "question.asked" and request.receive_message:
                    from agents_ide.adapters.interaction import questions_for_ui

                    question_id = validate_id(props.get("id"))
                    question_list = questions_for_ui(props.get("questions"))
                    questions[question_id] = question_list
                    emit_interaction(
                        "agent.input_requested",
                        {"question_id": question_id, "questions": question_list},
                    )
                elif kind in {"question.replied", "question.rejected"}:
                    question_id = str(props.get("requestID", ""))
                    questions.pop(question_id, None)
                    emit_interaction("agent.input_closed", {"question_id": question_id})
                elif kind == "permission.asked":
                    permission_id = validate_id(props.get("id"))
                    emit_interaction(
                        "agent.permission_requested",
                        {
                            "session_id": sid,
                            "permission_id": permission_id,
                            "permission": props.get("permission"),
                        },
                    )
                    if (
                        self.session.settings.get("permission_mode") == "native"
                        and self.session.settings.get("auto_approve") is True
                    ):
                        with self.client() as event_client:
                            reply, _ = bounded_request(
                                event_client,
                                "POST",
                                f"/permission/{permission_id}/reply",
                                json={"reply": "once"},
                            )
                        if reply.status_code != 200:
                            raise ValueError("Permission reply failed")
                        emit_interaction(
                            "agent.permission_resolved",
                            {
                                "session_id": sid,
                                "permission_id": permission_id,
                                "reply": "once",
                                "automatic": True,
                            },
                        )
                        return
                    if (
                        self.session.settings.get("permission_mode") == "native"
                        and request.receive_message
                    ):
                        approvals.add(permission_id)
                        emit_interaction(
                            "agent.input_requested",
                            {
                                "kind": "permission",
                                "question_id": f"permission:{permission_id}",
                                "permission_id": permission_id,
                                "permission": props.get("permission"),
                                "patterns": props.get("patterns", []),
                                "metadata": props.get("metadata", {}),
                                "questions": [],
                            },
                        )
                        return
                    permission.set()
                    with self.client() as event_client:
                        reply, _ = bounded_request(
                            event_client,
                            "POST",
                            f"/permission/{permission_id}/reply",
                            json={"reply": "reject"},
                        )
                    if reply.status_code == 200:
                        emit_interaction(
                            "agent.permission_resolved",
                            {
                                "session_id": sid,
                                "permission_id": permission_id,
                                "reply": "reject",
                            },
                        )
                    self.interrupt(str(resume))
                elif kind == "permission.replied":
                    permission_id = str(props.get("requestID", ""))
                    approvals.discard(permission_id)
                    emit_interaction(
                        "agent.input_closed", {"question_id": f"permission:{permission_id}"}
                    )
                elif kind == "message.part.delta" and props.get("field") == "text":
                    delta = props.get("delta")
                    if isinstance(delta, str):
                        if delta:
                            observe_output(props.get("messageID"))
                        text_delta(delta)
                elif kind == "message.part.updated" and isinstance(part, dict):
                    if part.get("type") in {"text", "reasoning", "tool"}:
                        observe_output(part.get("messageID"))
                    if part.get("type") == "tool":
                        if tool_changed and tool_update is not None:
                            emit("agent.tool_call", tool_update)
                    elif isinstance(props.get("delta"), str):
                        text_delta(props["delta"])
                elif kind == "message.updated" and isinstance(info, dict):
                    emit_context_usage(info)
                    if isinstance(info.get("id"), str) and isinstance(info.get("role"), str):
                        message_roles[info["id"]] = info["role"]
                    emit(
                        "attempt.progress",
                        {
                            "session_id": sid,
                            "message_id": validate_id(info.get("id")),
                            "role": info.get("role"),
                        },
                    )
                elif kind in {"session.status", "session.error", "session.idle"}:
                    emit(
                        "attempt.progress",
                        {"session_id": sid, "native_type": kind, "details": props},
                    )
                elif kind in {"todo.updated", "session.diff"}:
                    emit(
                        "agent.plan_updated",
                        {"session_id": sid, "native_type": kind, "details": props},
                    )
                elif kind == "message.part.delta":
                    if props.get("delta"):
                        observe_output(props.get("messageID"))
                    emit(
                        "agent.output_delta",
                        {"session_id": sid, "native_type": kind, "details": props},
                    )

            async def event_loop() -> None:
                async def consume() -> None:
                    with self.client() as config_client:
                        headers = dict(config_client.headers)
                    async with (
                        httpx.AsyncClient(
                            base_url=self.base_url,
                            headers=headers,
                            params={"directory": self.session.workspace_path},
                            auth=httpx.BasicAuth(self.session.username, self.session.password)
                            if self.session.password is not None
                            else None,
                            trust_env=False,
                            follow_redirects=False,
                            timeout=httpx.Timeout(None),
                        ) as client,
                        client.stream("GET", "/event") as response,
                    ):
                        if response.status_code != 200:
                            raise ValueError("Event stream rejected")
                        pending = bytearray()
                        async for chunk in response.aiter_bytes():
                            pending.extend(chunk)
                            while True:
                                lf, crlf = pending.find(b"\n\n"), pending.find(b"\r\n\r\n")
                                positions = [(lf, 2), (crlf, 4)]
                                positions = [(pos, size) for pos, size in positions if pos >= 0]
                                if not positions:
                                    break
                                pos, size = min(positions)
                                frame = bytes(pending[: pos + size])
                                del pending[: pos + size]
                                for event in sse_events([frame], limit=self.max_response_bytes):
                                    if event["type"] == "server.connected":
                                        ready.set()
                                    else:
                                        handle_event(event)
                            if (
                                self.max_response_bytes is not None
                                and len(pending) > self.max_response_bytes
                            ):
                                raise ResponseLimit("Event exceeds limit")
                        if not done.is_set():
                            raise ValueError("Event stream closed")

                task = asyncio.create_task(consume())
                try:
                    while not done.is_set() and not task.done():
                        await asyncio.sleep(0.02)
                    if task.done():
                        await task
                finally:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

            def stream() -> None:
                try:
                    asyncio.run(event_loop())
                except Exception as exc:
                    if not done.is_set():
                        errors.append(exc)
                finally:
                    ready.set()

            def watch() -> None:
                next_input_poll = 0.0
                while not done.wait(0.05):
                    try:
                        check()
                        if errors:
                            raise InterruptedError
                        if sent and request.receive_message and time.monotonic() >= next_input_poll:
                            next_input_poll = time.monotonic() + 0.4
                            incoming = request.receive_message()
                            if incoming:
                                delivered, reason = False, None
                                try:
                                    question_id = incoming.get("question_id")
                                    permission_id = incoming.get("permission_id")
                                    data: dict[str, Any]
                                    if permission_id:
                                        if permission_id not in approvals or incoming.get(
                                            "permission_reply"
                                        ) not in {"once", "reject"}:
                                            raise ValueError("permission_expired")
                                        path = f"/permission/{validate_id(permission_id)}/reply"
                                        data = {"reply": incoming["permission_reply"]}
                                    elif question_id:
                                        from agents_ide.adapters.interaction import question_answers

                                        if question_id not in questions:
                                            raise ValueError("question_expired")
                                        path = f"/question/{validate_id(question_id)}/reply"
                                        data = {
                                            "answers": question_answers(
                                                incoming, questions[question_id]
                                            )
                                        }
                                    else:
                                        path = f"/session/{resume}/prompt_async"
                                        data = {
                                            **body,
                                            # Append to the active loop; never start an unowned
                                            # turn if completion races with this message.
                                            "noReply": True,
                                            "parts": [{"type": "text", "text": incoming["text"]}],
                                        }
                                    with self.client() as input_client:
                                        reply, _ = bounded_request(
                                            input_client, "POST", path, json=data
                                        )
                                    delivered = reply.status_code in {200, 204}
                                    if delivered and permission_id:
                                        approvals.discard(permission_id)
                                        emit(
                                            "agent.permission_resolved",
                                            {
                                                "session_id": resume,
                                                "permission_id": permission_id,
                                                "reply": incoming["permission_reply"],
                                            },
                                        )
                                        emit(
                                            "agent.input_closed",
                                            {"question_id": f"permission:{permission_id}"},
                                        )
                                    elif delivered and question_id:
                                        questions.pop(question_id, None)
                                        emit("agent.input_closed", {"question_id": question_id})
                                    reason = None if delivered else "agent_rejected_message"
                                except (httpx.HTTPError, OSError, ValueError):
                                    reason = "delivery_unconfirmed"
                                emit(
                                    "agent.user_message_status",
                                    {
                                        "command_id": incoming["command_id"],
                                        "delivered": delivered,
                                        "reason": reason,
                                    },
                                )
                    except (AppError, InterruptedError):
                        self.interrupt(self.external_session_id or "")
                        return

            threads = [
                threading.Thread(target=stream, daemon=True),
                threading.Thread(target=watch, daemon=True),
            ]
            for thread in threads:
                thread.start()
            while not ready.wait(0.1) and not errors:
                check()
            if errors:
                return fail(
                    "event_stream_unavailable", ExternalOutcome.RETRYABLE_FAILURE, safe=True
                )
            check()
            with self.client(
                timeout=max(0.1, deadline - time.time()) if math.isfinite(deadline) else None
            ) as client:
                sent = True
                response, raw = bounded_request(
                    client,
                    "POST",
                    f"/session/{resume}/message",
                    json=body,
                    limit=self.max_response_bytes,
                )
            if response.status_code != 200:
                if response.status_code not in {401, 403}:
                    # The local harness may have dispatched tools before its HTTP
                    # request failed. Stop it and hand off the existing workspace.
                    return fail("server_unavailable", ExternalOutcome.UNAVAILABLE)
                return self._http_failure(response.status_code, dispatched=True)
            if permission.is_set():
                return fail("permission_denied", ExternalOutcome.PERMISSION_DENIED)
            if malformed_output.is_set():
                return fail("provider_tool_protocol_invalid", ExternalOutcome.INVALID_FORMAT)
            if errors:
                return fail("event_stream_lost", ExternalOutcome.UNKNOWN)
            payload = json.loads(raw)
            info, parts = payload.get("info"), payload.get("parts")
            if (
                not isinstance(info, dict)
                or not isinstance(parts, list)
                or info.get("sessionID") != resume
                or info.get("role") != "assistant"
            ):
                return fail("invalid_provider_payload", ExternalOutcome.INVALID_FORMAT)
            emit_context_usage(info)
            archive_native(
                request.emit_event if record_tool_history else None,
                "opencode",
                "message.completed",
                payload,
                resume,
            )
            emit(
                "attempt.progress",
                {
                    "session_id": resume,
                    "message_id": validate_id(info.get("id")),
                    "role": "assistant",
                },
            )
            if (
                isinstance(info.get("error"), dict)
                and info["error"].get("name") == "MessageAbortedError"
            ):
                emit("agent.session_aborted", {"session_id": resume})
                return fail("interrupted", ExternalOutcome.UNKNOWN, interruption_confirmed=True)
            if info.get("error"):
                native_error = info["error"]
                if (
                    isinstance(native_error, dict)
                    and native_error.get("name") == "StructuredOutputError"
                ):
                    return fail("invalid_response_format", ExternalOutcome.INVALID_FORMAT)
                usage = info.get("tokens", {})
                zero_usage = isinstance(usage, dict) and usage == {
                    "input": 0,
                    "output": 0,
                    "reasoning": 0,
                    "cache": {"read": 0, "write": 0},
                }
                zero_usage = zero_usage and all(
                    type(value) is int
                    for value in (
                        usage["input"],
                        usage["output"],
                        usage["reasoning"],
                        usage["cache"]["read"],
                        usage["cache"]["write"],
                    )
                )
                data = native_error.get("data", {}) if isinstance(native_error, dict) else {}
                from agents_ide.adapters.provider_limits import limit_error_code

                if (
                    isinstance(native_error, dict)
                    and native_error.get("name") == "APIError"
                    and isinstance(data, dict)
                    and data.get("statusCode") == 401
                    and data.get("isRetryable") is False
                    and parts == []
                    and zero_usage
                    and not observed_output.is_set()
                    and all(message_roles.get(mid) == "user" for mid in tuple(activity_message_ids))
                ):
                    return fail("provider_unauthorized", ExternalOutcome.UNAVAILABLE, safe=True)
                # Every completed native error permits a handoff, even HTTP 400
                # or an unfamiliar provider error marked isRetryable=false. That
                # flag controls retrying this provider, not trying another one.
                # The runner stops the process tree before transferring work.
                data = data if isinstance(data, dict) else {}
                status = data.get("statusCode")
                handoff_code = limit_error_code(data, status) or (
                    "provider_unauthorized" if status in (401, 403) else "provider_unavailable"
                )
                return AgentResult(
                    ExternalOutcome.UNAVAILABLE,
                    text_tail,
                    None,
                    None,
                    tool_calls=tuple(tool_progress.values())[-20:],
                    error=AdapterError(
                        handoff_code,
                        handoff_code,
                        "unknown",
                        {"status": status if type(status) is int else None},
                    ),
                    can_handoff=True,
                    elapsed_seconds=time.monotonic() - started,
                )
            schema = request.capabilities.get("output_schema")
            structured_key = "structured" if "structured" in info else "structured_output"
            structured_completion = isinstance(schema, dict) and structured_key in info
            if info.get("finish") not in {"stop", "end_turn", "length"} and not (
                info.get("finish") == "tool-calls" and structured_completion
            ):
                return fail("provider_result_incomplete", ExternalOutcome.UNKNOWN)
            text = "".join(
                p["text"]
                for p in parts
                if isinstance(p, dict)
                and p.get("type") == "text"
                and isinstance(p.get("text"), str)
            )
            if info.get("finish") == "length":
                return fail("response_truncated", ExternalOutcome.INVALID_FORMAT)
            if "]<]minimax[>[<tool_call>" in text:
                return fail("provider_tool_protocol_invalid", ExternalOutcome.INVALID_FORMAT)
            if structured_completion:
                assert isinstance(schema, dict)
                structured = info[structured_key]
                if (
                    "structured" in info
                    and "structured_output" in info
                    and info["structured"] != info["structured_output"]
                ):
                    return fail("schema_mismatch", ExternalOutcome.INVALID_FORMAT)
                if not isinstance(structured, dict) or not Draft202012Validator(schema).is_valid(
                    structured
                ):
                    return fail("schema_mismatch", ExternalOutcome.INVALID_FORMAT)
                # The native envelope (including commentary/tool parts) has already
                # been archived. The engine must validate the actual structured result.
                text = json.dumps(structured, ensure_ascii=False, allow_nan=False)
            tokens = _message_tokens(info)
            cost = info.get("cost")
            cost = (
                float(cost)
                if isinstance(cost, (int, float))
                and not isinstance(cost, bool)
                and math.isfinite(cost)
                and cost >= 0
                else None
            )
            calls = tuple(p for p in parts if isinstance(p, dict) and p.get("type") == "tool")
            return AgentResult(
                ExternalOutcome.SUCCEEDED,
                text,
                {"text": text},
                None,
                tool_calls=calls,
                elapsed_seconds=time.monotonic() - started,
                tokens_used=tokens,
                cost_estimated=cost,
                budget_quality="observed" if tokens is not None and cost is not None else "unknown",
            )
        except OpenCodeConfigurationError:
            return fail("configuration_invalid", ExternalOutcome.CONFIRMED_FAILURE, safe=True)
        except InterruptedError:
            return fail(
                "interrupted",
                ExternalOutcome.UNKNOWN if sent else ExternalOutcome.RETRYABLE_FAILURE,
                safe=not sent,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            return fail("server_connect_failed", ExternalOutcome.RETRYABLE_FAILURE, safe=True)
        except (httpx.HTTPError, OSError, ValueError, TypeError, KeyError):
            if sent:
                self.interrupt(self.external_session_id or "")
            if malformed_output.is_set():
                return fail("provider_tool_protocol_invalid", ExternalOutcome.INVALID_FORMAT)
            return fail(
                "server_transport_error",
                ExternalOutcome.UNKNOWN if sent else ExternalOutcome.RETRYABLE_FAILURE,
                safe=not sent,
            )
        finally:
            done.set()
            if sent and (
                (request.stop_event and request.stop_event.is_set())
                or errors
                or time.time() >= deadline
            ):
                self.interrupt(self.external_session_id or "")
            for thread in threads:
                thread.join(timeout=2.5)

    def _owns_child_session(self, session_id: Any, root: str, known: set[str]) -> bool:
        """Verify ancestry on demand, including children created before SSE attached."""
        from pathlib import Path

        if not isinstance(session_id, str) or not _ID.fullmatch(session_id):
            return False
        if session_id in known:
            return True
        ancestors: set[str] = set()
        current = session_id
        with self.client(timeout=self.health_timeout) as client:
            while current != root and current not in known:
                if current in ancestors or len(ancestors) >= 64:
                    return False
                ancestors.add(current)
                response, raw = bounded_request(client, "GET", f"/session/{current}")
                if response.status_code != 200:
                    return False
                data = json.loads(raw)
                if (
                    not isinstance(data, dict)
                    or data.get("id") != current
                    or not isinstance(data.get("directory"), str)
                    or Path(data["directory"]).resolve()
                    != Path(self.session.workspace_path).resolve()
                ):
                    return False
                parent = data.get("parentID")
                if not isinstance(parent, str) or not _ID.fullmatch(parent):
                    return False
                current = parent
        known.update(ancestors)
        return True

    def _permissions(self) -> list[dict[str, str]]:
        mode = self.session.settings.get("permission_mode", "no_tools")
        if mode not in {"no_tools", "native"}:
            raise OpenCodeConfigurationError("Unknown OpenCode permission mode")
        return [] if mode == "native" else DENY_TOOLS

    def _validate_session(self, data: Any, expected: str) -> None:
        if not isinstance(data, dict) or data.get("id") != expected:
            raise ValueError("Session identity mismatch")
        if data.get("permission") != self._permissions():
            raise OpenCodeConfigurationError("Server did not confirm the selected permission mode")
        from pathlib import Path

        if (
            not isinstance(data.get("directory"), str)
            or Path(data["directory"]).resolve() != Path(self.session.workspace_path).resolve()
        ):
            raise OpenCodeConfigurationError("Session directory mismatch")

    def _build_message(self, request: AgentAdapterRequest) -> dict[str, Any]:
        if "/" not in request.model_id:
            raise OpenCodeConfigurationError("Use an explicit provider/model identifier")
        provider, model = request.model_id.split("/", 1)
        if not provider or not model:
            raise OpenCodeConfigurationError("Use an explicit provider/model identifier")
        # These are model options, not arbitrary HTTP/config overrides. Unknown options
        # are rejected rather than silently accepting a different effective request.
        try:
            native_params = parameters_for(
                "opencode",
                request.model_id,
                request.params,
                self.session.settings.get("model_metadata", {}),
            )
        except AppError as exc:
            raise OpenCodeConfigurationError(exc.message) from None
        body = {
            **native_params,
            "model": {"providerID": provider, "modelID": model},
            "parts": [
                {"type": "text", "text": request.prompt},
                {
                    "type": "text",
                    "text": "Context and evidence (JSON):\n"
                    + json.dumps(request.context_package, ensure_ascii=False),
                },
            ],
        }
        if isinstance(request.capabilities.get("output_schema"), dict):
            if self.session.settings.get("permission_mode", "no_tools") == "native":
                body["format"] = {
                    "type": "json_schema",
                    "schema": request.capabilities["output_schema"],
                    # Retries belong to the durable engine, not a hidden harness loop.
                    "retryCount": 0,
                }
            else:
                # OpenCode implements native JSON via a tool. no_tools deliberately
                # denies that tool too; retain the policy and validate text in Runner.
                body["parts"].append(
                    {
                        "type": "text",
                        "text": "Return only a JSON object matching this output schema:\n"
                        + json.dumps(request.capabilities["output_schema"], ensure_ascii=False),
                    }
                )
        return body

    @staticmethod
    def _http_failure(status: int, *, dispatched: bool) -> AgentResult:
        # No prompt can have been executed when the server rejects basic auth.
        if status == 403:
            return AgentResult(
                ExternalOutcome.PERMISSION_DENIED,
                "",
                None,
                None,
                error=AdapterError("permission_denied", "OpenCode HTTP 403", "unknown"),
                no_effect=not dispatched,
            )
        auth = status == 401
        safe = not dispatched or auth
        outcome = (
            ExternalOutcome.UNAVAILABLE
            if auth
            else ExternalOutcome.RETRYABLE_FAILURE
            if safe and status >= 500
            else ExternalOutcome.CONFIRMED_FAILURE
            if safe
            else ExternalOutcome.UNKNOWN
        )
        code = (
            "server_unauthorized"
            if auth
            else "configuration_invalid"
            if safe and status < 500
            else "server_unavailable"
        )
        return AgentResult(
            outcome,
            "",
            None,
            None,
            error=AdapterError(
                code, f"OpenCode HTTP {status}", "safe" if safe else "unknown", {"status": status}
            ),
            no_effect=safe,
        )


def probe_json(
    base_url: str,
    path: str,
    *,
    timeout_seconds: float | None = None,
    username: str = "opencode",
    password: str | None = None,
    directory: str | None = None,
) -> Any:
    with httpx.Client(
        base_url=_validate_loopback_url(base_url),
        auth=httpx.BasicAuth(username, password) if password is not None else None,
        params={"directory": directory} if directory else {},
        trust_env=False,
        follow_redirects=False,
        timeout=timeout_seconds,
    ) as client:
        response, raw = bounded_request(client, "GET", path)
        response.raise_for_status()
        return json.loads(raw)


def fetch_opencode_version(
    base_url: str,
    *,
    timeout_seconds: float | None = 2,
    username: str = "opencode",
    password: str | None = None,
) -> str | None:
    try:
        data = probe_json(
            base_url,
            "/global/health",
            timeout_seconds=timeout_seconds,
            username=username,
            password=password,
        )
        if (
            isinstance(data, dict)
            and data.get("healthy") is True
            and isinstance(data.get("version"), str)
        ):
            return str(data["version"])
    except (httpx.HTTPError, OSError, ValueError):
        pass
    return None


def list_opencode_models(
    base_url: str,
    *,
    timeout_seconds: float | None = None,
    username: str = "opencode",
    password: str | None = None,
    directory: str | None = None,
) -> tuple[str, ...]:
    data = probe_json(
        base_url,
        "/config/providers",
        timeout_seconds=timeout_seconds,
        username=username,
        password=password,
        directory=directory,
    )
    if not isinstance(data, dict) or not isinstance(data.get("providers"), list):
        raise ValueError("Invalid provider catalog")
    metadata: dict[str, dict[str, Any]] = {}
    for provider in data["providers"]:
        if (
            not isinstance(provider, dict)
            or not isinstance(provider.get("id"), str)
            or not isinstance(provider.get("models"), dict)
        ):
            continue
        for key, model in provider["models"].items():
            if not isinstance(key, str) or not isinstance(model, dict):
                continue
            variants = model.get("variants", {})
            context = (
                model.get("limit", {}).get("context")
                if isinstance(model.get("limit"), dict)
                else None
            )
            metadata[f"{provider['id']}/{key}"] = {
                "source": "native_catalog",
                "reasoning_efforts": [
                    name
                    for name, options in variants.items()
                    if isinstance(options, dict) and options.get("reasoningEffort") == name
                ]
                if isinstance(variants, dict)
                else [],
                "context_window": context if type(context) is int and context > 0 else None,
            }
    return CatalogModels(metadata)


def decode_attachment(value: Any) -> bytes | None:
    if not isinstance(value, str) or len(value) > MAX_RESPONSE_BYTES:
        return None
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (ValueError, UnicodeError):
        return None
