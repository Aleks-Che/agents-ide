"""FastAPI dependency providers for the API layer."""

from __future__ import annotations

from collections.abc import Generator

from fastapi import Request
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm.exc import StaleDataError

from agents_ide.config import Settings
from agents_ide.errors import AppError
from agents_ide.security.secrets import SecretStore


def get_settings(request: Request) -> Settings:
    settings: Settings | None = getattr(request.app.state, "settings", None)
    if settings is None:
        raise AppError("settings_unavailable", "Settings not initialized", 500)
    return settings


def get_session_factory(request: Request) -> sessionmaker[Session]:
    factory: sessionmaker[Session] | None = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise AppError("database_unavailable", "Database session factory not initialized", 500)
    return factory


def get_session(request: Request) -> Generator[Session, None, None]:
    factory = get_session_factory(request)
    session = factory()
    session.info["data_dir"] = get_settings(request).data_dir
    try:
        yield session
        session.commit()
    except (IntegrityError, StaleDataError):
        session.rollback()
        raise AppError("conflict", "Данные изменены или нарушена связь объектов", 409) from None
    except OperationalError:
        session.rollback()
        raise AppError(
            "database_unavailable", "База данных временно недоступна", 503, retryable=True
        ) from None
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_secret_store(request: Request) -> SecretStore:
    store: SecretStore | None = getattr(request.app.state, "secrets", None)
    if store is None:
        raise AppError("secret_store_unavailable", "SecretStore not initialized", 500)
    return store
