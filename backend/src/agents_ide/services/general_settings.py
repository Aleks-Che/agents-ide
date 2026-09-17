"""Application defaults with optimistic concurrency."""

import json

from sqlalchemy.orm import Session

from agents_ide.domain.common import to_json
from agents_ide.domain.schemas import GeneralSettings
from agents_ide.errors import AppError
from agents_ide.persistence.models import GeneralSettings as SettingsRow
from agents_ide.persistence.models import ProviderConnection
from agents_ide.services.transactions import begin_write


def get_settings(session: Session) -> GeneralSettings:
    row = session.get(SettingsRow, 1)
    return (
        GeneralSettings(**json.loads(row.settings_json), revision=row.revision)
        if row
        else GeneralSettings()
    )


def save_settings(session: Session, payload: GeneralSettings) -> GeneralSettings:
    begin_write(session)
    row = session.get(SettingsRow, 1)
    if payload.revision != (row.revision if row else 0):
        raise AppError("version_conflict", "Общие настройки изменены в другом месте.", 409)
    if payload.commit_message.connection_id:
        connection = session.get(ProviderConnection, payload.commit_message.connection_id)
        if connection is None or connection.archived_at is not None:
            raise AppError("connection_unavailable", "Выберите доступное LLM-подключение", 422)
    if row is None:
        row = SettingsRow(id=1, revision=0)
        session.add(row)
    row.settings_json = to_json(payload.model_dump(exclude={"revision"}))
    row.revision += 1
    session.flush()
    return get_settings(session)
