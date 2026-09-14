"""Independent worker: heartbeat and at most two concurrent leased Runs."""

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
from sqlalchemy.orm import Session, sessionmaker

from agents_ide.config import Settings
from agents_ide.engine.queue import claim_next_job, refresh_lease, release_job
from agents_ide.engine.runner import build_runner
from agents_ide.persistence.database import create_database, migrate
from agents_ide.security.secrets import SecretStore

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
        migrate(settings)
        engine = create_database(settings)
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        worker_id, started_at = uuid.uuid4().hex, time.time()
        logger.info("worker.started", extra={"worker_id": worker_id})
        try:
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="run") as pool:
                pending: list[Future[bool]] = []
                last_heartbeat = 0.0
                while not stopping.is_set():
                    from agents_ide.launcher import stop_requested

                    if stop_requested(settings, os.environ.get("AGENTS_IDE_LAUNCH_ID")):
                        stopping.set()
                        break
                    if time.monotonic() - last_heartbeat >= settings.heartbeat_seconds:
                        write_heartbeat(engine, worker_id, started_at, "running")
                        last_heartbeat = time.monotonic()
                    for future in pending[:]:
                        if future.done():
                            pending.remove(future)
                            try:
                                future.result()
                            except Exception:
                                logger.exception("worker.dispatch_error")
                    while len(pending) < 2:
                        pending.append(
                            pool.submit(dispatch_once, settings, worker_id, factory, stopping)
                        )
                    stopping.wait(min(0.25, settings.heartbeat_seconds))
        finally:
            stopping.set()
            try:
                write_heartbeat(engine, worker_id, started_at, "stopped")
            finally:
                engine.dispose()
            logger.info("worker.stopped", extra={"worker_id": worker_id})


def dispatch_once(
    settings: Settings,
    worker_id: str,
    session_factory: sessionmaker[Session] | None = None,
    stopping: threading.Event | None = None,
) -> bool:
    owned_engine = create_database(settings) if session_factory is None else None
    factory = session_factory or sessionmaker(bind=owned_engine, expire_on_commit=False)
    try:
        job = claim_next_job(factory, worker_id=worker_id, lease_seconds=30)
        if job is None:
            return False
        abort = threading.Event()
        stop_renewal = threading.Event()
        runner = build_runner(
            session_factory=factory,
            worker_id=worker_id,
            generation=job.generation,
            data_dir=settings.data_dir,
            secret_store=SecretStore(settings.data_dir / "secrets"),
            abort=abort,
        )

        def renew() -> None:
            while not stop_renewal.wait(min(settings.heartbeat_seconds, 1)):
                if stopping is not None and stopping.is_set():
                    abort.set()
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
        finally:
            stop_renewal.set()
            thread.join(timeout=2)
        # Completed and waiting Runs are consumed atomically by Runner. An exception
        # leaves the intent and reservation for recovery; it never requeues the call.
        return True
    finally:
        if owned_engine is not None:
            owned_engine.dispose()
