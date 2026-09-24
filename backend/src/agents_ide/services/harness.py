"""Harness profile service (Codex, OpenCode, future kinds)."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from agents_ide.domain.common import assert_safe_name, new_id, to_json, utc_now
from agents_ide.domain.schemas import (
    HarnessCatalogModel,
    HarnessProbe,
    HarnessProfile,
    HarnessProfileCatalog,
    HarnessProfileCreate,
    HarnessProfileUpdate,
)
from agents_ide.errors import AppError
from agents_ide.persistence.models import HarnessProfile as HarnessProfileModel
from agents_ide.security.native_credentials import fingerprint
from agents_ide.services.mapping import ensure_unique, get_or_404, harness_from_model
from agents_ide.services.transactions import begin_write

_catalog_locks = {kind: threading.Lock() for kind in ("codex", "opencode")}


def discover_harnesses(session: Session) -> list[HarnessProfile]:
    """One shared settings record per installed harness; keep legacy references intact."""
    from agents_ide.services.harness_discovery import discover_executables, native_executable

    executables = discover_executables()
    begin_write(session)
    rows = list(
        session.scalars(
            select(HarnessProfileModel).order_by(
                HarnessProfileModel.created_at, HarnessProfileModel.id
            )
        )
    )
    discovered = []
    for kind, name in (("codex", "Codex"), ("opencode", "OpenCode")):
        active = [row for row in rows if row.harness_kind == kind and row.archived_at is None]
        executable = executables.get(kind) or next(
            (
                r.executable_path
                for r in active
                if r.executable_path and native_executable(Path(r.executable_path))
            ),
            None,
        )
        if executable is None:
            continue
        identity = uuid5(NAMESPACE_URL, f"agents-ide:installed-harness:{kind}").hex
        model = next((r for r in rows if r.id == identity), None)
        if model is None and active:
            # Stable across executable upgrades, including installations that
            # previously had several model-specific profiles.
            model = active[0]
        if model is None:
            now = utc_now()
            used_names = {r.name for r in rows}
            title = name
            suffix = 1
            while title in used_names:
                suffix += 1
                title = f"{name} {suffix}"
            model = HarnessProfileModel(
                id=identity,
                name=title,
                harness_kind=kind,
                executable_path=executable,
                settings_json=to_json(
                    {
                        "permission_mode": "read_only" if kind == "codex" else "native",
                    }
                ),
                catalog_models_json="[]",
                catalog_metadata_json="{}",
                catalog_fingerprint=fingerprint(kind, executable),
                catalog_ttl_seconds=900,
                version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(model)
        elif model.executable_path != executable or model.archived_at is not None:
            model.executable_path = executable
            model.archived_at = None
            model.catalog_fingerprint = None
        settings = json.loads(model.settings_json)
        if settings.get("permission_mode") is None:
            settings["permission_mode"] = "read_only" if kind == "codex" else "native"
            model.settings_json = to_json(settings)
            model.version += 1
            model.updated_at = utc_now()
        invalidate_native_catalog(session, model)
        session.flush()
        discovered.append(harness_from_model(model))
    return discovered


def refresh_model_catalog(
    session: Session, harness_id: str, force: bool = False
) -> HarnessProfileCatalog:
    model = get_or_404(session, HarnessProfileModel, harness_id)
    with _catalog_locks[model.harness_kind]:
        session.refresh(model)
        catalog = model_catalog(session, harness_id)
        session.commit()  # Publish invalidation before probe releases its read snapshot.
        if force or catalog.status != "fresh":
            probe = probe_harness(session, harness_id)
            session.commit()
            if probe.status != "ok":
                raise AppError(
                    "harness_catalog_unavailable",
                    "Не удалось загрузить модели. Проверьте вход и настройки самой harness.",
                    422,
                )
        return model_catalog(session, harness_id)


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
        catalog_fingerprint=fingerprint(payload.harness_kind, payload.executable_path),
        catalog_metadata_json="{}",
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
    rows = session.scalars(stmt).all()
    for row in rows:
        invalidate_native_catalog(session, row)
    return [harness_from_model(row) for row in rows]


def get_harness(session: Session, harness_id: str) -> HarnessProfile:
    model = get_or_404(session, HarnessProfileModel, harness_id)
    invalidate_native_catalog(session, model)
    return harness_from_model(model)


def invalidate_native_catalog(session: Session, model: HarnessProfileModel) -> None:
    current = fingerprint(model.harness_kind, model.executable_path)
    if model.catalog_fingerprint == current:
        return
    begin_write(session)
    model.catalog_models_json = "[]"
    model.catalog_metadata_json = "{}"
    model.catalog_fetched_at = None
    model.catalog_fingerprint = current
    model.last_test_status = None
    model.last_test_at = None
    model.version += 1
    model.updated_at = utc_now()
    session.flush()


def model_catalog_is_verified(model: HarnessProfileModel) -> bool:
    return model.catalog_fetched_at is not None and model.catalog_fingerprint == fingerprint(
        model.harness_kind, model.executable_path
    )


def current_model_metadata(model: HarnessProfileModel) -> dict[str, Any]:
    """Last confirmed options for this executable/account/configuration.

    TTL controls catalog refresh, not validity of already confirmed parameters.
    Dispatch fetches and validates the live catalog again. A changed native
    fingerprint still invalidates all cached options immediately.
    """
    if not model_catalog_is_verified(model):
        return {}
    metadata: dict[str, Any] = json.loads(model.catalog_metadata_json or "{}")
    return metadata


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
        from agents_ide.engine import codex_runtime, opencode_runtime

        validator = (
            codex_runtime.validate_settings
            if model.harness_kind == "codex"
            else opencode_runtime.validate_settings
        )
        validator(payload.settings, execution="permission_mode" in payload.settings)
        previous_settings = json.loads(model.settings_json)
        default_model = payload.settings.get("default_model")
        if default_model is not None and not isinstance(default_model, str):
            raise AppError("harness_model_unavailable", "Выберите модель из каталога harness", 422)
        if default_model is not None and default_model != previous_settings.get("default_model"):
            catalog = model_catalog(session, harness_id)
            if catalog.status != "fresh" or default_model not in {m.id for m in catalog.models}:
                raise AppError(
                    "harness_model_unavailable",
                    "Выберите модель из актуального каталога harness",
                    422,
                )
        model.settings_json = to_json(payload.settings)
        # Execution permissions and IDE defaults do not change model options.
        execution_keys = {"default_model", "permission_mode", "auto_approve", "approval_policy"}
        if {k: v for k, v in previous_settings.items() if k not in execution_keys} != {
            k: v for k, v in payload.settings.items() if k not in execution_keys
        }:
            model.last_test_status = None
            model.last_test_at = None
            model.catalog_models_json = "[]"
            model.catalog_metadata_json = "{}"
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
    invalidate_native_catalog(session, model)
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
        models=[
            HarnessCatalogModel(
                id=item,
                **json.loads(model.catalog_metadata_json or "{}").get(item, {"source": "cache"}),
            )
            for item in sorted(cached)
        ],
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
    expected_fingerprint = fingerprint(harness_kind, executable)
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
    if fingerprint(harness_kind, executable) != expected_fingerprint:
        raise AppError("version_conflict", "Native credentials changed during probe", 409)
    if version is None:
        record_probe_result(session, model, "failed", None, detail)
        return HarnessProbe(
            status="failed",
            version=None,
            detail=detail,
            tested_at=datetime.now(tz=UTC),
        )
    model.catalog_models_json = to_json(list(models))
    model.catalog_metadata_json = to_json(getattr(models, "metadata", {}))
    model.catalog_fingerprint = expected_fingerprint
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
