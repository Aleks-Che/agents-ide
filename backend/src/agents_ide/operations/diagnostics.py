"""Metadata-only diagnostics; never export prompts, source, argv or credentials."""

from __future__ import annotations

import shutil
import time
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from agents_ide import __version__, launcher
from agents_ide.config import Settings
from agents_ide.errors import AppError
from agents_ide.operations.maintenance import requested
from agents_ide.operations.storage import disk_usage
from agents_ide.persistence.database import SCHEMA_REVISION, check_database, create_database
from agents_ide.persistence.models import (
    AgentSession,
    HarnessProfile,
    ProviderConnection,
    QueueJob,
    Run,
    StepAttempt,
)
from agents_ide.security.secrets import SecretStore
from agents_ide.worker.main import worker_status


def diagnostics(settings: Settings) -> dict[str, Any]:
    report: dict[str, Any] = {
        "version": __version__,
        "expected_schema": SCHEMA_REVISION,
        "maintenance": requested(settings),
        "launcher": launcher.status(settings).get("status"),
        "frontend": "ready" if (settings.frontend_dir / "index.html").is_file() else "missing",
        "checks": {"git": "available" if shutil.which("git") else "missing"},
        "commands": [
            "agents-ide status",
            "agents-ide auth pair-code --rotate",
            "agents-ide diagnostics",
            "agents-ide runs list",
        ],
    }
    try:
        launcher.check_port(settings)
        report["port"] = "free"
    except AppError:
        report["port"] = "in_use"
    try:
        report["disk"] = {
            **disk_usage(settings),
            "budget_bytes": settings.data_budget_bytes,
            "reserve_bytes": settings.disk_reserve_bytes,
        }
    except (OSError, AppError):
        report["disk"] = {"status": "unavailable"}
    if not settings.database_path.exists():
        report["database"] = "missing"
        return report
    engine = create_database(settings)
    try:
        report["database"] = "ready" if check_database(engine) else "schema_mismatch"
        report["worker"] = worker_status(engine, settings)
        with Session(engine) as session:
            report["runs"] = {
                state: count
                for state, count in session.execute(
                    select(Run.state, func.count()).group_by(Run.state)
                )
            }
            report["leases"] = [
                {
                    "run_id": row.run_id,
                    "generation": row.generation,
                    "expires_at": row.lease_expires_at,
                    "expired": (row.lease_expires_at or 0) <= time.time(),
                }
                for row in session.scalars(
                    select(QueueJob).where(QueueJob.claimed_by.isnot(None)).limit(50)
                )
            ]
            report["attempts"] = [
                {
                    "attempt_id": row.id,
                    "status": row.status,
                    "heartbeat_at": row.heartbeat_at,
                    "error_code": row.error_code,
                }
                for row in session.scalars(
                    select(StepAttempt)
                    .where(StepAttempt.status.in_(["running", "unknown", "prepared"]))
                    .limit(50)
                )
            ]
            report["last_external_event"] = session.scalar(
                select(func.max(AgentSession.last_external_event_at))
            )
            store = SecretStore(settings.data_dir / "secrets")
            connections = []
            for connection in session.scalars(select(ProviderConnection)):
                access = "not_configured"
                if connection.secret_reference:
                    try:
                        store.get(connection.secret_reference)
                        access = "available"
                    except AppError:
                        access = "secret_unavailable"
                connections.append(
                    {
                        "id": connection.id,
                        "access": access,
                        "catalog_fetched_at": connection.catalog_fetched_at,
                        "archived": connection.archived_at is not None,
                    }
                )
            report["connections"] = connections
            report["harnesses"] = [
                {
                    "id": row.id,
                    "kind": row.harness_kind,
                    "catalog_fetched_at": row.catalog_fetched_at,
                    "authorization": "use_explicit_connection_test",
                }
                for row in session.scalars(select(HarnessProfile))
            ]
    except (SQLAlchemyError, ValueError):
        report["database"] = "unavailable"
    finally:
        engine.dispose()
    return report
