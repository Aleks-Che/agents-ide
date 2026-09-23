"""Narrow assistant actions, executed only after a concrete UI confirmation."""

import hashlib
from typing import Any, Literal

from pydantic import Field, model_validator
from sqlalchemy.orm import Session

from agents_ide.domain.schemas import ApiModel, ApiOutput, RunCommand
from agents_ide.errors import AppError
from agents_ide.logging import redact
from agents_ide.persistence.models import Chat, Project, Run
from agents_ide.services.assistance_catalog import AssistanceTarget
from agents_ide.services.mapping import get_or_404
from agents_ide.services.runs import submit_command
from agents_ide.services.transactions import begin_write

ToolName = Literal[
    "accept_git_head_and_resume",
    "accept_git_files_and_resume",
    "refresh_commit_connection_and_resume",
]


class AssistanceTool(ApiOutput):
    name: ToolName
    run_id: str
    title: str
    description: str
    confirmation_label: str
    requires_confirmation: Literal[True] = True


def git_acceptance_tool(run_id: str, title: str, kind: str | None) -> AssistanceTool | None:
    if kind == "head":
        return AssistanceTool(
            name="accept_git_head_and_resume",
            run_id=run_id,
            title=title,
            description="Сравнить HEAD, после подтверждения принять его как основу следующего "
            "этапа и продолжить запуск с повторной проверкой рабочей области.",
            confirmation_label="Принять HEAD и продолжить",
        )
    if kind == "protected_files":
        return AssistanceTool(
            name="accept_git_files_and_resume",
            run_id=run_id,
            title=title,
            description="Сравнить защищённые файлы, после выбора и подтверждения принять их "
            "текущее состояние и продолжить GitCommit с повторными проверками. "
            "Принятие обновляет эталон проверки; игнорируемые файлы в коммит не добавляются.",
            confirmation_label="Принять выбранные изменения и продолжить",
        )
    return None


class AssistanceToolRequest(ApiModel):
    tool: ToolName
    target: AssistanceTarget
    run_id: str = Field(min_length=1, max_length=32)
    comparison_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_state_version: int = Field(ge=0)
    paths: list[str] = Field(default_factory=list, max_length=100)
    acknowledge_risk: Literal[True]
    confirm_resume: Literal[True]

    @model_validator(mode="after")
    def validate_selection(self) -> "AssistanceToolRequest":
        if self.tool != "accept_git_files_and_resume" and self.paths:
            raise ValueError("Этот инструмент не поддерживает выбор файлов")
        if self.tool == "accept_git_files_and_resume" and (
            not self.paths or len(set(self.paths)) != len(self.paths)
        ):
            raise ValueError("Выберите конкретные файлы без повторений")
        return self


class AssistanceToolResult(ApiOutput):
    tool: ToolName
    run_id: str
    state: str
    content: str
    command_ids: list[str]
    accepted_paths: list[str] = Field(default_factory=list)


def execute(session: Session, payload: AssistanceToolRequest) -> AssistanceToolResult:
    # Both commands share the API transaction and write lock. A failed resume
    # rolls back acceptance too; workers cannot observe a half-applied action.
    begin_write(session)
    run = get_or_404(session, Run, payload.run_id)
    target = payload.target
    if (
        target.zone not in {"project", "chat", "runs"}
        or target.project_id != run.project_id
        or (target.zone == "chat" and target.chat_id != run.chat_id)
    ):
        raise AppError("assistance_scope_invalid", "Запуск не относится к выбранной теме.", 422)
    project = get_or_404(session, Project, run.project_id)
    if project.archived_at is not None:
        raise AppError("project_archived", "Проект архивирован", 409)
    if run.chat_id and get_or_404(session, Chat, run.chat_id).archived_at is not None:
        raise AppError("chat_archived", "Диалог архивирован", 409)

    # Stable IDs permit replay after a lost HTTP response without executing the
    # commands again. Original versions are preserved even on a later replay.
    identity = f"assistance:{payload.tool}:{run.id}:{payload.comparison_id}"
    command_ids = [
        hashlib.sha256(f"{identity}:{kind}".encode()).hexdigest() for kind in ("resolve", "resume")
    ]
    decision: dict[str, Any] = {
        "comparison_id": payload.comparison_id,
        "accept_head": payload.tool == "accept_git_head_and_resume",
        "acknowledge_risk": True,
    }
    if payload.tool == "accept_git_files_and_resume":
        decision["paths"] = sorted(payload.paths)
    resolution = {"git_changes": decision}
    if payload.tool == "refresh_commit_connection_and_resume":
        resolution = {
            "commit_connection": {
                "comparison_id": payload.comparison_id,
                "acknowledge_risk": True,
            }
        }
    accepted = submit_command(
        session,
        run.id,
        RunCommand(
            command_id=command_ids[0],
            command_type="resolve",
            expected_state_version=payload.expected_state_version,
            payload=resolution,
        ),
        initiator="assistance",
    )
    acceptance = accepted.response or {}
    if acceptance.get("remaining_changes", 0):
        raise AppError(
            "assistance_git_changes_remaining",
            "Остались непринятые защищённые изменения. Для принятия с продолжением "
            "нужно проверить и выбрать все расхождения. Решение не применено.",
            409,
            {"remaining_changes": acceptance["remaining_changes"]},
        )
    submit_command(
        session,
        run.id,
        RunCommand(
            command_id=command_ids[1],
            command_type="resume",
            expected_state_version=payload.expected_state_version + 1,
        ),
        initiator="assistance",
    )
    accepted_paths = acceptance.get("accepted_paths", [])
    if payload.tool == "refresh_commit_connection_and_resume":
        return AssistanceToolResult(
            tool=payload.tool,
            run_id=run.id,
            state=run.state,
            command_ids=command_ids,
            content=(
                "Инструмент помощника обновил ожидаемую версию подключения для сообщения "
                "GitCommit и применил команду «Продолжить». Модель, адрес, ключ и результаты "
                "предыдущих этапов сохранены. При продолжении Git-проверки выполнятся снова; "
                "затем возможны запрос сообщения к провайдеру и создание коммита. "
                "Это ещё не подтверждение успешного коммита. "
                f"Текущее состояние запуска: {run.state}."
            ),
        )
    change = (
        "проверенное изменение HEAD"
        if payload.tool == "accept_git_head_and_resume"
        else "изменения защищённых файлов: " + str(redact(", ".join(accepted_paths)))
    )
    return AssistanceToolResult(
        tool=payload.tool,
        run_id=run.id,
        state=run.state,
        command_ids=command_ids,
        accepted_paths=accepted_paths,
        content=(
            f"Инструмент помощника принял {change} и применил команду "
            "«Продолжить». Содержимое файлов, индекс и коммиты при принятии не изменялись. "
            "Это подтверждение управляющих действий, а не завершения следующего этапа. "
            f"Текущее состояние запуска: {run.state}."
        ),
    )
