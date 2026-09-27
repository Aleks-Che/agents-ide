"""Recover idle harness calls through the same journaled PAUSE/RESUME as the UI."""

import json
import logging
import time
from collections.abc import Collection
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from agents_ide.domain.common import to_json
from agents_ide.domain.schemas import RunCommand
from agents_ide.engine.events import append_event
from agents_ide.errors import AppError
from agents_ide.persistence.models import AgentSession, CommandJournal, Run, StepAttempt
from agents_ide.services.general_settings import get_settings
from agents_ide.services.runs import submit_command
from agents_ide.services.sidebar_activity import _runs_awaiting_input
from agents_ide.services.transactions import begin_write

logger = logging.getLogger("agents_ide.worker")
INITIATOR = "harness_watchdog"


def _event(session: Session, run: Run, command_id: str, action: str, message: str) -> None:
    append_event(
        session,
        run.id,
        "agent.watchdog",
        {"action": action, "message": message},
        node_id=run.current_node_id,
        execution_id=run.current_execution_id,
        attempt_id=run.current_attempt_id,
        command_id=command_id,
        generation=run.worker_generation or 0,
    )


def _check_run(session: Session, run: Run, now: float, active_runs: Collection[str]) -> None:
    settings = get_settings(session).harness_watchdog
    if not settings.enabled or not run.current_attempt_id:
        return
    attempt = session.get(StepAttempt, run.current_attempt_id)
    if attempt is None:
        return
    last_command = session.scalar(
        select(CommandJournal)
        .where(CommandJournal.run_id == run.id)
        .order_by(CommandJournal.sequence.desc())
        .limit(1)
    )
    if run.state == "running" and attempt.status == "running":
        # Every new attempt gets its own full interval. Worker heartbeats and
        # process health checks never enter this timestamp.
        last_progress = session.scalar(
            select(func.max(AgentSession.last_external_event_at)).where(
                AgentSession.attempt_id == attempt.id,
                AgentSession.harness_kind.in_(["opencode", "codex"]),
            )
        )
        last_progress = max(attempt.started_at, last_progress or attempt.started_at)
        if now - last_progress < settings.idle_minutes * 60:
            return
        if run.id in _runs_awaiting_input(session, run.project_id, run.chat_id):
            return
        # A late manual control always wins, even if its application is pending.
        if (
            last_command
            and last_command.status == "accepted"
            and last_command.command_type
            in {
                "pause",
                "stop",
                "cancel",
                "restart_stage",
            }
        ):
            return
        command_id = f"watchdog-pause-{attempt.id}"
        if session.scalar(
            select(CommandJournal.id).where(
                CommandJournal.run_id == run.id, CommandJournal.command_id == command_id
            )
        ):
            return
        submit_command(
            session,
            run.id,
            RunCommand(
                command_id=command_id,
                command_type="pause",
                expected_state_version=run.state_version,
                payload={"attempt_id": attempt.id, "idle_minutes": settings.idle_minutes},
            ),
            initiator=INITIATOR,
        )
        _event(
            session,
            run,
            command_id,
            "pause",
            f"Нет активности агента {settings.idle_minutes} мин. "
            "Автовосстановление: запрошена пауза с сохранением сессии.",
        )
        return
    if (
        run.state not in {"paused", "waiting_input"}
        or run.id in active_runs
        or last_command is None
        or last_command.initiator != INITIATOR
        or last_command.command_type != "pause"
        or last_command.status not in {"accepted", "applied"}
        or json.loads(last_command.payload_json or "{}").get("attempt_id") != attempt.id
    ):
        return
    response: dict[str, Any] = json.loads(last_command.response_json or "{}")
    if response.get("auto_resume_error"):
        return
    continuation = json.loads(run.runtime_json).get("agent_continuation", {})
    try:
        if run.state != "paused" or last_command.status != "applied":
            raise AppError("pause_unconfirmed", "Пауза агента не подтверждена", 409)
        if attempt.status != "succeeded" and not (
            attempt.status == "interrupted"
            and continuation.get("session_id")
            and continuation.get("execution_id") == run.current_execution_id
        ):
            raise AppError(
                "session_resume_unavailable", "Нет подтверждённой сессии для продолжения", 409
            )
        # A failed normal resume must not leave partial mutations behind.
        with session.begin_nested():
            submit_command(
                session,
                run.id,
                RunCommand(
                    command_id=f"watchdog-resume-{attempt.id}",
                    command_type="resume",
                    expected_state_version=run.state_version,
                ),
                initiator=INITIATOR,
            )
        _event(
            session,
            run,
            last_command.command_id,
            "resume",
            "Автовосстановление: пауза завершена, запуск поставлен в очередь на продолжение.",
        )
    except AppError as exc:
        # Record once. Do not bypass reconciliation or replay an unknown writer.
        response["auto_resume_error"] = {"code": exc.code, "message": exc.message}
        last_command.response_json = to_json(response)
        _event(
            session,
            run,
            last_command.command_id,
            "blocked",
            f"Автовосстановление требует внимания: {exc.message}.",
        )


def check_harnesses(
    factory: sessionmaker[Session], *, active_runs: Collection[str] = (), now: float | None = None
) -> None:
    now = time.time() if now is None else now
    with factory() as session:
        settings = get_settings(session).harness_watchdog
        if not settings.enabled:
            return
        cutoff = now - settings.idle_minutes * 60
        latest_initiator = (
            select(CommandJournal.initiator)
            .where(CommandJournal.run_id == Run.id)
            .order_by(CommandJournal.sequence.desc())
            .limit(1)
            .scalar_subquery()
        )
        # Flowing agents and manual pauses do not need a write reservation.
        run_ids = list(
            session.scalars(
                select(Run.id)
                .join(StepAttempt, StepAttempt.id == Run.current_attempt_id)
                .join(AgentSession, AgentSession.attempt_id == Run.current_attempt_id)
                .where(
                    AgentSession.harness_kind.in_(["opencode", "codex"]),
                    or_(
                        and_(
                            Run.state == "running",
                            StepAttempt.status == "running",
                            StepAttempt.started_at <= cutoff,
                            func.coalesce(
                                AgentSession.last_external_event_at, StepAttempt.started_at
                            )
                            <= cutoff,
                        ),
                        and_(
                            Run.state.in_(["paused", "waiting_input"]),
                            latest_initiator == INITIATOR,
                        ),
                    ),
                )
                .distinct()
            )
        )
    for run_id in run_ids:
        try:
            with factory() as session:
                # Re-read state, input requests and manual commands together.
                begin_write(session)
                run = session.get(Run, run_id)
                if run is not None:
                    _check_run(session, run, now, active_runs)
                session.commit()
        except AppError:
            logger.exception("worker.harness_watchdog_failed", extra={"run_id": run_id})
