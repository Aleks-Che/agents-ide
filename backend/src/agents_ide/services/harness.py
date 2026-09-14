"""Harness profile service (Codex, OpenCode, future kinds)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from agents_ide.domain.common import assert_safe_name, new_id, to_json, utc_now
from agents_ide.domain.schemas import (
    HarnessProfile,
    HarnessProfileCreate,
    HarnessProfileUpdate,
)
from agents_ide.errors import AppError
from agents_ide.persistence.models import HarnessProfile as HarnessProfileModel
from agents_ide.services.mapping import ensure_unique, get_or_404, harness_from_model
from agents_ide.services.transactions import begin_write


def create_harness(session: Session, payload: HarnessProfileCreate) -> HarnessProfile:
    begin_write(session)
    assert_safe_name(payload.name)
    now = utc_now()
    model = HarnessProfileModel(
        id=new_id(),
        name=payload.name,
        harness_kind=payload.harness_kind,
        executable_path=payload.executable_path,
        settings_json=to_json(payload.settings),
        archived_at=None,
        version=1,
        created_at=now,
        updated_at=now,
    )

    def _add() -> HarnessProfile:
        session.add(model)
        session.flush()
        return harness_from_model(model)

    return ensure_unique(session, _add)


def list_harnesses(session: Session, include_archived: bool = False) -> list[HarnessProfile]:
    stmt = select(HarnessProfileModel)
    if not include_archived:
        stmt = stmt.where(HarnessProfileModel.archived_at.is_(None))
    stmt = stmt.order_by(HarnessProfileModel.created_at.desc())
    return [harness_from_model(row) for row in session.scalars(stmt).all()]


def get_harness(session: Session, harness_id: str) -> HarnessProfile:
    return harness_from_model(get_or_404(session, HarnessProfileModel, harness_id))


def update_harness(
    session: Session, harness_id: str, payload: HarnessProfileUpdate
) -> HarnessProfile:
    begin_write(session)
    model = get_or_404(session, HarnessProfileModel, harness_id)
    if model.version != payload.expected_version:
        raise AppError(
            "version_conflict",
            "Профиль изменён в другом месте",
            409,
            {"expected_version": payload.expected_version, "actual_version": model.version},
        )
    if model.archived_at is not None:
        raise AppError("harness_archived", "Архивный профиль недоступен", 409)
    if payload.name is not None:
        assert_safe_name(payload.name)
        model.name = payload.name
    if payload.executable_path is not None:
        model.executable_path = payload.executable_path
    if payload.settings is not None:
        model.settings_json = to_json(payload.settings)
    model.version += 1
    model.updated_at = utc_now()
    session.flush()
    return harness_from_model(model)


def archive_harness(session: Session, harness_id: str, expected_version: int) -> HarnessProfile:
    begin_write(session)
    model = get_or_404(session, HarnessProfileModel, harness_id)
    if model.version != expected_version:
        raise AppError(
            "version_conflict",
            "Профиль изменён в другом месте",
            409,
            {"expected_version": expected_version, "actual_version": model.version},
        )
    model.archived_at = utc_now()
    model.version += 1
    model.updated_at = utc_now()
    session.flush()
    return harness_from_model(model)
