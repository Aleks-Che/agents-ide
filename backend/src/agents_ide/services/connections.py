"""Provider connection service backed by SecretStore."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from agents_ide.domain.common import assert_safe_name, new_id, to_json, utc_now
from agents_ide.domain.schemas import (
    ProviderConnection,
    ProviderConnectionCreate,
    ProviderConnectionUpdate,
    ProviderTest,
)
from agents_ide.errors import AppError
from agents_ide.persistence.models import ProviderConnection as ProviderConnectionModel
from agents_ide.security.secrets import SecretStore
from agents_ide.services.mapping import (
    ensure_unique,
    get_or_404,
    provider_from_model,
)
from agents_ide.services.transactions import begin_write


def create_connection(
    session: Session,
    secrets: SecretStore,
    payload: ProviderConnectionCreate,
) -> ProviderConnection:
    begin_write(session)
    assert_safe_name(payload.name)
    _validate_url(payload.base_url)
    reference: str | None = None
    if payload.secret:
        reference = secrets.put(payload.secret)
    protocol = _protocol_of(payload.base_url)
    now = utc_now()
    model = ProviderConnectionModel(
        id=new_id(),
        name=payload.name,
        provider_kind=payload.provider_kind,
        base_url=payload.base_url,
        protocol=protocol,
        secret_reference=reference,
        manual_models_json=to_json(payload.manual_models),
        catalog_models_json="[]",
        catalog_fetched_at=None,
        catalog_ttl_seconds=payload.catalog_ttl_seconds,
        last_test_status=None,
        last_test_at=None,
        archived_at=None,
        version=1,
        created_at=now,
        updated_at=now,
    )

    def _add() -> ProviderConnection:
        session.add(model)
        session.flush()
        return provider_from_model(model)

    return ensure_unique(session, _add)


def list_connections(session: Session, include_archived: bool = False) -> list[ProviderConnection]:
    stmt = select(ProviderConnectionModel)
    if not include_archived:
        stmt = stmt.where(ProviderConnectionModel.archived_at.is_(None))
    stmt = stmt.order_by(ProviderConnectionModel.created_at.desc())
    return [provider_from_model(row) for row in session.scalars(stmt).all()]


def get_connection(session: Session, connection_id: str) -> ProviderConnection:
    return provider_from_model(get_or_404(session, ProviderConnectionModel, connection_id))


def update_connection(
    session: Session,
    secrets: SecretStore,
    connection_id: str,
    payload: ProviderConnectionUpdate,
) -> ProviderConnection:
    begin_write(session)
    model = get_or_404(session, ProviderConnectionModel, connection_id)
    if model.version != payload.expected_version:
        raise AppError(
            "version_conflict",
            "Подключение изменено в другом месте",
            409,
            {"expected_version": payload.expected_version, "actual_version": model.version},
        )
    if model.archived_at is not None:
        raise AppError("connection_archived", "Архивное подключение недоступно", 409)
    if payload.name is not None:
        assert_safe_name(payload.name)
        model.name = payload.name
    if payload.base_url is not None:
        _validate_url(payload.base_url)
        model.base_url = payload.base_url
        model.protocol = _protocol_of(payload.base_url)
        model.catalog_models_json = "[]"
        model.catalog_fetched_at = None
        model.last_test_status = None
        model.last_test_at = None
    if payload.secret is not None:
        new_reference = secrets.put(payload.secret)
        old_reference = model.secret_reference
        model.secret_reference = new_reference
        model.catalog_models_json = "[]"
        model.catalog_fetched_at = None
        model.last_test_status = None
        model.last_test_at = None
        if old_reference:
            # Old ciphertext stays available for in-flight Runs until they end.
            # Cleanup is the GC story from SECURITY_AND_OPERATIONS §6.
            pass
    if payload.manual_models is not None:
        model.manual_models_json = to_json(payload.manual_models)
    if payload.catalog_ttl_seconds is not None:
        model.catalog_ttl_seconds = payload.catalog_ttl_seconds
    model.version += 1
    model.updated_at = utc_now()
    session.flush()
    return provider_from_model(model)


def archive_connection(
    session: Session, connection_id: str, expected_version: int
) -> ProviderConnection:
    begin_write(session)
    model = get_or_404(session, ProviderConnectionModel, connection_id)
    if model.version != expected_version:
        raise AppError(
            "version_conflict",
            "Подключение изменено в другом месте",
            409,
            {"expected_version": expected_version, "actual_version": model.version},
        )
    model.archived_at = utc_now()
    model.version += 1
    model.updated_at = utc_now()
    session.flush()
    return provider_from_model(model)


def record_test_result(
    session: Session,
    connection_id: str,
    status: str,
    models: list[str] | None,
    detail: str | None,
) -> ProviderConnection:
    begin_write(session)
    model = get_or_404(session, ProviderConnectionModel, connection_id)
    model.last_test_status = status
    model.last_test_at = utc_now()
    if status == "ok" and models is not None:
        model.catalog_models_json = to_json(models)
        model.catalog_fetched_at = utc_now()
    model.version += 1
    model.updated_at = utc_now()
    session.flush()
    return provider_from_model(model)


def combined_catalog(model: ProviderConnectionModel) -> list[str]:
    import json

    manual = json.loads(model.manual_models_json or "[]")
    catalog = json.loads(model.catalog_models_json or "[]")
    return sorted({*manual, *catalog})


def _validate_url(value: str) -> None:
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise AppError("connection_url_invalid", "Неверный адрес подключения", 400) from None
    if (
        not parts.hostname
        or parts.query
        or parts.fragment
        or "\\" in value
        or any(char.isspace() for char in value)
        or (port is not None and port < 1)
    ):
        raise AppError(
            "connection_url_invalid", "URL должен содержать host без query/fragment", 400
        )
    if parts.scheme not in {"http", "https"}:
        raise AppError("connection_url_invalid", "Допустимы только http и https", 400)
    if parts.username or parts.password:
        raise AppError("connection_url_invalid", "URL не должен содержать учётные данные", 400)
    if parts.scheme == "https":
        return
    # Plain HTTP is allowed only for loopback providers.
    host = (parts.hostname or "").lower()
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise AppError("connection_url_invalid", "Удалённый провайдер требует HTTPS", 400)


def _protocol_of(value: str) -> str:
    parts = urlsplit(value)
    return parts.scheme or "http"


def model_catalog(session: Session, connection_id: str) -> dict[str, Any]:
    import json

    model = get_or_404(session, ProviderConnectionModel, connection_id)
    manual = set(json.loads(model.manual_models_json))
    cached = set(json.loads(model.catalog_models_json))
    stale = (
        model.catalog_fetched_at is None
        or utc_now() - model.catalog_fetched_at > model.catalog_ttl_seconds
    )
    return {
        "connection_id": connection_id,
        "status": "unverified"
        if model.catalog_fetched_at is None
        else "stale"
        if stale
        else "fresh",
        "models": [
            {"id": item, "source": "manual" if item in manual else "catalog"}
            for item in sorted(manual | cached)
        ],
        "fetched_at": model.catalog_fetched_at,
        "ttl_seconds": model.catalog_ttl_seconds,
    }


def build_test_result(
    session: Session,
    connection_id: str,
    models: list[str],
    detail: str | None = None,
) -> ProviderTest:
    record_test_result(session, connection_id, "ok", models, detail)
    return ProviderTest(status="ok", models=models, detail=detail, tested_at=datetime.now(tz=UTC))


def failed_test(
    session: Session,
    connection_id: str,
    detail: str,
) -> ProviderTest:
    record_test_result(session, connection_id, "failed", None, detail)
    return ProviderTest(status="failed", models=[], detail=detail, tested_at=datetime.now(tz=UTC))


def connection_payload_for_export(
    model: ProviderConnectionModel,
) -> dict[str, Any]:
    """Return metadata safe for export. Secret reference is kept, value is never."""

    return {
        "id": model.id,
        "name": model.name,
        "provider_kind": model.provider_kind,
        "base_url": model.base_url,
        "protocol": model.protocol,
        "has_secret": model.secret_reference is not None,
        "manual_models": __import__("json").loads(model.manual_models_json or "[]"),
        "catalog_models": __import__("json").loads(model.catalog_models_json or "[]"),
        "version": model.version,
    }
