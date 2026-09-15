"""Bounded OpenCode HTTP/SSE transport; protocol fields come from server /doc.

Only sessions with all native tools denied are supported until write isolation
is independently verified. Catalog membership is never proof of provider access.
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

from agents_ide.adapters.base import (
    AdapterError,
    AgentAdapter,
    AgentAdapterRequest,
    AgentResult,
    ExternalOutcome,
)
from agents_ide.errors import AppError

MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
MAX_EVENT_BYTES = 64 * 1024
HEALTH_TIMEOUT_SECONDS = OPENCODE_HEALTH_TIMEOUT = 2.0
STARTUP_TIMEOUT_SECONDS = 30.0
DENY_TOOLS = [{"permission": "*", "pattern": "*", "action": "deny"}]
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


class OpenCodeConfigurationError(AppError):
    def __init__(self, message: str) -> None:
        super().__init__("configuration_invalid", message, status=422)


class ResponseLimit(ValueError):
    pass


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
    client: httpx.Client, method: str, path: str, *, limit: int = MAX_RESPONSE_BYTES, **kwargs: Any
) -> tuple[httpx.Response, bytes]:
    with client.stream(method, path, **kwargs) as response:
        body = bytearray()
        for chunk in response.iter_bytes():
            if len(body) + len(chunk) > limit:
                raise ResponseLimit("OpenCode response exceeds limit")
            body.extend(chunk)
        return response, bytes(body)


def sse_events(chunks: Iterable[bytes]) -> Iterator[dict[str, Any]]:
    """Incremental UTF-8 SSE parsing, including split frames and multiline data."""
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
            if size > MAX_EVENT_BYTES:
                raise ResponseLimit("OpenCode event exceeds limit")
            if not line:
                if data:
                    value = json.loads(b"\n".join(data))
                    if isinstance(value, dict) and isinstance(value.get("type"), str):
                        yield value
                data, size = [], 0
            elif line.startswith(b"data:"):
                data.append(bytes(line[5:].removeprefix(b" ")))
        if size + len(buffer) > MAX_EVENT_BYTES:
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
        startup_timeout: float = STARTUP_TIMEOUT_SECONDS,
        timeout_seconds: float = 300,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
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
            timeout=httpx.Timeout(timeout or self.timeout_seconds, connect=2, pool=2),
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
        started = time.monotonic()
        sent = False
        done, ready, permission = threading.Event(), threading.Event(), threading.Event()
        errors: list[Exception] = []
        threads: list[threading.Thread] = []
        deadline = min(request.deadline_at or float("inf"), time.time() + self.timeout_seconds)

        def fail(code: str, outcome: ExternalOutcome, *, safe: bool = False) -> AgentResult:
            return AgentResult(
                outcome,
                "",
                None,
                None,
                error=AdapterError(code, code, "safe" if safe else "unknown"),
                no_effect=safe,
                elapsed_seconds=time.monotonic() - started,
            )

        def emit(kind: str, payload: dict[str, Any]) -> None:
            if request.emit_event:
                request.emit_event(kind, payload)

        def check() -> None:
            if request.check_owned:
                request.check_owned()
            if (request.stop_event and request.stop_event.is_set()) or time.time() >= deadline:
                raise InterruptedError

        try:
            check()
            body = self._build_message(request)
            with self.client(
                timeout=min(self.startup_timeout, max(0.1, deadline - time.time()))
            ) as client:
                # Resume only the exact engine-selected session. Check before sending a message;
                # a 404 returned after dispatch is not proof of zero model/tool execution.
                resume = request.resume_session_id or self.external_session_id
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
                            "permission": DENY_TOOLS,
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
                if sid != self.external_session_id:
                    return
                kind = event["type"]
                if kind == "permission.asked":
                    permission.set()
                    permission_id = validate_id(props.get("id"))
                    emit(
                        "agent.permission_requested",
                        {
                            "session_id": sid,
                            "permission_id": permission_id,
                            "permission": props.get("permission"),
                        },
                    )
                    with self.client(timeout=2) as event_client:
                        reply, _ = bounded_request(
                            event_client,
                            "POST",
                            f"/permission/{permission_id}/reply",
                            json={"reply": "reject"},
                        )
                    if reply.status_code == 200:
                        emit(
                            "agent.permission_resolved",
                            {
                                "session_id": sid,
                                "permission_id": permission_id,
                                "reply": "reject",
                            },
                        )
                    self.interrupt(str(sid))
                elif kind == "message.part.delta" and props.get("field") == "text":
                    delta = props.get("delta")
                    if isinstance(delta, str):
                        emit("attempt.text_delta", {"text": delta, "session_id": sid})
                elif kind == "message.part.updated" and isinstance(part, dict):
                    if part.get("type") == "tool":
                        emit("agent.tool_call", {"session_id": sid, "part": part})
                    elif isinstance(props.get("delta"), str):
                        emit("attempt.text_delta", {"text": props["delta"], "session_id": sid})
                elif kind == "message.updated" and isinstance(info, dict):
                    emit(
                        "attempt.progress",
                        {
                            "session_id": sid,
                            "message_id": validate_id(info.get("id")),
                            "role": info.get("role"),
                        },
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
                            timeout=httpx.Timeout(20, connect=2),
                        ) as client,
                        client.stream("GET", "/event") as response,
                    ):
                        if response.status_code != 200:
                            raise ValueError("Event stream rejected")
                        pending = bytearray()
                        total = 0
                        async for chunk in response.aiter_bytes():
                            pending.extend(chunk)
                            total += len(chunk)
                            if total > MAX_RESPONSE_BYTES:
                                raise ResponseLimit("Event stream budget exceeded")
                            while True:
                                lf, crlf = pending.find(b"\n\n"), pending.find(b"\r\n\r\n")
                                positions = [(lf, 2), (crlf, 4)]
                                positions = [(pos, size) for pos, size in positions if pos >= 0]
                                if not positions:
                                    break
                                pos, size = min(positions)
                                frame = bytes(pending[: pos + size])
                                del pending[: pos + size]
                                for event in sse_events([frame]):
                                    if event["type"] == "server.connected":
                                        ready.set()
                                    else:
                                        handle_event(event)
                            if len(pending) > MAX_EVENT_BYTES:
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
                while not done.wait(0.05):
                    try:
                        check()
                        if errors:
                            raise InterruptedError
                    except (AppError, InterruptedError):
                        self.interrupt(self.external_session_id or "")
                        return

            threads = [
                threading.Thread(target=stream, daemon=True),
                threading.Thread(target=watch, daemon=True),
            ]
            for thread in threads:
                thread.start()
            if not ready.wait(min(3, max(0, deadline - time.time()))) or errors:
                return fail(
                    "event_stream_unavailable", ExternalOutcome.RETRYABLE_FAILURE, safe=True
                )
            check()
            with self.client(timeout=max(0.1, deadline - time.time())) as client:
                sent = True
                response, raw = bounded_request(
                    client,
                    "POST",
                    f"/session/{resume}/message",
                    json=body,
                    limit=self.max_response_bytes,
                )
            if response.status_code != 200:
                return self._http_failure(response.status_code, dispatched=True)
            if permission.is_set():
                return fail("permission_denied", ExternalOutcome.PERMISSION_DENIED)
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
                return fail("interrupted", ExternalOutcome.RETRYABLE_FAILURE, safe=True)
            if info.get("error"):
                # Provider errors can follow useful tools/text; do not guess no_effect.
                return fail("provider_result_unknown", ExternalOutcome.UNKNOWN)
            if info.get("finish") not in {"stop", "end_turn", "length"}:
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
            usage = info.get("tokens", {})
            metrics = []
            if isinstance(usage, dict):
                metrics = [usage.get(k) for k in ("input", "output", "reasoning")]
                cache = usage.get("cache", {})
                metrics += (
                    [cache.get(k) for k in ("read", "write")] if isinstance(cache, dict) else [None]
                )
            tokens = (
                sum(v for v in metrics if isinstance(v, int))
                if metrics and all(type(v) is int and v >= 0 for v in metrics)
                else None
            )
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

    def _validate_session(self, data: Any, expected: str) -> None:
        if not isinstance(data, dict) or data.get("id") != expected:
            raise ValueError("Session identity mismatch")
        if data.get("permission") != DENY_TOOLS:
            raise OpenCodeConfigurationError("Server did not confirm deny-all tool permissions")
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
        if request.params:
            raise OpenCodeConfigurationError(
                "OpenCode model parameters require a verified capability mapping"
            )
        body = {
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
        if len(json.dumps(body, ensure_ascii=False).encode("utf-8")) > MAX_REQUEST_BYTES:
            raise OpenCodeConfigurationError("OpenCode request exceeds limit")
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
    timeout_seconds: float = 2,
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
    timeout_seconds: float = 2,
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
    timeout_seconds: float = 2,
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
    return tuple(
        sorted(
            {
                f"{p['id']}/{m}"
                for p in data["providers"]
                if isinstance(p, dict)
                and isinstance(p.get("id"), str)
                and isinstance(p.get("models"), dict)
                for m in p["models"]
                if isinstance(m, str)
            }
        )
    )


def decode_attachment(value: Any) -> bytes | None:
    if not isinstance(value, str) or len(value) > MAX_RESPONSE_BYTES:
        return None
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (ValueError, UnicodeError):
        return None
