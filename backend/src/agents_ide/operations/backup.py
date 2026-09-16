"""Verified offline SQLite backups. Restore only into a fresh data directory."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from agents_ide import __version__
from agents_ide.config import Settings
from agents_ide.domain.graph_validation import ValidationReport, check_version_features
from agents_ide.errors import AppError
from agents_ide.operations.maintenance import offline
from agents_ide.persistence.database import SCHEMA_REVISION, create_database, migrate
from agents_ide.persistence.models import Run
from agents_ide.security.filesystem import atomic_write, private_directory
from agents_ide.services.presets import install_all

FORMAT = 1


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def safe_path(root: Path, name: str) -> Path:
    relative = PurePosixPath(name)
    reserved = {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{i}" for i in range(1, 10)),
        *(f"lpt{i}" for i in range(1, 10)),
    }
    if (
        not name
        or len(name) > 512
        or "\\" in name
        or ":" in name
        or relative.is_absolute()
        or any(
            part in {"", ".", ".."}
            or part.endswith((" ", "."))
            or part.split(".")[0].casefold() in reserved
            or any(character in part for character in '<>"|?*')
            for part in name.split("/")
        )
    ):
        raise AppError("backup_invalid", "Недопустимый путь в manifest", 422)
    path = root.joinpath(*relative.parts)
    for candidate in (path, *path.parents):
        if candidate.is_symlink() or candidate.is_junction():
            raise AppError("backup_invalid", "Ссылки в backup запрещены", 422)
    if not path.resolve().is_relative_to(root.resolve()):
        raise AppError("backup_invalid", "Путь вне backup", 422)
    return path


def _revision(connection: sqlite3.Connection) -> str:
    return str(connection.execute("SELECT version_num FROM alembic_version").fetchone()[0])


def _check_artifacts(connection: sqlite3.Connection, root: Path) -> set[str]:
    files: set[str] = set()
    for body, length, content_hash, source_ref in connection.execute(
        "SELECT body_json,byte_length,content_hash,source_ref FROM artifact_manifests"
    ):
        if body is not None:
            encoded = body.encode("utf-8")
            if len(encoded) != length or hashlib.sha256(encoded).hexdigest() != content_hash:
                raise AppError("backup_invalid", "Нарушен hash артефакта", 422)
        elif source_ref:
            if not source_ref.startswith("artifacts/"):
                raise AppError("backup_invalid", "Внешний source_ref не поддержан", 422)
            path = safe_path(root, source_ref)
            if not path.is_file() or path.stat().st_size != length or digest(path) != content_hash:
                raise AppError("backup_invalid", "Артефакт отсутствует или изменён", 422)
            files.add(source_ref)
    return files


def compatibility(settings: Settings) -> dict[str, Any]:
    """Read old schemas without selecting newly introduced Run columns."""
    from sqlalchemy import text

    engine = create_database(settings)
    try:
        with engine.connect() as connection:
            revision = str(
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
            )
            known = {
                p.stem
                for p in (Path(__file__).parents[1] / "persistence/migrations/versions").glob(
                    "*.py"
                )
            }
            if revision not in known:
                raise AppError("schema_unsupported", "Неизвестная версия БД", 409)
            errors: list[str] = []
            for row in connection.execute(
                text("SELECT id,schema_version,required_features_json FROM pipeline_versions")
            ):
                report = ValidationReport()
                check_version_features(
                    row.schema_version, json.loads(row.required_features_json), report
                )
                if not report.ok:
                    errors.append(row.id)
            active = 0
            for row in connection.execute(text("SELECT id,state,snapshot_json FROM runs")):
                snapshot = json.loads(row.snapshot_json)
                report = ValidationReport()
                check_version_features(
                    snapshot.get("schema_version"), snapshot.get("required_features", []), report
                )
                if not report.ok:
                    errors.append(row.id)
                if row.state not in {"completed", "failed", "cancelled"}:
                    active += 1
                    if snapshot.get("engine_version") != __version__:
                        errors.append(row.id)
            if errors:
                raise AppError(
                    "schema_unsupported",
                    "Обновление несовместимо с сохранёнными версиями",
                    409,
                    {"ids": errors[:100]},
                )
            return {
                "database_revision": revision,
                "target_revision": SCHEMA_REVISION,
                "nonterminal_runs": active,
                "compatible": True,
            }
    finally:
        engine.dispose()


def _backup(
    settings: Settings, destination: Path, *, include_secrets: bool = False
) -> dict[str, Any]:
    destination = destination.absolute()
    if (
        destination.exists()
        or destination.resolve().is_relative_to(settings.data_dir.resolve())
        or settings.data_dir.resolve().is_relative_to(destination.resolve())
    ):
        raise AppError("backup_destination_invalid", "Выберите новый каталог вне data_dir", 409)
    private_directory(destination)
    database = safe_path(destination, "db/agents-ide.db")
    private_directory(database.parent)
    files = {"db/agents-ide.db"}
    with (
        closing(sqlite3.connect(settings.database_path)) as source,
        closing(sqlite3.connect(database)) as target,
    ):
        source.backup(target)
        # A self-contained backup must not depend on, or accept, an unmanifested WAL.
        target.execute("PRAGMA journal_mode=DELETE")
        if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise AppError("backup_invalid", "SQLite integrity_check failed", 422)
        external = _check_artifacts(target, settings.data_dir)
        revision = _revision(target)
    for name in sorted(external):
        target_path = safe_path(destination, name)
        private_directory(target_path.parent)
        shutil.copyfile(safe_path(settings.data_dir, name), target_path)
        files.add(name)
    if include_secrets:
        private_directory(destination / "secrets")
        for path in (settings.data_dir / "secrets").glob("*.dpapi"):
            if not re.fullmatch(r"[a-f0-9]{32}\.dpapi", path.name):
                raise AppError("backup_invalid", "Invalid SecretStore filename", 422)
            name = f"secrets/{path.name}"
            shutil.copyfile(safe_path(settings.data_dir, name), safe_path(destination, name))
            files.add(name)
    manifest = {
        "format": FORMAT,
        "application_version": __version__,
        "database_revision": revision,
        "created_at": time.time(),
        "secrets": "windows_dpapi_user_bound" if include_secrets else "excluded",
        "workspaces": "excluded",
        "settings": settings.model_dump(mode="json", exclude={"data_dir", "frontend_dir"}),
        "files": [
            {
                "path": name,
                "bytes": (destination / name).stat().st_size,
                "sha256": digest(destination / name),
            }
            for name in sorted(files)
        ],
    }
    # Written last. An interrupted backup cannot be mistaken for a complete one.
    atomic_write(destination / "manifest.json", json.dumps(manifest, indent=2).encode())
    verify_backup(destination)
    return {
        "backup": str(destination),
        "files": len(files),
        "database_revision": revision,
        "secrets": manifest["secrets"],
    }


def verify_backup(source: Path) -> dict[str, Any]:
    try:
        path = safe_path(source, "manifest.json")
        if path.stat().st_size > 8 * 1024**2:
            raise ValueError("manifest too large")
        manifest: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        if manifest["format"] != FORMAT or manifest["application_version"] != __version__:
            raise AppError("schema_unsupported", "Версия backup не поддержана", 409)
        names: set[str] = set()
        for item in manifest["files"]:
            name = item["path"]
            path = safe_path(source, name)
            if name.casefold() in names or not (
                name == "db/agents-ide.db"
                or name.startswith("artifacts/")
                or re.fullmatch(r"secrets/[a-f0-9]{32}\.dpapi", name)
            ):
                raise ValueError("unexpected or duplicate file")
            names.add(name.casefold())
            if path.stat().st_size != item["bytes"] or digest(path) != item["sha256"]:
                raise ValueError("file hash mismatch")
        if "db/agents-ide.db" not in names:
            raise ValueError("missing database")
        if any(
            (source / f"db/agents-ide.db{suffix}").exists()
            for suffix in ("-wal", "-shm", "-journal")
        ):
            raise ValueError("unmanifested SQLite sidecar")
        with closing(
            sqlite3.connect(f"{(source / 'db/agents-ide.db').as_uri()}?mode=ro", uri=True)
        ) as connection:
            if (
                _revision(connection) != manifest["database_revision"]
                or connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
                or connection.execute("PRAGMA foreign_key_check").fetchone()
            ):
                raise ValueError("invalid database")
            if not {name.casefold() for name in _check_artifacts(connection, source)}.issubset(
                names
            ):
                raise ValueError("missing manifest entry")
        return manifest
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        raise AppError(
            "backup_invalid", "Backup неполон, повреждён или имеет неверный manifest", 422
        ) from None


def backup(
    settings: Settings, destination: Path, *, include_secrets: bool = False
) -> dict[str, Any]:
    with offline(settings, "backup"):
        compatibility(settings)
        return _backup(settings, destination, include_secrets=include_secrets)


def _recover_restored(connection: sqlite3.Connection) -> None:
    connection.execute("DELETE FROM auth_sessions")
    connection.execute("DELETE FROM pairing")
    connection.execute("DELETE FROM worker_heartbeat")
    # Reconcile automatically, then pause. Restoration never authorizes dispatch.
    connection.execute("DELETE FROM queue_jobs")
    for run_id, _state, target in connection.execute(
        "SELECT id,state,resume_target_json FROM runs "
        "WHERE state NOT IN ('completed','failed','cancelled')"
    ).fetchall():
        resume = json.loads(target or "{}")
        resume.update(
            action="reconcile",
            restored_backup=True,
            blockers=list(
                dict.fromkeys(
                    [
                        *resume.get("blockers", []),
                        "owner_expired",
                    ]
                )
            ),
        )
        connection.execute(
            "UPDATE runs SET state='recovering',state_version=state_version+1,worker_id=NULL,"
            "worker_generation=worker_generation+1,resume_target_json=?,updated_at=? WHERE id=?",
            (json.dumps(resume), time.time(), run_id),
        )
        connection.execute(
            "INSERT INTO queue_jobs(id,run_id,available_at,generation,created_at) "
            "VALUES(?,?,?,0,?)",
            (uuid.uuid4().hex, run_id, time.time(), time.time()),
        )
    connection.execute("UPDATE workspace_reservations SET lease_expires_at=NULL")
    connection.execute(
        "UPDATE planning_jobs SET lease_owner=NULL,lease_expires_at=NULL,generation=generation+1"
    )
    connection.execute(
        "UPDATE planning_attempts SET outcome='unknown',error_code='unknown_external_result' "
        "WHERE outcome='running'"
    )
    connection.execute("UPDATE planning_members SET status='unknown' WHERE status='running'")
    connection.execute(
        "UPDATE planning_jobs SET state='failed',state_version=state_version+1,"
        'last_error_json=\'{"code":"unknown_external_result"}\' '
        "WHERE state IN ('drafting','merging')"
    )
    connection.commit()


def restore(settings: Settings, source: Path) -> dict[str, Any]:
    source = source.absolute()
    with offline(settings, "restore"):
        if (
            any((settings.data_dir / "db").iterdir())
            or any((settings.data_dir / "artifacts").iterdir())
            or any((settings.data_dir / "secrets").iterdir())
        ):
            raise AppError(
                "restore_destination_not_empty", "Restore требует новый каталог данных", 409
            )
        manifest = verify_backup(source)
        # Copy and re-verify the complete set in an isolated staging directory;
        # database publication is last, so partial restore cannot be started.
        staging = settings.data_dir / "temp/restore"
        if staging.exists():
            raise AppError(
                "restore_incomplete", "Используйте новый каталог после прерванного restore", 409
            )
        atomic_write(settings.data_dir / "runtime/maintenance-request", b"restore:writing")
        private_directory(staging)
        for item in manifest["files"]:
            target = safe_path(staging, item["path"])
            private_directory(target.parent)
            shutil.copyfile(safe_path(source, item["path"]), target)
        atomic_write(staging / "manifest.json", json.dumps(manifest).encode())
        verify_backup(staging)
        with closing(sqlite3.connect(staging / "db/agents-ide.db")) as connection:
            _recover_restored(connection)
        for item in manifest["files"]:
            if item["path"] == "db/agents-ide.db":
                continue
            target = safe_path(settings.data_dir, item["path"])
            private_directory(target.parent)
            os.replace(staging / item["path"], target)
        os.replace(staging / "db/agents-ide.db", settings.database_path)
        migrate(settings, lock_held=True)
        (settings.data_dir / "runtime/pair-code").unlink(missing_ok=True)
        return {
            "restored": True,
            "runs": "recovering_requires_explicit_reconciliation",
            "secrets": manifest["secrets"],
            "workspaces": "not_restored",
        }


def update(settings: Settings, destination: Path) -> dict[str, Any]:
    with offline(settings, "update"):
        report = compatibility(settings)
        with closing(sqlite3.connect(settings.database_path)) as connection:
            before = dict(connection.execute("SELECT id,snapshot_json FROM runs"))
        saved = _backup(settings, destination, include_secrets=True)
        atomic_write(settings.data_dir / "runtime/maintenance-request", b"update:writing")
        migrate(settings, lock_held=True)
        engine = create_database(settings)
        try:
            with Session(engine) as session:
                install_all(session)
                session.commit()
                if before != {
                    key: value for key, value in session.execute(select(Run.id, Run.snapshot_json))
                }:
                    raise AppError("snapshot_changed", "Обновление изменило snapshot", 500)
            with engine.connect() as connection:
                if connection.exec_driver_sql("PRAGMA integrity_check").scalar() != "ok":
                    raise AppError("database_unavailable", "Проверка обновления не пройдена", 503)
        finally:
            engine.dispose()
        return {**report, **saved, "updated": True}


def compact(settings: Settings) -> dict[str, Any]:
    with offline(settings, "compact"):
        with closing(sqlite3.connect(settings.database_path)) as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.execute("VACUUM")
        return {"compacted": True}
