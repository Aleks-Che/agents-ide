"""Pipeline template, version and binding services.

Templates can be archived but their versions stay immutable. Editing a draft
version is not allowed; an edit produces a new PipelineVersion with a new
execution hash and policy hash.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agents_ide.domain.common import (
    assert_safe_name,
    content_hash,
    new_id,
    to_json,
    utc_now,
)
from agents_ide.domain.schemas import (
    PipelineBinding,
    PipelineBindingCreate,
    PipelineBindingUpdate,
    PipelineDraft,
    PipelineDraftUpdate,
    PipelineTemplate,
    PipelineTemplateCreate,
    PipelineTemplateUpdate,
    PipelineVersion,
    PipelineVersionCreate,
    ResolvedSettings,
    ResolvedSettingSource,
    SettingsOverrides,
)
from agents_ide.errors import AppError
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
from agents_ide.services.mapping import (
    binding_from_model,
    ensure_unique,
    get_or_404,
    template_from_model,
    version_from_model,
)
from agents_ide.services.settings import (
    DEFAULTS,
    capture_dependencies,
    resolve_configuration,
)
from agents_ide.services.settings import (
    execution_hash as effective_execution_hash,
)
from agents_ide.services.settings import policy_hash as effective_policy_hash
from agents_ide.services.transactions import begin_write


def create_template(session: Session, payload: PipelineTemplateCreate) -> PipelineTemplate:
    begin_write(session)
    assert_safe_name(payload.name)
    now = utc_now()
    model = PipelineTemplateModel(
        id=new_id(),
        name=payload.name,
        description=payload.description,
        kind="user",
        schema_version=payload.schema_version,
        draft_json=to_json(PipelineDraft().model_dump()),
        archived_at=None,
        version=1,
        created_at=now,
        updated_at=now,
    )

    def _add() -> PipelineTemplate:
        session.add(model)
        session.flush()
        return template_from_model(model)

    return ensure_unique(session, _add)


def list_templates(session: Session, include_archived: bool = False) -> list[PipelineTemplate]:
    stmt = select(PipelineTemplateModel)
    if not include_archived:
        stmt = stmt.where(PipelineTemplateModel.archived_at.is_(None))
    stmt = stmt.order_by(PipelineTemplateModel.created_at.desc())
    return [template_from_model(row) for row in session.scalars(stmt).all()]


def get_template(session: Session, template_id: str) -> PipelineTemplate:
    return template_from_model(get_or_404(session, PipelineTemplateModel, template_id))


def update_template(
    session: Session, template_id: str, payload: PipelineTemplateUpdate
) -> PipelineTemplate:
    begin_write(session)
    model = get_or_404(session, PipelineTemplateModel, template_id)
    if model.version != payload.expected_version:
        raise AppError(
            "version_conflict",
            "Шаблон изменён в другом месте",
            409,
            {"expected_version": payload.expected_version, "actual_version": model.version},
        )
    if model.kind == "system":
        raise AppError("system_template_immutable", "Системный шаблон нельзя редактировать", 409)
    if model.archived_at is not None:
        raise AppError("template_archived", "Архивный шаблон недоступен для правки", 409)
    if payload.name is not None:
        assert_safe_name(payload.name)
        model.name = payload.name
    if payload.description is not None:
        model.description = payload.description
    model.version += 1
    model.updated_at = utc_now()
    session.flush()
    return template_from_model(model)


def archive_template(session: Session, template_id: str, expected_version: int) -> PipelineTemplate:
    begin_write(session)
    model = get_or_404(session, PipelineTemplateModel, template_id)
    if model.version != expected_version:
        raise AppError(
            "version_conflict",
            "Шаблон изменён в другом месте",
            409,
            {"expected_version": expected_version, "actual_version": model.version},
        )
    if model.kind == "system":
        raise AppError("system_template_immutable", "Системный шаблон нельзя архивировать", 409)
    model.archived_at = utc_now()
    model.version += 1
    model.updated_at = utc_now()
    session.flush()
    return template_from_model(model)


def create_version(
    session: Session, template_id: str, payload: PipelineVersionCreate
) -> PipelineVersion:
    begin_write(session)
    template = get_or_404(session, PipelineTemplateModel, template_id)
    if template.archived_at is not None:
        raise AppError("template_archived", "Архивный шаблон недоступен", 409)
    # Strict validation: a published version must run successfully.
    from agents_ide.domain.graph_validation import (
        check_version_features,
        definition_hash,
        validate_graph,
    )

    report = validate_graph(payload.graph, inputs=payload.inputs, allow_incomplete=False)
    check_version_features(template.schema_version, payload.required_features, report)
    check_version_features(payload.schema_version, payload.required_features, report)
    if not report.ok:
        raise AppError(
            "graph_validation_failed",
            "Граф не прошёл проверку",
            422,
            {"errors": [issue.to_dict() for issue in report.errors]},
        )
    next_number = _next_version_number(session, template_id)
    features = sorted(set(payload.required_features) | set(report.features))
    policy_payload = {**DEFAULTS, **payload.settings.model_dump(exclude_none=True)}
    capture_dependencies(session, payload.graph, policy_payload)
    execution_hash = definition_hash(
        {
            "graph": payload.graph,
            "required_features": features,
            "inputs": payload.inputs,
            "settings": payload.settings.model_dump(exclude_none=True),
            "schema_version": template.schema_version,
        }
    )
    policy_hash = content_hash(policy_payload)
    now = utc_now()
    model = PipelineVersionModel(
        id=new_id(),
        template_id=template_id,
        version_number=next_number,
        schema_version=template.schema_version,
        required_features_json=to_json(features),
        graph_json=to_json(payload.graph),
        execution_hash=execution_hash,
        policy_hash=policy_hash,
        inputs_json=to_json(payload.inputs),
        settings_json=to_json(payload.settings.model_dump(exclude_none=True)),
        origin="imported"
        if json.loads(template.draft_json).get("origin") == "imported"
        else payload.origin,
        created_at=now,
    )

    def _add() -> PipelineVersion:
        session.add(model)
        session.flush()
        return version_from_model(model)

    return ensure_unique(session, _add)


def list_versions(session: Session, template_id: str) -> list[PipelineVersion]:
    if session.get(PipelineTemplateModel, template_id) is None:
        raise AppError("template_not_found", "Шаблон не найден", 404)
    stmt = (
        select(PipelineVersionModel)
        .where(PipelineVersionModel.template_id == template_id)
        .order_by(PipelineVersionModel.version_number.desc())
    )
    return [version_from_model(row) for row in session.scalars(stmt).all()]


def get_version(session: Session, version_id: str) -> PipelineVersion:
    return version_from_model(get_or_404(session, PipelineVersionModel, version_id))


def _compute_hash(graph: dict[str, Any], features: list[str], inputs: dict[str, Any]) -> str:
    return content_hash({"graph": graph, "features": sorted(features), "inputs": inputs})


def _next_version_number(session: Session, template_id: str) -> int:
    stmt = select(func.max(PipelineVersionModel.version_number)).where(
        PipelineVersionModel.template_id == template_id
    )
    current = session.execute(stmt).scalar()
    return int(current or 0) + 1


def create_binding(
    session: Session, version_id: str, payload: PipelineBindingCreate
) -> PipelineBinding:
    begin_write(session)
    if session.get(ProjectModel, payload.project_id) is None:
        raise AppError("project_not_found", "Проект не найден", 404)
    version = get_or_404(session, PipelineVersionModel, version_id)
    if get_or_404(session, ProjectModel, payload.project_id).archived_at is not None:
        raise AppError("project_archived", "Архивный проект недоступен", 409)
    now = utc_now()
    model = PipelineBindingModel(
        id=new_id(),
        version_id=version.id,
        project_id=payload.project_id,
        name=payload.name,
        role_assignments_json=to_json(payload.role_assignments),
        model_overrides_json=to_json(payload.model_overrides),
        model_selections_json=to_json(
            {
                role: selection.model_dump(mode="json")
                for role, selection in payload.model_selections.items()
            }
        ),
        limit_overrides_json=to_json(payload.limit_overrides),
        command_filter_json=to_json(payload.command_filter),
        settings_json=to_json(
            payload.model_dump(include=set(DEFAULTS), exclude_unset=True, exclude_none=True)
        ),
        branch_policy=payload.branch_policy,
        dirty_policy=payload.dirty_policy,
        archived_at=None,
        revision=1,
        created_at=now,
        updated_at=now,
    )

    def _add() -> PipelineBinding:
        session.add(model)
        session.flush()
        values, _ = resolve_configuration(model, version)
        capture_dependencies(session, version_from_model(version).graph, values)
        return binding_from_model(model)

    return ensure_unique(session, _add)


def list_bindings(
    session: Session, project_id: str | None = None, include_archived: bool = False
) -> list[PipelineBinding]:
    stmt = select(PipelineBindingModel)
    if project_id is not None:
        stmt = stmt.where(PipelineBindingModel.project_id == project_id)
    if not include_archived:
        stmt = stmt.where(PipelineBindingModel.archived_at.is_(None))
    stmt = stmt.order_by(PipelineBindingModel.created_at.desc())
    return [binding_from_model(row) for row in session.scalars(stmt).all()]


def get_binding(session: Session, binding_id: str) -> PipelineBinding:
    return binding_from_model(get_or_404(session, PipelineBindingModel, binding_id))


def update_binding(
    session: Session, binding_id: str, payload: PipelineBindingUpdate
) -> PipelineBinding:
    begin_write(session)
    model = get_or_404(session, PipelineBindingModel, binding_id)
    if model.revision != payload.expected_version:
        raise AppError(
            "version_conflict",
            "Привязка изменена в другом месте",
            409,
            {"expected_version": payload.expected_version, "actual_revision": model.revision},
        )
    if model.archived_at is not None:
        raise AppError("binding_archived", "Архивная привязка недоступна", 409)
    fields = payload.model_dump(exclude_none=True, exclude={"expected_version"})
    import json

    explicit = json.loads(model.settings_json)
    for key, opposite in [
        ("model_selections", "model_overrides"),
        ("model_overrides", "model_selections"),
    ]:
        if key in fields:
            current = json.loads(getattr(model, opposite + "_json"))
            for role in fields[key]:
                current.pop(role, None)
                explicit.get(opposite, {}).pop(role, None)
            setattr(model, opposite + "_json", to_json(current))
    explicit.update({key: value for key, value in fields.items() if key in DEFAULTS})
    model.settings_json = to_json(explicit)
    for name, value in fields.items():
        if name in {
            "role_assignments",
            "model_overrides",
            "model_selections",
            "limit_overrides",
            "command_filter",
        }:
            setattr(model, f"{name}_json", to_json(value))
        elif name == "role_parameters":
            pass  # Stored in settings_json with the other explicit overrides.
        else:
            setattr(model, name, value)
    model.revision += 1
    model.updated_at = utc_now()
    session.flush()
    if set(fields) & {"model_selections", "model_overrides", "role_parameters", "role_assignments"}:
        version = get_or_404(session, PipelineVersionModel, model.version_id)
        values, _ = resolve_configuration(model, version)
        capture_dependencies(session, version_from_model(version).graph, values)
    return binding_from_model(model)


def archive_binding(session: Session, binding_id: str, expected_version: int) -> PipelineBinding:
    begin_write(session)
    model = get_or_404(session, PipelineBindingModel, binding_id)
    if model.revision != expected_version:
        raise AppError(
            "version_conflict",
            "Привязка изменена в другом месте",
            409,
            {"expected_version": expected_version, "actual_revision": model.revision},
        )
    has_active_runs = _binding_has_active_runs(session, binding_id)
    if has_active_runs:
        raise AppError(
            "binding_has_active_runs",
            "Активные Run препятствуют архивации",
            409,
        )
    model.archived_at = utc_now()
    model.revision += 1
    model.updated_at = utc_now()
    session.flush()
    return binding_from_model(model)


def resolve_settings(
    session: Session,
    binding_id: str,
    overrides: SettingsOverrides | None = None,
) -> ResolvedSettings:
    binding = get_or_404(session, PipelineBindingModel, binding_id)
    version = get_or_404(session, PipelineVersionModel, binding.version_id)
    values, sources = resolve_configuration(binding, version, overrides)
    dependencies = capture_dependencies(session, version_from_model(version).graph, values)
    settings = []
    for name, value in values.items():
        settings.append(
            ResolvedSettingSource(
                name=name,
                value=value,
                source=sources[name],
                locked=True,
            )
        )
        if isinstance(value, dict):
            for key, item in value.items():
                setting_name = f"{name}.{key}"
                settings.append(
                    ResolvedSettingSource(
                        name=setting_name,
                        value=item,
                        source=sources.get(setting_name, sources[name]),
                        locked=True,
                    )
                )
    return ResolvedSettings(
        settings=settings,
        execution_hash=effective_execution_hash(version, values, dependencies),
        policy_hash=effective_policy_hash({**values, **dependencies}),
        schema_version=version.schema_version,
    )


def update_draft(
    session: Session, template_id: str, payload: PipelineDraftUpdate
) -> PipelineTemplate:
    begin_write(session)
    model = get_or_404(session, PipelineTemplateModel, template_id)
    if model.version != payload.expected_version:
        raise AppError("version_conflict", "Черновик изменён в другом месте", 409)
    if model.archived_at is not None or model.kind == "system":
        raise AppError("template_immutable", "Шаблон недоступен для правки", 409)
    draft = payload.model_dump(exclude={"expected_version"})
    if json.loads(model.draft_json).get("origin") == "imported":
        draft["origin"] = "imported"
    model.draft_json = to_json(draft)
    model.version += 1
    model.updated_at = utc_now()
    session.flush()
    return template_from_model(model)


def publish_draft(session: Session, template_id: str, expected_version: int) -> PipelineVersion:
    import json

    from pydantic import ValidationError

    begin_write(session)
    model = get_or_404(session, PipelineTemplateModel, template_id)
    if model.version != expected_version:
        raise AppError("version_conflict", "Черновик изменён в другом месте", 409)
    try:
        payload = PipelineVersionCreate.model_validate(json.loads(model.draft_json))
    except ValidationError:
        raise AppError("draft_incomplete", "Черновик не содержит структуры графа", 422) from None
    return create_version(session, template_id, payload)


def _binding_has_active_runs(session: Session, binding_id: str) -> bool:
    from agents_ide.persistence.models import Run as RunModel

    stmt = select(RunModel.id).where(
        RunModel.binding_id == binding_id,
        RunModel.state.notin_(["completed", "failed", "cancelled"]),
    )
    return session.execute(stmt).first() is not None
