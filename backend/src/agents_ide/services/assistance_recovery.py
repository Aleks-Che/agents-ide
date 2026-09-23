"""Read-only recovery facts and command semantics for the assistant."""

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from agents_ide.persistence.models import AgentSession, ArtifactManifest, Run, StepAttempt
from agents_ide.services.agent_recovery import recovery_options
from agents_ide.services.run_controls import unsettled_attempts
from agents_ide.worker.processes import stored_processes_stopped


def recovery_context(
    session: Session, run: Run, attempt: StepAttempt | None, *, verify_processes: bool
) -> dict[str, Any]:
    runtime = json.loads(run.runtime_json or "{}")
    waiting = json.loads(run.waiting_reason_json or "{}") or {}
    details = waiting.get("details") or {}
    options = recovery_options(run) if attempt else {}
    native = (
        session.scalar(select(AgentSession).where(AgentSession.attempt_id == attempt.id))
        if attempt
        else None
    )
    late_id = runtime.get("late_result_refs", {}).get(attempt.id) if attempt else None
    late = session.get(ArtifactManifest, late_id) if late_id else None
    validated_late_result = bool(
        late
        and attempt
        and late.run_id == run.id
        and late.step_attempt_id == attempt.id
        and (json.loads(late.body_json or "{}") or {}).get("validated_by_server")
    )
    unsettled = unsettled_attempts(session, run)
    processes_stopped = stored_processes_stopped(session, run) if verify_processes else None
    target = json.loads(run.resume_target_json or "{}") or {}
    can_resolve = run.state == "waiting_input" or (
        run.state in {"paused", "stopped"} and bool(target.get("blockers"))
    )
    actions: list[dict[str, Any]] = []

    def add(action: str, label: str, blockers: list[str]) -> None:
        if not can_resolve:
            blockers.append("Сохранение решения недоступно в текущем состоянии")
        if processes_stopped is False:
            blockers.append("Остановка прежнего владельца и процессов не подтверждена")
        actions.append(
            {
                "action": action,
                "label": label,
                "command": "resolve",
                "available": False if blockers else processes_stopped,
                "blocked_by": blockers,
                "after_save": "Нажать «Продолжить»; сервер повторно проверит условия продолжения",
            }
        )

    continuation_blockers = []
    if not attempt or attempt.execution_id != run.current_execution_id:
        continuation_blockers.append("Текущая попытка этапа не найдена")
    if (
        not attempt
        or attempt.status not in {"unknown", "failed", "interrupted"}
        or attempt.finished_at is None
    ):
        continuation_blockers.append("Предыдущая попытка ещё не завершена")
    if any(item.id != run.current_attempt_id for item in unsettled):
        continuation_blockers.append("Есть другие попытки с неподтверждённым результатом")
    selection = json.loads(attempt.selection_json or "{}") if attempt else {}
    if not isinstance(selection.get("member_index"), int):
        continuation_blockers.append("Не сохранена выбранная модель попытки")
    for action, label in (
        ("continue_session", "Продолжить в той же сессии агента"),
        ("next_candidate", "Продолжить следующей моделью группы"),
    ):
        if action not in options.get("actions", []):
            continue
        blockers = continuation_blockers.copy()
        if action == "continue_session":
            current = runtime.get("current_agent_session", {})
            saved = runtime.get("native_sessions", {}).get(current.get("session_key"), {})
            if not native or native.external_session_id != saved.get("session_id"):
                blockers.append("Сохранённая сессия не соответствует текущей попытке")
        add(action, label, blockers)
    if waiting.get("code") == "unknown_external_result":
        reconciliation_blockers = []
        if (
            not attempt
            or attempt.status != "unknown"
            or not any(a.id == attempt.id for a in unsettled)
        ):
            reconciliation_blockers.append("Нет неразрешённой неизвестной попытки для сверки")
        add(
            "accept_result",
            "Принять подтверждённый поздний результат",
            [
                *reconciliation_blockers,
                *(
                    []
                    if validated_late_result
                    else ["Нет подтверждённого валидного позднего результата"]
                ),
            ],
        )
        add(
            "retry_authorized",
            "Разрешить повтор операции после проверки её последствий",
            reconciliation_blockers.copy(),
        )
    pending = runtime.get("pending_agent_recovery") or {}
    return {
        "attempt_id": attempt.id if attempt else None,
        "operation_id": attempt.operation_id if attempt else None,
        "harness": native.harness_kind if native else None,
        "native_session_saved": bool(native and native.external_session_id),
        "request_reference_saved": bool(attempt and attempt.request_artifact_id),
        "result_reference_saved": bool(attempt and attempt.result_artifact_id),
        "validated_late_result": validated_late_result,
        "resume_checkpoint_saved": bool(runtime.get("work")),
        "processes_stopped": processes_stopped,
        "last_reconciliation": {
            "processes_stopped": details.get("stopped"),
            "checked_at": details.get("checked_at"),
        },
        "next_model": options.get("next_model"),
        "pending_action": pending.get("action")
        if pending.get("attempt_id") == run.current_attempt_id
        else None,
        "actions": actions,
        "rules": [
            "available=null: процессы сейчас не проверялись; "
            "available=false: действие заблокировано.",
            "Сохранить решение (resolve) не запускает работу. "
            "В обычной форме затем отдельно нажать «Продолжить» (resume). "
            "Если для этого запуска в tools доступен инструмент принятия Git-изменений, "
            "он выполнит resolve и resume после подтверждения конкретного сравнения в чате.",
            "continue_session и next_candidate продолжают текущий этап с сохранённым контекстом; "
            "доступность сессии у провайдера выясняется при продолжении.",
            "accept_result требует валидного позднего результата сервера. Пользовательская "
            "уверенность или git diff не заменяют этот результат.",
            "retry_authorized разрешает повтор после сверки эффектов: нужны attempt_id и "
            "текст доказательств. Для продолжения также нужен сохранённый checkpoint.",
            "stop не устраняет неизвестность, может потребовать сверки и при применении "
            "сбрасывает продолжение сессии агента; последующее выполнение может повторить этап.",
            "cancel завершает запуск после подтверждения остановки процессов. "
            "Ни stop, ни cancel не откатывают изменения файлов и внешние эффекты.",
            "Отсутствие ошибки в переданных данных не доказывает отсутствие сохранённого "
            "запроса или операции. Первопричина потери результата может оставаться неизвестной.",
            "Чистый git diff не доказывает отсутствие эффектов: есть коммиты, индекс, "
            "неотслеживаемые и игнорируемые файлы, а также внешние действия.",
        ],
    }
