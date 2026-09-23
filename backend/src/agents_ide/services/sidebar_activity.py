"""Compact activity indicators across all projects and chats."""

from __future__ import annotations

import json
from dataclasses import dataclass

from pydantic import Field
from sqlalchemy import func, select, true
from sqlalchemy.orm import Session

from agents_ide.domain.schemas import ApiOutput
from agents_ide.persistence.models import (
    ArtifactManifest,
    Chat,
    PlanningJob,
    Project,
    Run,
    RunEvent,
)

RUNNING_STATES = {
    "queued",
    "running",
    "pause_requested",
    "stop_requested",
    "retry_wait",
    "recovering",
}
ATTENTION_STATES = {"waiting_input", "failed"}
PLANNING_RUNNING_STATES = {"drafting", "merging"}
PLANNING_ATTENTION_STATES = {"needs_answers", "ready_for_confirmation", "failed"}


class ActivityStatus(ApiOutput):
    running: bool = False
    attention: bool = False


class SidebarActivity(ApiOutput):
    projects: dict[str, ActivityStatus] = Field(default_factory=dict)
    chats: dict[str, ActivityStatus] = Field(default_factory=dict)


def _runs_awaiting_input(
    session: Session, project_id: str | None = None, chat_id: str | None = None
) -> set[str]:
    # Questions and permission prompts do not change the Run's running state.
    # Only the current attempt can require a reply; ignore stale attempt events.
    events = session.execute(
        select(RunEvent.run_id, RunEvent.type, RunEvent.payload_json)
        .join(Run, Run.id == RunEvent.run_id)
        .join(Project, Project.id == Run.project_id)
        .outerjoin(Chat, Chat.id == Run.chat_id)
        .where(
            Project.archived_at.is_(None),
            Chat.archived_at.is_(None),
            Run.state == "running",
            Run.project_id == project_id if project_id else true(),
            Run.chat_id == chat_id if chat_id else true(),
            RunEvent.step_attempt_id == Run.current_attempt_id,
            RunEvent.type.in_(["agent.input_requested", "agent.input_closed"]),
        )
        .order_by(RunEvent.sequence)
    ).all()
    payloads = [json.loads(event.payload_json) for event in events]
    artifact_ids = {payload["artifact_id"] for payload in payloads if payload.get("artifact_id")}
    artifacts = (
        {
            artifact.id: artifact
            for artifact in session.scalars(
                select(ArtifactManifest).where(ArtifactManifest.id.in_(artifact_ids))
            )
        }
        if artifact_ids
        else {}
    )
    pending: set[tuple[str, str]] = set()
    for event, payload in zip(events, payloads, strict=True):
        artifact = artifacts.get(payload.get("artifact_id"))
        if artifact and artifact.run_id == event.run_id:
            payload = json.loads(artifact.body_json or "{}")
        key = (event.run_id, str(payload.get("question_id", "")))
        if event.type == "agent.input_requested":
            pending.add(key)
        else:
            pending.discard(key)
    return {run_id for run_id, _ in pending}


@dataclass(frozen=True)
class ActivitySource:
    id: str
    kind: str
    project_id: str
    chat_id: str | None
    state: str
    running: bool
    attention: bool


def activity_sources(
    session: Session, project_id: str | None = None, chat_id: str | None = None
) -> list[ActivitySource]:
    """Shared source of truth for badges and contextual assistance."""
    result: list[ActivitySource] = []

    pending_inputs = _runs_awaiting_input(session, project_id, chat_id)
    # Rank before filtering states: a completed/cancelled replacement also
    # supersedes old failures and waiting runs. Running processes still count.
    ranked_runs = (
        select(
            Run.id,
            Run.project_id,
            Run.chat_id,
            Run.state,
            Run.waiting_reason_json,
            func.row_number()
            .over(partition_by=Run.chat_id, order_by=(Run.created_at.desc(), Run.id.desc()))
            .label("recency"),
        )
        .join(Project, Project.id == Run.project_id)
        .outerjoin(Chat, Chat.id == Run.chat_id)
        .where(
            Project.archived_at.is_(None),
            Chat.archived_at.is_(None),
            Run.project_id == project_id if project_id else true(),
            Run.chat_id == chat_id if chat_id else true(),
        )
        .subquery()
    )
    for run in session.execute(
        select(ranked_runs).where(
            ranked_runs.c.state.in_(RUNNING_STATES | ATTENTION_STATES | {"paused", "stopped"})
        )
    ):
        current = run.chat_id is None or run.recency == 1
        attention = run.id in pending_inputs or (
            current
            and (
                run.state in ATTENTION_STATES
                or (
                    run.state in {"paused", "stopped"}
                    and bool(json.loads(run.waiting_reason_json or "null"))
                )
            )
        )
        result.append(
            ActivitySource(
                run.id,
                "run",
                run.project_id,
                run.chat_id,
                run.state,
                running=run.state in RUNNING_STATES and not attention,
                attention=attention,
            )
        )
    ranked_jobs = (
        select(
            PlanningJob.id,
            PlanningJob.project_id,
            PlanningJob.chat_id,
            PlanningJob.state,
            func.row_number()
            .over(
                partition_by=PlanningJob.chat_id,
                order_by=(PlanningJob.created_at.desc(), PlanningJob.id.desc()),
            )
            .label("recency"),
        )
        .join(Project, Project.id == PlanningJob.project_id)
        .outerjoin(Chat, Chat.id == PlanningJob.chat_id)
        .where(
            Project.archived_at.is_(None),
            Chat.archived_at.is_(None),
            PlanningJob.project_id == project_id if project_id else true(),
            PlanningJob.chat_id == chat_id if chat_id else true(),
        )
        .subquery()
    )
    for job in session.execute(
        select(ranked_jobs).where(
            ranked_jobs.c.state.in_(PLANNING_RUNNING_STATES | PLANNING_ATTENTION_STATES)
        )
    ):
        result.append(
            ActivitySource(
                job.id,
                "planning",
                job.project_id,
                job.chat_id,
                job.state,
                running=job.state in PLANNING_RUNNING_STATES,
                attention=job.state in PLANNING_ATTENTION_STATES
                and (job.chat_id is None or job.recency == 1),
            )
        )
    return [source for source in result if source.running or source.attention]


def sidebar_activity(session: Session) -> SidebarActivity:
    result = SidebarActivity()
    for source in activity_sources(session):
        statuses = [result.projects.setdefault(source.project_id, ActivityStatus())]
        if source.chat_id is not None:
            statuses.append(result.chats.setdefault(source.chat_id, ActivityStatus()))
        for status in statuses:
            status.running |= source.running
            status.attention |= source.attention
    return result
