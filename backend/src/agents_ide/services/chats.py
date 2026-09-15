"""Chat and message services."""

from __future__ import annotations

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from agents_ide.domain.common import new_id, utc_now
from agents_ide.domain.schemas import (
    Chat,
    ChatArchive,
    ChatCreate,
    ChatUpdate,
    Message,
    MessageCreate,
    MessageUpdate,
)
from agents_ide.errors import AppError
from agents_ide.persistence.models import Chat as ChatModel
from agents_ide.persistence.models import Message as MessageModel
from agents_ide.persistence.models import Project as ProjectModel
from agents_ide.services.mapping import (
    chat_from_model,
    ensure_unique,
    get_or_404,
    message_from_model,
)
from agents_ide.services.transactions import begin_write


def create_chat(session: Session, project_id: str, payload: ChatCreate) -> Chat:
    begin_write(session)
    if session.get(ProjectModel, project_id) is None:
        raise AppError("project_not_found", "Проект не найден", 404)
    if get_or_404(session, ProjectModel, project_id).archived_at is not None:
        raise AppError("project_archived", "Архивный проект недоступен", 409)
    now = utc_now()
    model = ChatModel(
        id=new_id(),
        project_id=project_id,
        title=payload.title,
        archived_at=None,
        version=1,
        created_at=now,
        updated_at=now,
    )

    def _add() -> Chat:
        session.add(model)
        session.flush()
        return chat_from_model(model)

    return ensure_unique(session, _add)


def list_chats(session: Session, project_id: str, include_archived: bool = False) -> list[Chat]:
    if session.get(ProjectModel, project_id) is None:
        raise AppError("project_not_found", "Проект не найден", 404)
    stmt = select(ChatModel).where(ChatModel.project_id == project_id)
    if not include_archived:
        stmt = stmt.where(ChatModel.archived_at.is_(None))
    stmt = stmt.order_by(ChatModel.created_at.desc())
    return [chat_from_model(row) for row in session.scalars(stmt).all()]


def get_chat(session: Session, chat_id: str) -> Chat:
    return chat_from_model(get_or_404(session, ChatModel, chat_id))


def archive_chat(session: Session, chat_id: str, payload: ChatArchive) -> Chat:
    begin_write(session)
    model = get_or_404(session, ChatModel, chat_id)
    if model.version != payload.expected_version:
        raise AppError(
            "version_conflict",
            "Чат изменён в другом месте",
            409,
            {"expected_version": payload.expected_version, "actual_version": model.version},
        )
    if payload.archive:
        from agents_ide.persistence.models import Run

        if session.scalar(
            select(Run.id).where(
                Run.chat_id == chat_id, Run.state.notin_(["completed", "failed", "cancelled"])
            )
        ):
            raise AppError("chat_has_active_runs", "В чате есть активный Run", 409)
        model.archived_at = utc_now()
    else:
        model.archived_at = None
    model.version += 1
    model.updated_at = utc_now()
    session.flush()
    return chat_from_model(model)


def add_message(session: Session, chat_id: str, payload: MessageCreate) -> Message:
    begin_write(session)
    _editable_chat(session, chat_id)
    model = MessageModel(
        id=new_id(),
        chat_id=chat_id,
        role=payload.role,
        content=payload.content,
        archived_at=None,
        version=1,
        created_at=utc_now(),
    )
    session.add(model)
    session.flush()
    return message_from_model(model)


def list_messages(
    session: Session,
    chat_id: str,
    limit: int = 200,
    *,
    latest: bool = False,
    before_id: str | None = None,
) -> list[Message]:
    if session.get(ChatModel, chat_id) is None:
        raise AppError("chat_not_found", "Чат не найден", 404)
    stmt = select(MessageModel).where(
        MessageModel.chat_id == chat_id, MessageModel.archived_at.is_(None)
    )
    if before_id is not None:
        cursor = session.get(MessageModel, before_id)
        if cursor is None or cursor.chat_id != chat_id:
            raise AppError("message_cursor_invalid", "Курсор сообщения не принадлежит чату", 400)
        stmt = stmt.where(
            or_(
                MessageModel.created_at < cursor.created_at,
                and_(MessageModel.created_at == cursor.created_at, MessageModel.id < cursor.id),
            )
        )
    if latest or before_id is not None:
        stmt = stmt.order_by(MessageModel.created_at.desc(), MessageModel.id.desc()).limit(limit)
        return [message_from_model(row) for row in reversed(session.scalars(stmt).all())]
    stmt = stmt.order_by(MessageModel.created_at.asc(), MessageModel.id.asc()).limit(limit)
    return [message_from_model(row) for row in session.scalars(stmt).all()]


def _editable_chat(session: Session, chat_id: str) -> ChatModel:
    chat = get_or_404(session, ChatModel, chat_id)
    project = get_or_404(session, ProjectModel, chat.project_id)
    if chat.archived_at is not None or project.archived_at is not None:
        raise AppError("chat_archived", "Чат или проект архивирован", 409)
    return chat


def update_chat(session: Session, chat_id: str, payload: ChatUpdate) -> Chat:
    begin_write(session)
    model = _editable_chat(session, chat_id)
    if model.version != payload.expected_version:
        raise AppError("version_conflict", "Чат изменён в другом месте", 409)
    model.title = payload.title
    model.version += 1
    model.updated_at = utc_now()
    session.flush()
    return chat_from_model(model)


def get_message(session: Session, message_id: str) -> Message:
    return message_from_model(get_or_404(session, MessageModel, message_id))


def update_message(session: Session, message_id: str, payload: MessageUpdate) -> Message:
    begin_write(session)
    model = get_or_404(session, MessageModel, message_id)
    _editable_chat(session, model.chat_id)
    if model.version != payload.expected_version or model.archived_at is not None:
        raise AppError("version_conflict", "Сообщение изменено или архивировано", 409)
    model.content = payload.content
    model.version += 1
    session.flush()
    return message_from_model(model)


def archive_message(session: Session, message_id: str, payload: ChatArchive) -> Message:
    begin_write(session)
    model = get_or_404(session, MessageModel, message_id)
    _editable_chat(session, model.chat_id)
    if model.version != payload.expected_version:
        raise AppError("version_conflict", "Сообщение изменено в другом месте", 409)
    model.archived_at = utc_now() if payload.archive else None
    model.version += 1
    session.flush()
    return message_from_model(model)
