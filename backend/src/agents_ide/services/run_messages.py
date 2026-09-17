"""Durable, attempt-scoped input for a live agent; never replay uncertain delivery."""

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from agents_ide.domain.common import to_json, utc_now
from agents_ide.domain.schemas import RunCommand
from agents_ide.engine.artifacts import sanitize
from agents_ide.engine.events import append_event
from agents_ide.errors import AppError
from agents_ide.persistence.models import CommandJournal, Run, StepAttempt, StepExecution


def prepare_message(session: Session, run: Run, command: RunCommand) -> dict[str, Any]:
    payload = command.payload
    text = payload.get("text")
    attempt_id = payload.get("attempt_id")
    if (
        set(payload)
        - {"text", "attempt_id", "question_id", "answers", "permission_id", "permission_reply"}
        or not isinstance(text, str)
        or not text.strip()
        or len(text.encode()) > 8000
        or not isinstance(attempt_id, str)
    ):
        raise AppError("message_invalid", "Нужны текст до 8000 байт и текущая попытка агента", 422)
    if "question_id" in payload and (
        not isinstance(payload["question_id"], str) or len(payload["question_id"]) > 200
    ):
        raise AppError("message_invalid", "Некорректный вопрос агента", 422)
    if {"permission_id", "permission_reply"} & set(payload) and (
        not isinstance(payload.get("permission_id"), str)
        or not 1 <= len(payload["permission_id"]) <= 128
        or payload.get("permission_reply") not in ("once", "reject")
        or {"question_id", "answers"} & set(payload)
    ):
        raise AppError("message_invalid", "Некорректное решение о разрешении", 422)
    if "answers" in payload and (
        not isinstance(payload["answers"], list)
        or not 1 <= len(payload["answers"]) <= 8
        or any(
            not isinstance(answer, list)
            or not answer
            or any(not isinstance(value, str) or not value.strip() for value in answer)
            for answer in payload["answers"]
        )
        or len(to_json(payload["answers"]).encode()) > 8000
    ):
        raise AppError("message_invalid", "Заполните ответы на вопросы агента", 422)
    attempt = session.get(StepAttempt, attempt_id)
    node: dict[str, Any] = next(
        (
            n
            for n in json.loads(run.snapshot_json)["graph"]["nodes"]
            if n["id"] == run.current_node_id
        ),
        {},
    )
    if (
        run.state != "running"
        or run.current_attempt_id != attempt_id
        or attempt is None
        or attempt.status != "running"
        or node.get("type") != "AgentTask"
    ):
        raise AppError(
            "message_target_changed", "Этап уже сменился. Обновите состояние задания", 409
        )
    append_event(
        session,
        run.id,
        "agent.user_message",
        {
            "text": sanitize(text.strip()),
            "question_id": payload.get("question_id"),
            "permission_id": payload.get("permission_id"),
            "permission_reply": payload.get("permission_reply"),
            "delivery": "queued",
        },
        node_id=run.current_node_id,
        execution_id=run.current_execution_id,
        attempt_id=attempt_id,
        command_id=command.command_id,
    )
    return {"attempt_id": attempt_id, "delivery": "queued"}


def claim_message(session: Session, run: Run, attempt_id: str) -> dict[str, Any] | None:
    if run.state != "running" or run.current_attempt_id != attempt_id:
        return None
    for row in session.scalars(
        select(CommandJournal)
        .where(
            CommandJournal.run_id == run.id,
            CommandJournal.command_type == "message",
            CommandJournal.status == "accepted",
        )
        .order_by(CommandJournal.sequence)
    ):
        response = json.loads(row.response_json or "{}")
        if response.get("attempt_id") == attempt_id and response.get("delivery") == "queued":
            row.response_json = to_json({**response, "delivery": "sending"})
            return {**json.loads(row.payload_json or "{}"), "command_id": row.command_id}
    return None


def finish_message(session: Session, run: Run, attempt_id: str, payload: dict[str, Any]) -> None:
    row = session.scalar(
        select(CommandJournal).where(
            CommandJournal.run_id == run.id,
            CommandJournal.command_id == payload.get("command_id"),
            CommandJournal.command_type == "message",
            CommandJournal.status == "accepted",
        )
    )
    if row is None or json.loads(row.response_json or "{}").get("attempt_id") != attempt_id:
        return
    delivered = payload.get("delivered") is True
    row.status = "applied" if delivered else "rejected"
    row.applied_at = utc_now()
    row.response_json = to_json(
        {
            "attempt_id": attempt_id,
            "delivery": "delivered" if delivered else "failed",
            "reason": payload.get("reason"),
        }
    )
    attempt = session.get(StepAttempt, attempt_id)
    execution = session.get(StepExecution, attempt.execution_id) if attempt else None
    append_event(
        session,
        run.id,
        "agent.user_message_status",
        json.loads(row.response_json),
        node_id=execution.node_id if execution else None,
        execution_id=execution.id if execution else None,
        attempt_id=attempt_id,
        command_id=row.command_id,
    )


def close_messages(session: Session, run: Run) -> None:
    for row in session.scalars(
        select(CommandJournal).where(
            CommandJournal.run_id == run.id,
            CommandJournal.command_type == "message",
            CommandJournal.status == "accepted",
        )
    ):
        response = json.loads(row.response_json or "{}")
        finish_message(
            session,
            run,
            response.get("attempt_id", ""),
            {
                "command_id": row.command_id,
                "delivered": False,
                "reason": "delivery_unknown"
                if response.get("delivery") == "sending"
                else "stage_finished",
            },
        )
