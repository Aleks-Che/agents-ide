"""Exclusive offline operations with a durable admission gate and boundary drain."""

from __future__ import annotations

import contextlib
import time
from collections.abc import Iterator

import portalocker
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from agents_ide import launcher
from agents_ide.config import Settings
from agents_ide.engine.ownership import owner_may_be_alive
from agents_ide.errors import AppError
from agents_ide.persistence.database import create_database
from agents_ide.persistence.models import PlanningJob, QueueJob
from agents_ide.security.filesystem import atomic_write


def requested(settings: Settings) -> bool:
    return (settings.data_dir / "runtime/maintenance-request").exists()


def ensure_available(settings: Settings) -> None:
    if requested(settings):
        raise AppError(
            "maintenance_in_progress", "Обслуживание: новые действия приостановлены", 503
        )


def _busy(settings: Settings) -> bool:
    if not settings.database_path.exists():
        return False
    engine = create_database(settings)
    try:
        with Session(engine) as session:
            for row in session.scalars(select(QueueJob).where(QueueJob.claimed_by.isnot(None))):
                if (row.lease_expires_at or 0) > time.time() or owner_may_be_alive(
                    row.owner_pid, row.owner_create_time
                ):
                    return True
            # offline() also runs before upgrading an existing database. Old
            # Council rows have no process identity, so never assume their owner died.
            columns = {column["name"] for column in inspect(engine).get_columns("planning_jobs")}
            if "owner_pid" not in columns:
                return (
                    session.scalar(
                        select(PlanningJob.id).where(PlanningJob.lease_owner.isnot(None)).limit(1)
                    )
                    is not None
                )
            for job in session.scalars(
                select(PlanningJob).where(PlanningJob.lease_owner.isnot(None))
            ):
                if (job.lease_expires_at or 0) > time.time() or owner_may_be_alive(
                    job.owner_pid, job.owner_create_time
                ):
                    return True
            return False
    finally:
        engine.dispose()


@contextlib.contextmanager
def offline(settings: Settings, operation: str, *, timeout: float = 60) -> Iterator[None]:
    runtime = settings.data_dir / "runtime"
    with portalocker.Lock(str(runtime / "operations.lock"), timeout=0):
        ensure_available(settings)
        managed = (
            launcher.resolve_process(launcher.status(settings).get("launcher", {})) is not None
        )
        restart = False
        succeeded = False
        atomic_write(runtime / "maintenance-request", operation.encode())
        try:
            if managed:
                deadline = time.monotonic() + timeout
                while _busy(settings):
                    if time.monotonic() >= deadline:
                        raise AppError(
                            "maintenance_busy", "Операции не достигли безопасной границы", 409
                        )
                    time.sleep(0.1)
                launcher.stop(settings)
                restart = True
            # Manual services and concurrent local writers must relinquish their
            # locks. Never copy a DB just because a heartbeat appears stale.
            with contextlib.ExitStack() as stack:
                for name in ("start", "launcher", "api", "worker", "migrate"):
                    stack.enter_context(portalocker.Lock(str(runtime / f"{name}.lock"), timeout=0))
                yield
                succeeded = True
        finally:
            retain_gate = not succeeded and (runtime / "maintenance-request").read_text().endswith(
                ":writing"
            )
            if not retain_gate:
                (runtime / "maintenance-request").unlink(missing_ok=True)
            if restart and not retain_gate:
                launcher.start(settings)


def clear_interrupted(settings: Settings) -> None:
    """Explicit recovery of a gate left by a crashed maintenance process."""
    with contextlib.ExitStack() as stack:
        for name in ("operations", "start", "launcher", "api", "worker", "migrate"):
            stack.enter_context(
                portalocker.Lock(
                    str(settings.data_dir / f"runtime/{name}.lock"),
                    timeout=0,
                )
            )
        (settings.data_dir / "runtime/maintenance-request").unlink(missing_ok=True)
