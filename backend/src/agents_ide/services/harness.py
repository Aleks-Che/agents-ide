"""Harness profile service (Codex, OpenCode, future kinds)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from agents_ide.domain.common import assert_safe_name, new_id, to_json, utc_now
from agents_ide.domain.schemas import (
    HarnessProbe,
    HarnessProfile,
    HarnessProfileCatalog,
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
        catalog_models_json="[]",
        catalog_fetched_at=None,
        catalog_ttl_seconds=payload.catalog_ttl_seconds,
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


def reset_catalog(session: Session, harness_id: str) -> HarnessProfileModel | None:
    """Invalidate the cached catalog when the profile version, executable
    or its settings move. The next read repopulates it."""
    model = session.get(HarnessProfileModel, harness_id)
    if model is None or model.archived_at is not None:
        return None
    model.catalog_models_json = "[]"
    model.catalog_fetched_at = None
    model.version += 1
    model.updated_at = utc_now()
    session.flush()
    return model


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
    if "executable_path" in payload.model_fields_set:
        model.executable_path = payload.executable_path
        model.catalog_models_json = "[]"
        model.catalog_fetched_at = None
        model.last_test_status = None
        model.last_test_at = None
    if payload.settings is not None:
        model.settings_json = to_json(payload.settings)
        model.last_test_status = None
        model.last_test_at = None
        # Settings include default model parameters or auth hints; the catalog
        # itself does not depend on the secret value, but a version bump keeps
        # the cache honest until the next read.
        model.catalog_models_json = "[]"
        model.catalog_fetched_at = None
    if payload.catalog_ttl_seconds is not None:
        model.catalog_ttl_seconds = payload.catalog_ttl_seconds
        model.catalog_fetched_at = None
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


def model_catalog(session: Session, harness_id: str) -> HarnessProfileCatalog:
    model = get_or_404(session, HarnessProfileModel, harness_id)
    if model.archived_at is not None:
        raise AppError("harness_archived", "Архивный профиль недоступен", 409)
    from datetime import UTC, datetime

    now = utc_now()
    fetched_at = model.catalog_fetched_at
    stale = fetched_at is None or now - fetched_at > model.catalog_ttl_seconds
    cached = set(json.loads(model.catalog_models_json or "[]"))
    status = "unverified" if model.catalog_fetched_at is None else "stale" if stale else "fresh"
    fetched_dt: datetime | None = None
    if model.catalog_fetched_at is not None:
        fetched_dt = datetime.fromtimestamp(model.catalog_fetched_at, tz=UTC)
    return HarnessProfileCatalog(
        status=status,  # type: ignore[arg-type]
        models=[{"id": item, "source": "cache"} for item in sorted(cached)],
        fetched_at=fetched_dt,
        ttl_seconds=model.catalog_ttl_seconds,
    )


def probe_harness(
    session: Session,
    harness_id: str,
) -> HarnessProbe:
    """Probe a harness server attached to a profile.

    OpenCode 6A and Codex 6B are probeable. The probe is an explicit user
    action; the catalog is never accepted as proof of access.
    """

    from datetime import UTC, datetime

    model = get_or_404(session, HarnessProfileModel, harness_id)
    if model.archived_at is not None:
        raise AppError("harness_archived", "Архивный профиль недоступен", 409)
    if model.harness_kind not in {"opencode", "codex"}:
        detail = f"Harness kind {model.harness_kind} is not probeable yet"
        record_probe_result(session, model, "failed", None, detail)
        return HarnessProbe(
            status="failed",
            version=None,
            detail=detail,
            tested_at=datetime.now(tz=UTC),
        )
    executable = str(model.executable_path or "")
    if not executable:
        detail = "executable_path is required for probe"
        record_probe_result(session, model, "failed", None, detail)
        return HarnessProbe(
            status="failed",
            version=None,
            detail=detail,
            tested_at=datetime.now(tz=UTC),
        )
    expected_version = model.version
    harness_kind = model.harness_kind
    settings = json.loads(model.settings_json)
    session.rollback()  # No DB lock or read snapshot while a process starts.
    try:
        if harness_kind == "opencode":
            from agents_ide.engine import opencode_runtime

            version, models = opencode_runtime.probe_executable(executable, settings)
        else:
            import tempfile

            from agents_ide.engine import codex_runtime

            with tempfile.TemporaryDirectory(
                prefix=f"agents-ide-codex-probe-{harness_id[:8]}-"
            ) as probe_dir:
                version, models = codex_runtime.probe_executable(
                    executable, Path(probe_dir), settings
                )
        detail = f"{len(models)} models discovered; model execution and write isolation unverified"
    except (AppError, OSError, ValueError, RuntimeError, httpx.HTTPError) as exc:
        version, models = None, ()
        code = exc.code if isinstance(exc, AppError) else type(exc).__name__
        detail = f"{harness_kind} probe failed: {code}"
    begin_write(session)
    model = get_or_404(session, HarnessProfileModel, harness_id)
    session.refresh(model)
    if model.version != expected_version or model.archived_at is not None:
        raise AppError("version_conflict", "Profile changed during probe", 409)
    if version is None:
        record_probe_result(session, model, "failed", None, detail)
        return HarnessProbe(
            status="failed",
            version=None,
            detail=detail,
            tested_at=datetime.now(tz=UTC),
        )
    model.catalog_models_json = to_json(list(models))
    model.catalog_fetched_at = utc_now()
    model.last_test_status = "ok"
    model.last_test_at = utc_now()
    model.version += 1
    model.updated_at = utc_now()
    session.flush()
    return HarnessProbe(
        status="ok",
        version=version,
        detail=detail,
        tested_at=datetime.now(tz=UTC),
    )


def record_probe_result(
    session: Session,
    model: HarnessProfileModel,
    status: str,
    version: str | None,
    detail: str | None,
) -> None:
    begin_write(session)
    model.last_test_status = status
    model.last_test_at = utc_now()
    if status == "failed":
        model.catalog_models_json = "[]"
        model.catalog_fetched_at = None
    model.version += 1
    model.updated_at = utc_now()
    session.flush()


def profile_payload_for_export(model: HarnessProfileModel) -> dict[str, Any]:
    """Return metadata safe for export. Settings are kept without secrets."""

    return {
        "id": model.id,
        "name": model.name,
        "harness_kind": model.harness_kind,
        "executable_path": model.executable_path,
        "settings": json.loads(model.settings_json or "{}"),
        "catalog_models": json.loads(model.catalog_models_json or "[]"),
        "version": model.version,
        "archived": model.archived_at is not None,
    }
