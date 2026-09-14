"""ORM → API model mapping and small repository helpers."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import TypeVar

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from agents_ide.domain.common import from_json
from agents_ide.domain.schemas import (
    Chat,
    HarnessProfile,
    Message,
    PipelineBinding,
    PipelineTemplate,
    PipelineVersion,
    Project,
    ProviderConnection,
    WorkspaceInfo,
)
from agents_ide.errors import AppError
from agents_ide.persistence.models import (
    Chat as ChatModel,
)
from agents_ide.persistence.models import (
    HarnessProfile as HarnessProfileModel,
)
from agents_ide.persistence.models import (
    Message as MessageModel,
)
from agents_ide.persistence.models import (
    PipelineBinding as PipelineBindingModel,
)
from agents_ide.persistence.models import (
    PipelineTemplate as PipelineTemplateModel,
)
from agents_ide.persistence.models import (
    PipelineVersion as PipelineVersionModel,
)
from agents_ide.persistence.models import (
    Project as ProjectModel,
)
from agents_ide.persistence.models import (
    ProviderConnection as ProviderConnectionModel,
)

T = TypeVar("T")


def ensure_unique[T](session: Session, action: Callable[[], T]) -> T:
    try:
        return action()
    except IntegrityError:
        session.rollback()
        raise AppError(
            "conflict", "Нарушено ограничение уникальности или связь объектов", 409
        ) from None


def workspace_info(model: ProjectModel) -> WorkspaceInfo:
    return WorkspaceInfo(
        entered_path=model.workspace_entered_path,
        normalized_path=model.workspace_normalized_path,
        identity_dev=model.workspace_identity_dev,
        identity_ino=model.workspace_identity_ino,
        git_root_path=model.git_root_path,
        git_head_sha=model.git_head_sha,
        git_remote_url=model.git_remote_url,
        git_default_branch=model.git_default_branch,
        git_dirty=model.git_dirty,
    )


def _dt(value: float | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=UTC)


def project_from_model(model: ProjectModel) -> Project:
    return Project(
        id=model.id,
        name=model.name,
        workspace=workspace_info(model),
        archived=model.archived_at is not None,
        version=model.version,
        created_at=_dt(model.created_at) or datetime.now(tz=UTC),
        updated_at=_dt(model.updated_at) or datetime.now(tz=UTC),
    )


def chat_from_model(model: ChatModel) -> Chat:
    return Chat(
        id=model.id,
        project_id=model.project_id,
        title=model.title,
        archived=model.archived_at is not None,
        version=model.version,
        created_at=_dt(model.created_at) or datetime.now(tz=UTC),
        updated_at=_dt(model.updated_at) or datetime.now(tz=UTC),
    )


def message_from_model(model: MessageModel) -> Message:
    return Message(
        id=model.id,
        chat_id=model.chat_id,
        role=model.role,
        content=model.content,
        archived=model.archived_at is not None,
        version=model.version,
        created_at=_dt(model.created_at) or datetime.now(tz=UTC),
    )


def template_from_model(model: PipelineTemplateModel) -> PipelineTemplate:
    return PipelineTemplate(
        id=model.id,
        name=model.name,
        description=model.description,
        kind=model.kind,  # type: ignore[arg-type]
        schema_version=model.schema_version,
        draft=from_json(model.draft_json, {}),
        archived=model.archived_at is not None,
        version=model.version,
        created_at=_dt(model.created_at) or datetime.now(tz=UTC),
        updated_at=_dt(model.updated_at) or datetime.now(tz=UTC),
    )


def version_from_model(model: PipelineVersionModel) -> PipelineVersion:
    return PipelineVersion(
        id=model.id,
        template_id=model.template_id,
        version_number=model.version_number,
        schema_version=model.schema_version,
        execution_hash=model.execution_hash,
        policy_hash=model.policy_hash,
        graph=from_json(model.graph_json, {}),
        required_features=from_json(model.required_features_json, []),
        inputs=from_json(model.inputs_json, {}),
        settings=from_json(model.settings_json, {}),
        created_at=_dt(model.created_at) or datetime.now(tz=UTC),
    )


def binding_from_model(model: PipelineBindingModel) -> PipelineBinding:
    return PipelineBinding(
        id=model.id,
        version_id=model.version_id,
        project_id=model.project_id,
        name=model.name,
        role_assignments=from_json(model.role_assignments_json, {}),
        model_overrides=from_json(model.model_overrides_json, {}),
        limit_overrides=from_json(model.limit_overrides_json, {}),
        command_filter=from_json(model.command_filter_json, []),
        branch_policy=model.branch_policy,  # type: ignore[arg-type]
        dirty_policy=model.dirty_policy,  # type: ignore[arg-type]
        archived=model.archived_at is not None,
        version=model.revision,
        created_at=_dt(model.created_at) or datetime.now(tz=UTC),
        updated_at=_dt(model.updated_at) or datetime.now(tz=UTC),
    )


def provider_from_model(model: ProviderConnectionModel) -> ProviderConnection:
    return ProviderConnection(
        id=model.id,
        name=model.name,
        provider_kind=model.provider_kind,  # type: ignore[arg-type]
        base_url=model.base_url,
        protocol=model.protocol,  # type: ignore[arg-type]
        has_secret=model.secret_reference is not None,
        manual_models=from_json(model.manual_models_json, []),
        catalog_models=from_json(model.catalog_models_json, []),
        catalog_fetched_at=_dt(model.catalog_fetched_at),
        catalog_ttl_seconds=model.catalog_ttl_seconds,
        last_test_status=model.last_test_status,
        last_test_at=_dt(model.last_test_at),
        archived=model.archived_at is not None,
        version=model.version,
        created_at=_dt(model.created_at) or datetime.now(tz=UTC),
        updated_at=_dt(model.updated_at) or datetime.now(tz=UTC),
    )


def harness_from_model(model: HarnessProfileModel) -> HarnessProfile:
    return HarnessProfile(
        id=model.id,
        name=model.name,
        harness_kind=model.harness_kind,  # type: ignore[arg-type]
        executable_path=model.executable_path,
        settings=from_json(model.settings_json, {}),
        archived=model.archived_at is not None,
        version=model.version,
        created_at=_dt(model.created_at) or datetime.now(tz=UTC),
        updated_at=_dt(model.updated_at) or datetime.now(tz=UTC),
    )


def get_or_404[T](session: Session, model: type[T], primary_key: str) -> T:
    instance = session.get(model, primary_key)
    if instance is None:
        raise AppError("not_found", "Объект не найден", 404)
    return instance


def list_active[T](session: Session, model: type[T]) -> list[T]:
    stmt = select(model)
    if hasattr(model, "archived_at"):
        stmt = stmt.where(model.archived_at.is_(None))  # type: ignore[attr-defined]
    return list(session.scalars(stmt))
