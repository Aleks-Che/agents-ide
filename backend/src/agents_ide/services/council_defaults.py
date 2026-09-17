"""Saved Council composition; external access is checked when a job starts."""

import json

from sqlalchemy.orm import Session

from agents_ide.domain.common import to_json
from agents_ide.domain.planning import PlanningCouncilDefaults
from agents_ide.domain.schemas import DirectAgentSelection, DirectLLMSelection, GroupSelection
from agents_ide.errors import AppError
from agents_ide.persistence.models import HarnessProfile, ModelGroup, ProviderConnection
from agents_ide.persistence.models import PlanningCouncilDefaults as DefaultsRow
from agents_ide.services.transactions import begin_write


def get_defaults(session: Session) -> PlanningCouncilDefaults:
    row = session.get(DefaultsRow, 1)
    return (
        PlanningCouncilDefaults(
            participants=json.loads(row.participants_json), revision=row.revision
        )
        if row
        else PlanningCouncilDefaults()
    )


def save_defaults(session: Session, payload: PlanningCouncilDefaults) -> PlanningCouncilDefaults:
    begin_write(session)
    row = session.get(DefaultsRow, 1)
    if payload.revision != (row.revision if row else 0):
        raise AppError(
            "version_conflict", "Состав совета изменён в другом месте. Обновите настройки.", 409
        )
    for member in payload.participants:
        selection = member.selection
        resource: HarnessProfile | ProviderConnection | ModelGroup | None
        if isinstance(selection, DirectAgentSelection):
            resource = session.get(HarnessProfile, selection.harness_profile_id)
        elif isinstance(selection, DirectLLMSelection):
            resource = session.get(ProviderConnection, selection.provider_connection_id)
        elif isinstance(selection, GroupSelection):
            resource = session.get(ModelGroup, selection.group_id)
        else:
            raise AppError("configuration_invalid", "Неподдерживаемый выбор модели", 422)
        if resource is None or resource.archived_at is not None:
            raise AppError("resource_unavailable", "Выберите доступную модель или группу", 422)
    if row is None:
        row = DefaultsRow(id=1, revision=0)
        session.add(row)
    row.participants_json = to_json([member.model_dump() for member in payload.participants])
    row.revision += 1
    session.flush()
    return get_defaults(session)
