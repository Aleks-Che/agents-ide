"""Bounded run facts; never include raw event payloads or request bodies."""

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from agents_ide.engine.run_configuration import effective_snapshot
from agents_ide.persistence.models import Run, RunEvent, StepAttempt

TIMELINE_TYPES = {
    "run.started",
    "run.paused",
    "run.stopped",
    "run.completed",
    "run.failed",
    "run.cancelled",
    "run.waiting",
    "run.resumed",
    "run.reconciling",
    "run.state_changed",
    "run.waiting_input",
    "node.entered",
    "node.left",
    "attempt.started",
    "attempt.finished",
    "attempt.late_result",
    "attempt.retry_scheduled",
    "error.reported",
    "error.technical",
    "model_group.candidate_switched",
    "model_group.exhausted",
    "agent.input_requested",
    "agent.input_closed",
    "process.interrupted",
    "git.commit_intent_saved",
    "git.commit_created",
    "git.commit_recovered",
    "control.requested",
    "control.applied",
}


def run_evidence(session: Session, run: Run, attempt: StepAttempt | None) -> dict[str, Any]:
    snapshot = effective_snapshot(run)
    node: dict[str, Any] = next(
        (
            node
            for node in snapshot.get("graph", {}).get("nodes", [])
            if node["id"] == run.current_node_id
        ),
        {},
    )
    selection = json.loads(attempt.selection_json or "{}") if attempt else {}
    events = list(
        session.scalars(
            select(RunEvent)
            .where(RunEvent.run_id == run.id, RunEvent.type.in_(TIMELINE_TYPES))
            .order_by(RunEvent.sequence.desc())
            .limit(11)
        )
    )
    latest = session.scalar(
        select(RunEvent)
        .where(RunEvent.run_id == run.id)
        .order_by(RunEvent.sequence.desc())
        .limit(1)
    )
    attempt_events = (
        list(
            session.scalars(
                select(RunEvent)
                .where(
                    RunEvent.run_id == run.id,
                    RunEvent.step_attempt_id == attempt.id,
                    RunEvent.type.in_(TIMELINE_TYPES),
                )
                .order_by(RunEvent.sequence.desc())
                .limit(11)
            )
        )
        if attempt
        else []
    )

    def event_metadata(event: RunEvent) -> dict[str, Any]:
        return {
            "sequence": event.sequence,
            "type": event.type,
            "occurred_at": event.occurred_at,
            "node_id": event.node_id,
            "attempt_id": event.step_attempt_id,
        }

    return {
        "stage": {"id": run.current_node_id, "type": node.get("type"), "label": node.get("label")},
        "stage_semantics": (
            "GitCommit выполняет сервер. Он может отдельно вызвать LLM для текста коммита, "
            "но model_id=null у Git-попытки не является пробелом диагностики защиты файлов."
            if node.get("type") == "GitCommit"
            else None
        ),
        "attempt": {
            "id": attempt.id if attempt else None,
            "started_at": attempt.started_at if attempt else None,
            "finished_at": attempt.finished_at if attempt else None,
            "external_outcome": attempt.external_outcome if attempt else None,
            "model_id": selection.get("model_id"),
        },
        "last_recorded_event": event_metadata(latest) if latest else None,
        "recent_events": [event_metadata(event) for event in reversed(events[:10])],
        "earlier_events_omitted": len(events) > 10,
        "current_attempt_events": [
            event_metadata(event) for event in reversed(attempt_events[:10])
        ],
        "earlier_attempt_events_omitted": len(attempt_events) > 10,
        "coverage": "Последние 10 событий состояния всего запуска, с идентификаторами попыток. "
        "Отдельно до 10 событий текущей попытки: поздние команды управления их не вытесняют. "
        "Показана текущая попытка, а не перечень всех попыток; нельзя называть её единственной. "
        "Тексты запросов, ответов и вызовов инструментов не переданы. Последнее событие "
        "и heartbeat не доказывают, что процесс жив сейчас; "
        "первопричина может быть не установлена.",
    }
