"""Project CRUD and workspace identity handling."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from agents_ide.domain.common import assert_safe_name, new_id, utc_now
from agents_ide.domain.schemas import Project, ProjectArchive, ProjectCreate, ProjectUpdate
from agents_ide.domain.workspace import collect_workspace
from agents_ide.errors import AppError
from agents_ide.persistence.models import Project as ProjectModel
from agents_ide.services.mapping import (
    ensure_unique,
    get_or_404,
    project_from_model,
)
from agents_ide.services.transactions import begin_write


def create_project(session: Session, payload: ProjectCreate) -> Project:
    assert_safe_name(payload.name)
    entered, normalized, dev, ino, git = collect_workspace(payload.workspace_path)
    begin_write(session)
    now = utc_now()
    model = ProjectModel(
        id=new_id(),
        name=payload.name,
        workspace_entered_path=entered,
        workspace_normalized_path=normalized,
        workspace_identity_dev=dev,
        workspace_identity_ino=ino,
        git_root_path=git.root_path if git else None,
        git_head_sha=git.head_sha if git else None,
        git_remote_url=git.remote_url if git else None,
        git_default_branch=git.default_branch if git else None,
        git_dirty=git.dirty if git else False,
        archived_at=None,
        version=1,
        created_at=now,
        updated_at=now,
    )

    def _add() -> Project:
        session.add(model)
        session.flush()
        return project_from_model(model)

    return ensure_unique(session, _add)


def list_projects(session: Session, include_archived: bool = False) -> list[Project]:
    stmt = select(ProjectModel)
    if not include_archived:
        stmt = stmt.where(ProjectModel.archived_at.is_(None))
    stmt = stmt.order_by(ProjectModel.created_at.desc())
    return [project_from_model(row) for row in session.scalars(stmt).all()]


def get_project(session: Session, project_id: str) -> Project:
    return project_from_model(get_or_404(session, ProjectModel, project_id))


def update_project(session: Session, project_id: str, payload: ProjectUpdate) -> Project:
    begin_write(session)
    model = get_or_404(session, ProjectModel, project_id)
    if model.version != payload.expected_version:
        raise AppError(
            "version_conflict",
            "Проект изменён в другом месте",
            409,
            {"expected_version": payload.expected_version, "actual_version": model.version},
        )
    if model.archived_at is not None:
        raise AppError("project_archived", "Архивный проект нельзя редактировать", 409)
    if payload.name is not None:
        assert_safe_name(payload.name)
        model.name = payload.name
    model.version += 1
    model.updated_at = utc_now()
    session.flush()
    return project_from_model(model)


def archive_project(session: Session, project_id: str, payload: ProjectArchive) -> Project:
    begin_write(session)
    model = get_or_404(session, ProjectModel, project_id)
    if model.version != payload.expected_version:
        raise AppError(
            "version_conflict",
            "Проект изменён в другом месте",
            409,
            {"expected_version": payload.expected_version, "actual_version": model.version},
        )
    has_active_runs = _has_active_runs(session, project_id)
    if payload.archive and has_active_runs:
        raise AppError(
            "project_has_active_runs",
            "Активные Run препятствуют архивации проекта",
            409,
        )
    if payload.archive:
        model.archived_at = utc_now()
    else:
        model.archived_at = None
    model.version += 1
    model.updated_at = utc_now()
    session.flush()
    return project_from_model(model)


def _has_active_runs(session: Session, project_id: str) -> bool:
    from agents_ide.persistence.models import Run as RunModel

    stmt = select(RunModel.id).where(
        RunModel.project_id == project_id,
        RunModel.state.notin_(["completed", "failed", "cancelled"]),
    )
    return session.execute(stmt).first() is not None
