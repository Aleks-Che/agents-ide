"""Atomic revisioned model groups and portable, credential-free definitions."""

import json
from datetime import UTC, datetime
from typing import Any, Literal, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from agents_ide.domain.common import assert_safe_name, new_id, to_json, utc_now
from agents_ide.domain.schemas import (
    ModelGroup,
    ModelGroupAgentCreate,
    ModelGroupAgentMemberCreate,
    ModelGroupAgentMembersReplace,
    ModelGroupAgentUpdate,
    ModelGroupCopy,
    ModelGroupExport,
    ModelGroupImport,
    ModelGroupLLMCreate,
    ModelGroupLLMMemberCreate,
    ModelGroupLLMMembersReplace,
    ModelGroupLLMUpdate,
    ModelGroupMember,
    ModelGroupMemberDelete,
    PortableGroupMember,
    validate_model_params,
)
from agents_ide.errors import AppError
from agents_ide.persistence.models import HarnessProfile, ProviderConnection
from agents_ide.persistence.models import ModelGroup as GroupRow
from agents_ide.persistence.models import ModelGroupMember as MemberRow
from agents_ide.services.mapping import get_or_404
from agents_ide.services.transactions import begin_write

MemberInput = ModelGroupAgentMemberCreate | ModelGroupLLMMemberCreate
GroupKind = Literal["agent", "llm"]


def _params(raw: str) -> dict[str, Any]:
    try:
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("Expected parameters object")
        validate_model_params(result)
        return result
    except (ValueError, TypeError):
        raise AppError(
            "configuration_invalid", "Исправьте недопустимые параметры кандидата", 422
        ) from None


def _check_revision(model: GroupRow, revision: int) -> None:
    if model.revision != revision:
        raise AppError(
            "version_conflict",
            "Группа изменена в другом месте",
            409,
            {"expected_revision": revision, "actual_revision": model.revision},
        )


def _editable(model: GroupRow, revision: int, kind: GroupKind | None = None) -> None:
    _check_revision(model, revision)
    if model.archived_at is not None:
        raise AppError("model_group_archived", "Архивная группа недоступна для правки", 409)
    if kind is not None and model.kind != kind:
        raise AppError("model_group_kind_mismatch", "Тип группы не совпадает", 422)


def _changed(model: GroupRow) -> None:
    model.revision += 1
    model.version += 1
    model.updated_at = utc_now()


def create_group(
    session: Session, payload: ModelGroupAgentCreate | ModelGroupLLMCreate
) -> ModelGroup:
    begin_write(session)
    assert_safe_name(payload.name)
    kind = "agent" if isinstance(payload, ModelGroupAgentCreate) else "llm"
    now = utc_now()
    model = GroupRow(
        id=new_id(),
        name=payload.name,
        description=payload.description,
        kind=kind,
        revision=1,
        version=1,
        created_at=now,
        updated_at=now,
    )
    session.add(model)
    session.flush()
    _replace_members(session, model, list(payload.members))
    return _output(model)


def list_groups(
    session: Session, kind: GroupKind | None = None, include_archived: bool = False
) -> list[ModelGroup]:
    stmt = select(GroupRow).order_by(GroupRow.created_at.desc(), GroupRow.id)
    if kind is not None:
        stmt = stmt.where(GroupRow.kind == kind)
    if not include_archived:
        stmt = stmt.where(GroupRow.archived_at.is_(None))
    return [_output(row) for row in session.scalars(stmt)]


def get_group(session: Session, group_id: str) -> ModelGroup:
    return _output(get_or_404(session, GroupRow, group_id))


def update_group(
    session: Session, group_id: str, payload: ModelGroupAgentUpdate | ModelGroupLLMUpdate
) -> ModelGroup:
    begin_write(session)
    model = get_or_404(session, GroupRow, group_id)
    _editable(
        model,
        payload.expected_revision,
        "agent" if isinstance(payload, ModelGroupAgentUpdate) else "llm",
    )
    if payload.name is not None:
        assert_safe_name(payload.name)
        model.name = payload.name
    if payload.description is not None:
        model.description = payload.description
    if payload.members is not None:
        _replace_members(session, model, list(payload.members))
    _changed(model)
    session.flush()
    return _output(model)


def replace_members(
    session: Session,
    group_id: str,
    payload: ModelGroupAgentMembersReplace | ModelGroupLLMMembersReplace,
) -> ModelGroup:
    begin_write(session)
    model = get_or_404(session, GroupRow, group_id)
    _editable(
        model,
        payload.expected_revision,
        "agent" if isinstance(payload, ModelGroupAgentMembersReplace) else "llm",
    )
    _replace_members(session, model, list(payload.members))
    _changed(model)
    session.flush()
    return _output(model)


def _member_input(member: MemberRow, *, keep_id: bool = True) -> MemberInput:
    fields: dict[str, Any] = dict(
        id=member.id if keep_id else None,
        enabled=member.enabled,
        model_id=member.model_id,
        params=_params(member.params_json),
        schedule=json.loads(member.schedule_json) if member.schedule_json else None,
    )
    if member.harness_profile_id is not None:
        return ModelGroupAgentMemberCreate(harness_profile_id=member.harness_profile_id, **fields)
    return ModelGroupLLMMemberCreate(
        provider_connection_id=cast(str, member.provider_connection_id), **fields
    )


def delete_member(
    session: Session, group_id: str, member_id: str, payload: ModelGroupMemberDelete
) -> ModelGroup:
    begin_write(session)
    model = get_or_404(session, GroupRow, group_id)
    _editable(model, payload.expected_revision)
    members = sorted(model.members, key=lambda m: m.member_index)
    if not any(m.id == member_id for m in members):
        raise AppError("model_group_member_not_found", "Кандидат не найден", 404)
    _replace_members(session, model, [_member_input(m) for m in members if m.id != member_id])
    _changed(model)
    session.flush()
    return _output(model)


def archive_group(session: Session, group_id: str, expected_revision: int) -> ModelGroup:
    begin_write(session)
    model = get_or_404(session, GroupRow, group_id)
    _check_revision(model, expected_revision)
    if model.archived_at is None:
        model.archived_at = utc_now()
        _changed(model)
        session.flush()
    return _output(model)


def copy_group(session: Session, group_id: str, payload: ModelGroupCopy) -> ModelGroup:
    begin_write(session)
    source = get_or_404(session, GroupRow, group_id)
    _check_revision(source, payload.expected_revision)
    fields: dict[str, Any] = dict(
        name=payload.name,
        description=source.description if payload.description is None else payload.description,
        members=[
            _member_input(m, keep_id=False).model_dump(exclude_none=True)
            for m in sorted(source.members, key=lambda m: m.member_index)
        ],
    )
    definition = (
        ModelGroupAgentCreate if source.kind == "agent" else ModelGroupLLMCreate
    ).model_validate(fields)
    return create_group(session, definition)


def load_group_snapshot(session: Session, group_id: str) -> tuple[GroupRow, list[MemberRow]] | None:
    model = session.get(GroupRow, group_id)
    if model is None or model.archived_at is not None:
        return None
    return model, sorted(model.members, key=lambda m: m.member_index)


def _replace_members(session: Session, group: GroupRow, payloads: list[MemberInput]) -> None:
    if not payloads or not any(p.enabled for p in payloads):
        raise AppError("model_group_empty", "Нужен хотя бы один включённый кандидат", 422)
    existing = {m.id: m for m in group.members}
    by_pair = {
        (m.harness_profile_id or m.provider_connection_id, m.model_id): m for m in existing.values()
    }
    seen_pairs: set[tuple[str, str]] = set()
    seen_ids: set[str] = set()
    ordered: list[tuple[MemberRow, MemberInput]] = []
    now = utc_now()
    for index, payload in enumerate(payloads):
        agent = isinstance(payload, ModelGroupAgentMemberCreate)
        if group.kind != ("agent" if agent else "llm"):
            raise AppError("model_group_kind_mismatch", "Тип кандидата не совпадает", 422)
        ref = (
            payload.harness_profile_id
            if isinstance(payload, ModelGroupAgentMemberCreate)
            else payload.provider_connection_id
        )
        pair = (ref, payload.model_id)
        if pair in seen_pairs:
            raise AppError("model_group_duplicate", "Дубликат исполнителя и модели", 422)
        seen_pairs.add(pair)
        member = existing.get(payload.id) if payload.id else by_pair.get(pair)
        if payload.id and member is None:
            raise AppError("model_group_member_not_found", "ID не принадлежит группе", 422)
        resource = (
            session.get(HarnessProfile, ref) if agent else session.get(ProviderConnection, ref)
        )
        # Existing archived references remain editable/removable; new references must be active.
        unchanged_ref = (
            member is not None
            and (member.harness_profile_id or member.provider_connection_id) == ref
        )
        if resource is None or (resource.archived_at is not None and not unchanged_ref):
            raise AppError(
                "harness_unavailable" if agent else "connection_unavailable",
                "Исполнитель недоступен",
                409,
            )
        if member is None:
            member = MemberRow(
                id=new_id(),
                group_id=group.id,
                member_index=index,
                revision=1,
                created_at=now,
                updated_at=now,
            )
        if member.id in seen_ids:
            raise AppError("model_group_duplicate", "ID кандидата повторён", 422)
        seen_ids.add(member.id)
        ordered.append((member, payload))
    for member in existing.values():
        if member.id not in seen_ids:
            session.delete(member)
    session.flush()
    # Vacate all positions before applying a permutation; SQLite checks UNIQUE row by row.
    old_indexes = {m.id: m.member_index for m in existing.values()}
    old_values = {
        m.id: (
            m.enabled,
            m.harness_profile_id,
            m.provider_connection_id,
            m.model_id,
            m.params_json,
            m.schedule_json,
        )
        for m in existing.values()
    }
    offset = max(old_indexes.values(), default=0) + len(payloads) + 1
    for index, (member, _) in enumerate(ordered):
        if member.id in existing:
            member.member_index = offset + index
            member.model_id = "__reorder__" + new_id()
    session.flush()
    for index, (member, payload) in enumerate(ordered):
        profile = (
            payload.harness_profile_id if isinstance(payload, ModelGroupAgentMemberCreate) else None
        )
        connection = (
            payload.provider_connection_id
            if isinstance(payload, ModelGroupLLMMemberCreate)
            else None
        )
        values = (
            payload.enabled,
            profile,
            connection,
            payload.model_id,
            to_json(payload.params),
            to_json(payload.schedule.model_dump()) if payload.schedule is not None else None,
        )
        previous = old_values.get(member.id)
        if member.id in existing and (old_indexes[member.id] != index or values != previous):
            member.revision += 1
            member.updated_at = now
        member.member_index = index
        (
            member.enabled,
            member.harness_profile_id,
            member.provider_connection_id,
            member.model_id,
            member.params_json,
            member.schedule_json,
        ) = values
        session.add(member)
    session.flush()
    session.expire(group, ["members"])


def export_group(session: Session, group_id: str) -> ModelGroupExport:
    model = get_or_404(session, GroupRow, group_id)
    return ModelGroupExport(
        kind=cast(GroupKind, model.kind),
        name=model.name,
        description=model.description,
        members=[
            PortableGroupMember(
                resource_ref=cast(str, m.harness_profile_id or m.provider_connection_id),
                model_id=m.model_id,
                enabled=m.enabled,
                params=_params(m.params_json),
                schedule=json.loads(m.schedule_json) if m.schedule_json else None,
            )
            for m in sorted(model.members, key=lambda m: m.member_index)
        ],
    )


def import_group(session: Session, payload: ModelGroupImport) -> ModelGroup:
    begin_write(session)
    definition = payload.definition
    required = {m.resource_ref for m in definition.members}
    if set(payload.resource_bindings) != required:
        raise AppError(
            "model_group_bindings_required", "Привяжите все внешние профили/подключения", 422
        )
    field = "harness_profile_id" if definition.kind == "agent" else "provider_connection_id"
    fields: dict[str, Any] = dict(
        name=payload.name or definition.name,
        description=definition.description,
        members=[
            {
                field: payload.resource_bindings[m.resource_ref],
                "model_id": m.model_id,
                "enabled": m.enabled,
                "params": m.params,
                "schedule": m.schedule.model_dump() if m.schedule is not None else None,
            }
            for m in definition.members
        ],
    )
    model = (
        ModelGroupAgentCreate if definition.kind == "agent" else ModelGroupLLMCreate
    ).model_validate(fields)
    return create_group(session, model)  # Unique name conflict never overwrites local data.


def _output(model: GroupRow) -> ModelGroup:
    return ModelGroup(
        id=model.id,
        name=model.name,
        description=model.description,
        kind=cast(GroupKind, model.kind),
        revision=model.revision,
        archived=model.archived_at is not None,
        version=model.version,
        created_at=datetime.fromtimestamp(model.created_at, UTC),
        updated_at=datetime.fromtimestamp(model.updated_at, UTC),
        members=[
            ModelGroupMember(
                id=m.id,
                member_index=m.member_index,
                enabled=m.enabled,
                harness_profile_id=m.harness_profile_id,
                provider_connection_id=m.provider_connection_id,
                model_id=m.model_id,
                params=_params(m.params_json),
                schedule=json.loads(m.schedule_json) if m.schedule_json else None,
                revision=m.revision,
                updated_at=datetime.fromtimestamp(m.updated_at, UTC),
            )
            for m in sorted(model.members, key=lambda m: m.member_index)
        ],
    )
