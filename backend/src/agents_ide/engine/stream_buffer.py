"""Coalesce streaming telemetry before its fenced, durable transaction.

The caller supplies periodic flush_due calls and closes the buffer before saving
the attempt result. Control/session/tool events are synchronous flush boundaries.
No timer threads or database connections are allocated by the buffer.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from agents_ide.adapters.history import OMITTED_HISTORY_EVENTS
from agents_ide.domain.common import to_json

ProgressBatch = list[tuple[str, dict[str, Any]]]
BLOCK_BYTES = 64 * 1024
BATCH_BYTES = 4 * BLOCK_BYTES
FLUSH_SECONDS = 2.0


@dataclass
class _Chunk:
    kind: str
    body: dict[str, Any]
    parent: dict[str, Any]
    field: str
    signature: dict[str, Any]
    snapshot: bool = False
    count: int = 1
    size: int = 0


def _chunk(kind: str, payload: dict[str, Any]) -> _Chunk | None:
    path: tuple[str, ...] = ()
    snapshot = False
    if kind == "attempt.text_delta":
        path = ("text",)
    elif kind == "agent.output_delta":
        path = ("details", "delta")
    elif kind == "agent.native_event":
        native = payload.get("native_type", "")
        if not isinstance(native, str):
            return None
        if payload.get("harness") == "codex" and native.endswith("/delta"):
            path = ("payload", "params", "delta")
        elif payload.get("harness") == "opencode":
            if native == "message.part.delta":
                path = ("payload", "properties", "delta")
            elif native == "message.part.updated":
                envelope = payload.get("payload")
                if not isinstance(envelope, dict):
                    return None
                props = envelope.get("properties")
                if not isinstance(props, dict):
                    return None
                part = props.get("part", {})
                if isinstance(part, dict) and part.get("type") in {"text", "reasoning"}:
                    # A completed part is a flush boundary, including its final snapshot.
                    if (part.get("time") or {}).get("end") is not None:
                        return None
                    snapshot = not isinstance(props.get("delta"), str)
                    path = (
                        ("payload", "properties", "part", "text")
                        if snapshot
                        else ("payload", "properties", "delta")
                    )
    if not path:
        return None
    body = deepcopy(payload)
    signature = deepcopy(payload)
    parent, identity = body, signature
    for key in path[:-1]:
        if not isinstance(parent.get(key), dict):
            return None
        parent, identity = parent[key], identity[key]
    field = path[-1]
    if not isinstance(parent.get(field), str):
        return None
    identity.pop(field)
    if kind == "agent.native_event" and payload.get("native_type") == "message.part.updated":
        part_identity = signature["payload"]["properties"]["part"]
        part_identity.pop("text", None)
        part_identity.pop("time", None)
    return _Chunk(kind, body, parent, field, signature, snapshot)


class StreamEventBuffer:
    def __init__(
        self,
        persist: Callable[[ProgressBatch], None],
        *,
        clock: Callable[[], float] = time.monotonic,
        record_tool_history: bool = True,
    ) -> None:
        self._persist = persist
        self._clock = clock
        self._record_tool_history = record_tool_history
        self._lock = threading.RLock()
        self._pending: list[_Chunk] = []
        self._last: dict[str, int] = {}
        self._bytes = 0
        self._since: float | None = None
        self._closed = False
        self._error: Exception | None = None

    def emit(self, kind: str, payload: dict[str, Any]) -> None:
        with self._lock:
            if self._closed:
                return  # Detached late calls cannot write into a subsequent attempt.
            if self._error:
                raise self._error
            if not self._record_tool_history and kind in OMITTED_HISTORY_EVENTS:
                if kind == "agent.tool_call":
                    self._flush()
                return
            self.flush_due()
            chunk = _chunk(kind, payload)
            if chunk is None:
                self._flush([(kind, payload)])
                return
            previous = self._last.get(kind)
            if previous is not None:
                old = self._pending[previous]
                if chunk.signature == old.signature and chunk.snapshot == old.snapshot:
                    if not chunk.snapshot:
                        chunk.parent[chunk.field] = (
                            old.parent[old.field] + chunk.parent[chunk.field]
                        )
                    chunk.count = old.count + 1
                    chunk.body["coalesced_chunks"] = chunk.count
                    chunk.size = len(to_json(chunk.body).encode("utf-8"))
                    if chunk.size <= BLOCK_BYTES:
                        self._pending[previous] = chunk
                        self._bytes += chunk.size - old.size
                        if self._bytes >= BATCH_BYTES:
                            self._flush()
                        return
                    # Do not turn a readable text event into an oversized artifact.
                    self._flush()
                    chunk = _chunk(kind, payload)
                    assert chunk is not None
            chunk.size = len(to_json(chunk.body).encode("utf-8"))
            if self._pending and self._bytes + chunk.size > BATCH_BYTES:
                self._flush()
            if self._since is None:
                self._since = self._clock()
            self._last[kind] = len(self._pending)
            self._pending.append(chunk)
            self._bytes += chunk.size
            if chunk.size >= BLOCK_BYTES or self._bytes >= BATCH_BYTES:
                self._flush()

    def flush_due(self) -> None:
        with self._lock:
            if self._error:
                raise self._error
            if self._since is not None and self._clock() - self._since >= FLUSH_SECONDS:
                self._flush()

    def flush(self) -> None:
        with self._lock:
            self._flush()

    def close(self) -> None:
        with self._lock:
            try:
                self._flush()
            finally:
                self._closed = True

    def _flush(self, boundary: ProgressBatch | None = None) -> None:
        if self._error:
            raise self._error
        batch = [(chunk.kind, chunk.body) for chunk in self._pending]
        batch.extend(boundary or [])
        if not batch:
            return
        try:
            self._persist(batch)
        except Exception as exc:
            # A failed write must not be hidden by a later successful adapter result.
            self._error = exc
            raise
        finally:
            self._pending.clear()
            self._last.clear()
            self._bytes = 0
            self._since = None
