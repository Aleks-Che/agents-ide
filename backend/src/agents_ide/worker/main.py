"""Independent worker: heartbeat and at most two concurrent leased Runs.

Stage 5 adds:

* a database-unavailable safety check that aborts dispatch when the
  engine cannot be reached; the worker stops accepting new work instead
  of silently losing progress.
* separate heartbeats for worker (process liveness), attempt (last
  dispatch activity) and registered child processes.
* recovery leases for ``recovering`` Runs. Waiting/paused/stopped Runs
  require an explicit command and retain their reservations and blockers.
"""

import logging
import os
import signal
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

import portalocker
from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from agents_ide.config import Settings
from agents_ide.engine.planning_worker import dispatch_planning_job
from agents_ide.engine.queue import claim_next_job, refresh_lease, release_job
from agents_ide.engine.runner import build_runner
from agents_ide.persistence.database import check_database, create_database, migrate
from agents_ide.security.secrets import SecretStore
from agents_ide.worker.processes import ProcessRegistry, ProcessSupervisor

logger = logging.getLogger("agents_ide.worker")


def write_heartbeat(engine: Engine, worker_id: str, started_at: float, status: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO worker_heartbeat VALUES (1,:worker,:pid,:started,:seen,:status) "
                "ON CONFLICT(id) DO UPDATE SET worker_id=:worker, pid=:pid, "
                "started_at=:started, last_seen_at=:seen, status=:status"
            ),
            {
                "worker": worker_id,
                "pid": os.getpid(),
                "started": started_at,
                "seen": time.time(),
                "status": status,
            },
        )


def worker_status(engine: Engine, settings: Settings) -> dict[str, Any]:
    with engine.connect() as connection:
        row = (
            connection.execute(text("SELECT * FROM worker_heartbeat WHERE id=1")).mappings().first()
        )
    if not row:
        return {"status": "unavailable", "last_seen_at": None}
    status = row["status"]
    if status == "running" and time.time() - row["last_seen_at"] > settings.worker_stale_seconds:
        status = "stale"
    return {"status": status, "last_seen_at": row["last_seen_at"]}


def run_worker(settings: Settings) -> None:
    stopping = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stopping.set())
    with portalocker.Lock(str(settings.data_dir / "runtime/worker.lock"), timeout=0):
        from agents_ide.operations.maintenance import ensure_available, requested

        ensure_available(settings)
        migrate(settings)
        engine = create_database(settings)
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        worker_id, started_at = uuid.uuid4().hex, time.time()
        registry = ProcessRegistry()
        logger.info("worker.started", extra={"worker_id": worker_id})
        pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="run")
        planning_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="planning")
        try:
            pending: list[Future[bool]] = []
            planning_pending: list[Future[bool]] = []
            last_heartbeat = 0.0
            last_gc = time.monotonic()
            db_failures = 0
            while not stopping.is_set():
                from agents_ide.launcher import stop_requested

                if stop_requested(settings, os.environ.get("AGENTS_IDE_LAUNCH_ID")):
                    stopping.set()
                    break
                if time.monotonic() - last_heartbeat >= settings.heartbeat_seconds:
                    try:
                        if check_database(engine):
                            write_heartbeat(engine, worker_id, started_at, "running")
                            db_failures = 0
                        else:
                            db_failures += 1
                            write_heartbeat(engine, worker_id, started_at, "stale")
                    except (OperationalError, SQLAlchemyError):
                        db_failures += 1
                        try:
                            write_heartbeat(engine, worker_id, started_at, "unavailable")
                        except Exception:
                            logger.warning("worker.heartbeat_unavailable")
                    last_heartbeat = time.monotonic()
                    if db_failures:
                        registry.abort_all()
                    if db_failures >= 3:
                        logger.error(
                            "worker.database_unavailable",
                            extra={"worker_id": worker_id, "failures": db_failures},
                        )
                        stopping.set()
                        break
                for future in pending[:]:
                    if future.done():
                        pending.remove(future)
                        try:
                            future.result()
                        except Exception:
                            logger.exception("worker.dispatch_error")
                for future in planning_pending[:]:
                    if future.done():
                        planning_pending.remove(future)
                        try:
                            future.result()
                        except Exception:
                            logger.exception("worker.planning_dispatch_error")
                if (
                    not pending
                    and not planning_pending
                    and not requested(settings)
                    and not db_failures
                    and time.monotonic() - last_gc >= 60
                ):
                    from agents_ide.operations.storage import collect_garbage

                    try:
                        collect_garbage(factory)
                    except Exception:
                        logger.warning("worker.retention_failed")
                    last_gc = time.monotonic()
                while (
                    len(pending) < 2
                    and db_failures == 0
                    and not stopping.is_set()
                    and not requested(settings)
                ):
                    pending.append(
                        pool.submit(
                            dispatch_once,
                            settings,
                            worker_id,
                            factory,
                            stopping,
                            registry,
                        )
                    )
                while (
                    len(planning_pending) < 2
                    and db_failures == 0
                    and not stopping.is_set()
                    and not requested(settings)
                ):
                    planning_pending.append(
                        planning_pool.submit(
                            dispatch_planning_once,
                            settings,
                            worker_id,
                            factory,
                            stopping,
                        )
                    )
                stopping.wait(min(0.25, settings.heartbeat_seconds))
        finally:
            stopping.set()
            registry.abort_all()
            pool.shutdown(wait=True, cancel_futures=True)
            planning_pool.shutdown(wait=True, cancel_futures=True)
            for entry in registry.entries():
                if entry.group:
                    entry.group.close()
            try:
                write_heartbeat(engine, worker_id, started_at, "stopped")
            except Exception:
                logger.warning("worker.shutdown_heartbeat_failed")
            finally:
                engine.dispose()
            logger.info("worker.stopped", extra={"worker_id": worker_id})


def dispatch_planning_once(
    settings: Settings,
    worker_id: str,
    session_factory: sessionmaker[Session] | None = None,
    stopping: threading.Event | None = None,
) -> bool:
    from agents_ide.engine.planning_worker import claim_planning_job
    from agents_ide.operations.maintenance import requested
    from agents_ide.security.secrets import SecretStore

    if requested(settings):
        return False
    owned_engine = create_database(settings) if session_factory is None else None
    factory = session_factory or sessionmaker(bind=owned_engine, expire_on_commit=False)
    try:
        claim = claim_planning_job(factory, worker_id)
        if claim is None:
            return False
        dispatch_planning_job(
            factory,
            claim.job_id,
            claim=claim,
            abort=stopping,
            secret_store=SecretStore(settings.data_dir / "secrets"),
        )
        return True
    finally:
        if owned_engine is not None:
            owned_engine.dispose()


def dispatch_once(
    settings: Settings,
    worker_id: str,
    session_factory: sessionmaker[Session] | None = None,
    stopping: threading.Event | None = None,
    registry: ProcessRegistry | None = None,
) -> bool:
    from agents_ide.operations.maintenance import requested

    if requested(settings):
        return False
    owned_engine = create_database(settings) if session_factory is None else None
    factory = session_factory or sessionmaker(bind=owned_engine, expire_on_commit=False)
    try:
        job = claim_next_job(factory, worker_id=worker_id, lease_seconds=30)
        if job is None:
            return False
        abort = threading.Event()
        if registry is not None:
            with registry.lock:
                registry.aborts[job.run_id] = abort
        stop_renewal = threading.Event()
        runner = build_runner(
            session_factory=factory,
            worker_id=worker_id,
            generation=job.generation,
            data_dir=settings.data_dir,
            secret_store=SecretStore(settings.data_dir / "secrets"),
            abort=abort,
            registry=registry or ProcessRegistry(),
        )

        def renew() -> None:
            while not stop_renewal.wait(min(settings.heartbeat_seconds, 1)):
                if stopping is not None and stopping.is_set():
                    abort.set()
                    if runner.registry:
                        ProcessSupervisor(
                            factory, runner.registry, job.run_id, worker_id, job.generation
                        ).stop(cooperative_seconds=0)
                    return
                try:
                    refresh_lease(
                        factory,
                        job_id=job.job_id,
                        worker_id=worker_id,
                        expected_generation=job.generation,
                        lease_seconds=30,
                    )
                except Exception:
                    abort.set()
                    if runner.registry:
                        ProcessSupervisor(
                            factory, runner.registry, job.run_id, worker_id, job.generation
                        ).stop(cooperative_seconds=0)
                    logger.warning("worker.lease_renew_failed", extra={"run_id": job.run_id})
                    return

        thread = threading.Thread(target=renew, daemon=True)
        thread.start()
        try:
            if stopping is not None and stopping.is_set():
                release_job(
                    factory,
                    job_id=job.job_id,
                    worker_id=worker_id,
                    expected_generation=job.generation,
                )
                return False
            runner.execute(job.run_id)
        except BaseException:
            # A failure after the adapter returned (for example while saving its
            # result) must stop owned processes too, even if lease refresh succeeds.
            abort.set()
            if runner.registry:
                ProcessSupervisor(
                    factory, runner.registry, job.run_id, worker_id, job.generation
                ).stop(cooperative_seconds=0)
            raise
        finally:
            stop_renewal.set()
            thread.join(timeout=2)
            if registry is not None:
                with registry.lock:
                    registry.aborts.pop(job.run_id, None)
        # Completed and waiting Runs are consumed atomically by Runner. An exception
        # leaves the intent and reservation for recovery; it never requeues the call.
        return True
    finally:
        if owned_engine is not None:
            owned_engine.dispose()
