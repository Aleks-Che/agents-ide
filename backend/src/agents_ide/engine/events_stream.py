"""Consistent event reads, replay and one bounded fan-out poller per active Run."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from agents_ide.domain.planning import PlanningProvenance
from agents_ide.domain.schemas import Run as RunSchema
from agents_ide.engine.events import EventEnvelope
from agents_ide.errors import AppError
from agents_ide.persistence.models import Run, RunEvent
from agents_ide.services.run_observation import RunObservation
from agents_ide.services.run_selection import SelectionSummary

MAX_EVENTS_PER_BATCH = 200
MAX_BUFFER_BYTES = 1024 * 1024


class EventBatchResponse(BaseModel):
    events: list[EventEnvelope]
    last_sequence: int
    reset_required: bool = False
    final_state: str | None = None
    has_more: bool = False
    min_retained_sequence: int = 0


class RunSnapshot(BaseModel):
    run: RunSchema
    last_sequence: int
    min_retained_sequence: int
    selection: SelectionSummary | None = None
    planning_provenance: PlanningProvenance | None = None
    observation: RunObservation | None = None


class ArtifactView(BaseModel):
    id: str
    run_id: str
    schema_type: str
    source_kind: str
    byte_length: int
    content_hash: str
    step_execution_id: str | None
    step_attempt_id: str | None
    body: Any = None
    redaction: list[str]
    truncation: dict[str, Any] | None


class ArtifactContent(BaseModel):
    artifact: ArtifactView
    text: str
    offset: int
    total_chars: int


@dataclass(frozen=True)
class EventBatch:
    events: list[dict[str, Any]]
    last_sequence: int
    reset_required: bool = False
    final_state: str | None = None
    has_more: bool = False
    min_retained_sequence: int = 0


def _read_transaction(session: Session) -> None:
    if not session.in_transaction():
        session.connection().exec_driver_sql("BEGIN")


def fetch_events_after(
    session: Session, run_id: str, *, after_sequence: int, limit: int = MAX_EVENTS_PER_BATCH
) -> EventBatch:
    _read_transaction(session)
    run = session.get(Run, run_id)
    if run is None:
        raise AppError("run_not_found", "Run не найден", 404)
    minimum, highest = session.execute(
        select(func.min(RunEvent.sequence), func.max(RunEvent.sequence)).where(
            RunEvent.run_id == run_id
        )
    ).one()
    highest, minimum = int(highest or 0), int(minimum or 0)
    minimum = max(minimum, run.retention_sequence)
    if after_sequence > highest or (minimum and after_sequence < minimum - 1):
        return EventBatch([], highest, True, run.state, False, minimum)
    rows = session.scalars(
        select(RunEvent)
        .where(RunEvent.run_id == run_id, RunEvent.sequence > after_sequence)
        .order_by(RunEvent.sequence)
        .limit(min(limit, 1000))
    )
    batch: list[dict[str, Any]] = []
    size = 0
    for row in rows:
        event = _serialize_event(row)
        encoded_size = len(json.dumps(event, ensure_ascii=False).encode("utf-8")) + 64
        if batch and size + encoded_size > MAX_BUFFER_BYTES // 2:
            break
        size += encoded_size
        batch.append(event)
    last = batch[-1]["sequence"] if batch else after_sequence
    return EventBatch(batch, last, False, run.state, last < highest, minimum)


def fetch_full_history(
    session: Session, run_id: str, *, limit: int = MAX_EVENTS_PER_BATCH
) -> EventBatch:
    """First retained page; subsequent pages use its cursor without gaps."""
    _read_transaction(session)
    minimum = (
        session.scalar(select(func.min(RunEvent.sequence)).where(RunEvent.run_id == run_id)) or 1
    )
    run = session.get(Run, run_id)
    minimum = max(minimum, run.retention_sequence if run else 0)
    return fetch_events_after(session, run_id, after_sequence=minimum - 1, limit=limit)


def is_terminal(state: str) -> bool:
    return state in {"completed", "failed", "cancelled"}


def format_sse(events: Iterable[dict[str, Any]], *, event_name: str = "run.event") -> list[str]:
    return [
        f"id: {event['sequence']}\nevent: {event_name}\n"
        f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        for event in events
    ]


def _serialize_event(row: RunEvent) -> dict[str, Any]:
    return {
        key: value
        for key, value in {
            "event_version": row.event_version,
            "run_id": row.run_id,
            "sequence": row.sequence,
            "type": row.type,
            "occurred_at": row.occurred_at,
            "persisted_at": row.persisted_at,
            "node_id": row.node_id,
            "step_execution_id": row.step_execution_id,
            "step_attempt_id": row.step_attempt_id,
            "agent_session_id": row.agent_session_id,
            "command_id": row.command_id,
            "worker_generation": row.worker_generation,
            "payload": json.loads(row.payload_json),
        }.items()
        if value is not None
    }


@dataclass(eq=False)
class Subscription:
    queue: asyncio.Queue[EventBatch] = field(default_factory=asyncio.Queue)
    buffered_bytes: int = 0
    overflow: bool = False

    def push(self, batch: EventBatch) -> None:
        if self.overflow:
            return
        size = len(json.dumps(batch.events, ensure_ascii=False).encode("utf-8")) + 128
        if self.buffered_bytes + size > MAX_BUFFER_BYTES:
            self.overflow = True
            while not self.queue.empty():
                self.queue.get_nowait()
            self.buffered_bytes = 0
            self.queue.put_nowait(EventBatch([], batch.last_sequence, True, batch.final_state))
            return
        self.buffered_bytes += size
        self.queue.put_nowait(batch)

    async def get(self) -> EventBatch:
        batch = await self.queue.get()
        self.buffered_bytes = max(
            0,
            self.buffered_bytes
            - len(json.dumps(batch.events, ensure_ascii=False).encode("utf-8"))
            - 128,
        )
        return batch


class StreamHub:
    def __init__(self, factory: sessionmaker[Session]) -> None:
        self.factory = factory
        self.subscribers: dict[str, set[Subscription]] = {}
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.lock = asyncio.Lock()

    def read(self, run_id: str, after: int) -> EventBatch:
        with self.factory() as session:
            return fetch_events_after(session, run_id, after_sequence=after)

    def _cursor(self, run_id: str) -> int:
        with self.factory() as session:
            return int(
                session.scalar(select(func.max(RunEvent.sequence)).where(RunEvent.run_id == run_id))
                or 0
            )

    async def subscribe(self, run_id: str) -> Subscription:
        async with self.lock:
            subscription = Subscription()
            self.subscribers.setdefault(run_id, set()).add(subscription)
            if run_id not in self.tasks:
                cursor = await asyncio.to_thread(self._cursor, run_id)
                self.tasks[run_id] = asyncio.create_task(self._poll(run_id, cursor))
            return subscription

    async def unsubscribe(self, run_id: str, subscription: Subscription) -> None:
        subscribers = self.subscribers.get(run_id, set())
        subscribers.discard(subscription)
        if not subscribers:
            self.subscribers.pop(run_id, None)
            task = self.tasks.pop(run_id, None)
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    async def _poll(self, run_id: str, cursor: int) -> None:
        delay = 0.2
        while self.subscribers.get(run_id):
            try:
                batch = await asyncio.to_thread(self.read, run_id, cursor)
            except Exception:
                batch = EventBatch([], cursor, True)
            if batch.events or batch.reset_required or is_terminal(batch.final_state or ""):
                for subscriber in self.subscribers.get(run_id, set()):
                    subscriber.push(batch)
                cursor = batch.last_sequence
            if batch.reset_required or (
                is_terminal(batch.final_state or "") and not batch.has_more
            ):
                return
            delay = 0.2 if batch.events else min(delay * 2, 0.5)
            await asyncio.sleep(0 if batch.has_more else delay)

    async def close(self) -> None:
        for run_id, subscribers in list(self.subscribers.items()):
            for subscriber in list(subscribers):
                await self.unsubscribe(run_id, subscriber)
