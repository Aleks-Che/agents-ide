"""Review revision drift without changing a commit's pinned execution settings."""

import hashlib
import json
import time
from typing import Any, Literal

from pydantic import Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from agents_ide.domain.common import to_json
from agents_ide.domain.schemas import ApiModel, ApiOutput, RunCommand
from agents_ide.engine import artifacts
from agents_ide.engine.run_configuration import (
    commit_connection_source,
    commit_message_configuration,
    effective_snapshot,
)
from agents_ide.errors import AppError
from agents_ide.persistence.models import (
    ArtifactManifest,
    ProviderConnection,
    QueueJob,
    Run,
    StepAttempt,
    StepExecution,
)
from agents_ide.services.mapping import get_or_404

EXECUTION_FIELDS = ("base_url", "protocol", "provider_kind", "secret_reference")
EFFECT = (
    "Обновится только ожидаемая версия подключения для сообщения этого GitCommit. "
    "Модель, адрес, ключ, инструкции и остальные этапы сохранятся. После подтверждения "
    "запуск продолжится: повторит Git-проверки и сможет отправить diff прежнему "
    "провайдеру для генерации сообщения и создать коммит. Результаты предыдущих этапов "
    "не пересчитываются. Это не подтверждение успешного коммита."
)


class CommitConnectionReview(ApiOutput):
    run_id: str
    node_id: str | None
    state_version: int
    comparison_id: str
    reviewed_at: float
    connection_name: str | None
    model: str | None
    expected_version: int | None
    current_version: int | None
    previous_guard_attempt_id: str | None = None
    changed_fields: list[str]
    execution_settings_match: bool
    can_accept: bool
    blockers: list[str]
    assessment: str
    effect: str = EFFECT
    checks_performed: list[str]
    checks_not_performed: list[str] = Field(
        default_factory=lambda: [
            "git_workspace",
            "provider_access",
            "secret_value",
            "commit_result",
        ]
    )


class CommitConnectionDecision(ApiModel):
    comparison_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    acknowledge_risk: Literal[True]


def can_review(run: Run) -> bool:
    waiting = json.loads(run.waiting_reason_json or "{}")
    snapshot = effective_snapshot(run)
    return (
        run.state == "waiting_input"
        and waiting.get("code") == "configuration_invalid"
        and waiting.get("details", {}).get("reason") == "resource_changed"
        and any(
            node["id"] == run.current_node_id and node["type"] == "GitCommit"
            for node in snapshot["graph"]["nodes"]
        )
        and bool(
            snapshot["dependencies"]["nodes"].get(run.current_node_id, {}).get("generate_message")
        )
    )


def review_connection(
    session: Session, run_id: str, *, verify_processes: bool = True
) -> CommitConnectionReview:
    from agents_ide.services.run_controls import unsettled_attempts
    from agents_ide.worker.processes import stored_processes_stopped

    run = get_or_404(session, Run, run_id)
    snapshot, runtime = effective_snapshot(run), json.loads(run.runtime_json)
    blockers: list[str] = []
    if not can_review(run):
        blockers.append("Это не остановка GitCommit из-за изменения подключения.")
    generation = (
        commit_message_configuration(snapshot, runtime, run.current_node_id)
        if run.current_node_id is not None
        and run.current_node_id in snapshot["dependencies"]["nodes"]
        else None
    ) or {}
    ref = generation.get("connection_id")
    pinned = snapshot["dependencies"].get("provider_connections", {}).get(ref, {})
    resource = session.get(ProviderConnection, ref) if ref else None
    current = (
        {
            "id": resource.id,
            "version": resource.version,
            "archived": resource.archived_at is not None,
            **{field: getattr(resource, field) for field in EXECUTION_FIELDS},
        }
        if resource
        else {}
    )
    changed = [field for field in EXECUTION_FIELDS if pinned.get(field) != current.get(field)]
    matches = bool(resource and set(EXECUTION_FIELDS) <= pinned.keys() and not changed)
    if not resource or resource.archived_at is not None:
        blockers.append(
            "Подключение удалено или архивировано. Выберите доступное в настройках этапа."
        )
    if not matches:
        blockers.append(
            "Исполняемые настройки подключения отличаются или исходные данные неполны. "
            "Обновлять только версию нельзя: проверьте подключение и настройки этапа."
        )
    target = json.loads(run.resume_target_json or "{}")
    attempt = session.get(StepAttempt, run.current_attempt_id) if run.current_attempt_id else None
    latest_attempt_id = (
        session.scalar(
            select(StepAttempt.id)
            .where(StepAttempt.execution_id == run.current_execution_id)
            .order_by(StepAttempt.attempt_index.desc())
            .limit(1)
        )
        if run.current_execution_id
        else None
    )
    has_intent = bool(
        session.scalar(
            select(ArtifactManifest.id)
            .where(
                ArtifactManifest.run_id == run.id,
                ArtifactManifest.step_execution_id == run.current_execution_id,
                ArtifactManifest.schema_type == "git_intent",
            )
            .limit(1)
        )
    )
    # The runner retains the previous attempt pointer when a retry stops at the
    # provider gate, BEFORE _server_call creates its next attempt. Accept only
    # the completed Git guard whose retry was already authorized by resume.
    previous_guard = bool(
        attempt
        and not has_intent
        and attempt.id == latest_attempt_id
        and attempt.execution_id == run.current_execution_id
        and attempt.status == "unknown"
        and attempt.finished_at is not None
        and attempt.error_code in {"external_change_detected", "git_index_dirty", "path_violation"}
        and attempt.id in runtime.get("retry_authorized_attempts", [])
    )
    execution = (
        session.get(StepExecution, run.current_execution_id) if run.current_execution_id else None
    )
    if (
        not execution
        or execution.run_id != run.id
        or execution.node_id != run.current_node_id
        or execution.status != "waiting_input"
        or target.get("node_id") != run.current_node_id
        or target.get("execution_id") != run.current_execution_id
        or target.get("action") != "retry_attempt"
        or target.get("blockers") != ["configuration_invalid"]
    ):
        blockers.append("Точка продолжения этапа изменилась. Нужна новая диагностика.")
    if (run.current_attempt_id is not None or latest_attempt_id is not None) and not previous_guard:
        blockers.append(
            "Предыдущая попытка не подтверждена как завершённая Git-проверка "
            "с разрешённым повтором."
        )
    if has_intent:
        blockers.append("Намерение коммита уже записано. Сначала требуется сверка его результата.")
    job = session.scalar(select(QueueJob).where(QueueJob.run_id == run.id))
    if (job and job.claimed_by is not None) or unsettled_attempts(session, run):
        blockers.append("Операции запуска ещё не завершены. Дождитесь их остановки.")
    if verify_processes and not stored_processes_stopped(session, run):
        blockers.append("Остановка процессов не подтверждена.")
    expected = generation.get("resource_version")
    if not isinstance(expected, int):
        blockers.append("Ожидаемая версия подключения не сохранена.")
    digest = hashlib.sha256(
        to_json(
            {
                "run": run.id,
                "state": run.state,
                "version": run.state_version,
                "node": run.current_node_id,
                "execution": run.current_execution_id,
                "attempt": run.current_attempt_id,
                "attempt_state": {
                    "execution_id": attempt.execution_id,
                    "status": attempt.status,
                    "finished_at": attempt.finished_at,
                    "error_code": attempt.error_code,
                    "retry_authorized": attempt.id in runtime.get("retry_authorized_attempts", []),
                }
                if attempt
                else None,
                "latest_attempt_id": latest_attempt_id,
                "target": target,
                "generation": generation,
                "pinned": pinned,
                "current": current,
            }
        ).encode()
    ).hexdigest()
    return CommitConnectionReview(
        run_id=run.id,
        node_id=run.current_node_id,
        state_version=run.state_version,
        comparison_id=digest,
        reviewed_at=time.time(),
        connection_name=resource.name if resource else None,
        model=generation.get("model"),
        expected_version=expected,
        current_version=resource.version if resource else None,
        previous_guard_attempt_id=attempt.id if attempt and previous_guard else None,
        changed_fields=changed,
        execution_settings_match=matches,
        can_accept=bool(verify_processes and not blockers and expected != current.get("version")),
        blockers=blockers,
        assessment=(
            "Исполняемые настройки совпадают: адрес, протокол, тип подключения и ссылка на ключ. "
            "Различается версия ресурса; какое служебное поле изменилось, "
            "сохранённые данные не показывают. "
            "Это позволяет обновить ожидаемую версию, но не доказывает доступность провайдера."
            if matches and expected != current.get("version")
            else "Версии уже совпадают; повторное принятие не требуется."
            if matches
            else "Сохранённые и текущие исполняемые настройки не подтверждены как одинаковые."
        )
        + (
            " Предыдущая попытка завершилась на защитной проверке Git; её повтор уже разрешён. "
            "Новая попытка коммита ещё не началась."
            if previous_guard
            else ""
        ),
        checks_performed=[
            "connection_configuration",
            "revision",
            "resource_active",
            "stage_boundary",
            "previous_attempt_retry",
            "git_intent_absent",
            "unsettled_attempts",
            "queue_owner",
            *(["processes_stopped"] if verify_processes else []),
        ],
    )


def accept_connection(session: Session, run: Run, command: RunCommand) -> dict[str, Any]:
    if set(command.payload) != {"commit_connection"}:
        raise AppError("resolution_invalid", "Обновление подключения отправляется отдельно.", 422)
    try:
        decision = CommitConnectionDecision.model_validate(command.payload["commit_connection"])
    except ValidationError as error:
        raise AppError("resolution_invalid", "Нужны сравнение и подтверждение.", 422) from error
    review = review_connection(session, run.id)
    if decision.comparison_id != review.comparison_id:
        raise AppError(
            "commit_connection_stale", "Подключение или запуск изменились. Обновите сравнение.", 409
        )
    if not review.can_accept:
        raise AppError(
            "commit_connection_blocked",
            "Обновление подключения недоступно.",
            409,
            {"blockers": review.blockers},
        )
    snapshot, runtime = effective_snapshot(run), json.loads(run.runtime_json)
    assert run.current_node_id is not None
    audit = artifacts.record_artifact(
        session,
        run.id,
        artifacts.ArtifactPayload(
            "commit_connection_accepted",
            body={
                "command_id": command.command_id,
                "comparison": review.model_dump(),
                "acknowledge_risk": True,
            },
        ),
        source_kind="api",
        step_execution_id=run.current_execution_id,
    )
    runtime.setdefault("commit_connection_versions", {})[run.current_node_id] = {
        "source_hash": commit_connection_source(snapshot, run.current_node_id),
        "resource_version": review.current_version,
        "execution_id": run.current_execution_id,
        "artifact_id": audit.id,
    }
    run.runtime_json = to_json(runtime)
    return {"applied": True, "artifact_id": audit.id, "next_action": "resume"}


def check_connection_resume(session: Session, run: Run) -> bool:
    accepted = (
        json.loads(run.runtime_json)
        .get("commit_connection_versions", {})
        .get(run.current_node_id, {})
    )
    if not accepted or accepted.get("execution_id") != run.current_execution_id:
        return False
    audit = session.get(ArtifactManifest, accepted.get("artifact_id"))
    if not audit or audit.run_id != run.id or audit.schema_type != "commit_connection_accepted":
        return False
    review = review_connection(session, run.id)
    if review.blockers or review.expected_version != review.current_version:
        raise AppError(
            "commit_connection_stale", "Подключение или запуск изменились. Обновите сравнение.", 409
        )
    return True
