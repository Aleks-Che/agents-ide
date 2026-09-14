import logging
import os
import signal
import threading
import time
import uuid
from typing import Any

import portalocker
from sqlalchemy import Engine, text

from agents_ide.config import Settings
from agents_ide.persistence.database import create_database, migrate


def write_heartbeat(engine: Engine, worker_id: str, started_at: float, status: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO worker_heartbeat VALUES (1,:worker,:pid,:started,:seen,:status) "
                "ON CONFLICT(id) DO UPDATE SET worker_id=:worker, pid=:pid, started_at=:started, "
                "last_seen_at=:seen, status=:status"
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
        worker_id = uuid.uuid4().hex
        started_at = time.time()
        logging.info("worker.started", extra={"worker_id": worker_id})
        try:
            while not stopping.is_set():
                from agents_ide.launcher import stop_requested

                if stop_requested(settings, os.environ.get("AGENTS_IDE_LAUNCH_ID")):
                    break
                write_heartbeat(engine, worker_id, started_at, "running")
                stopping.wait(settings.heartbeat_seconds)
        finally:
            try:
                write_heartbeat(engine, worker_id, started_at, "stopped")
            finally:
                engine.dispose()
            logging.info("worker.stopped", extra={"worker_id": worker_id})
