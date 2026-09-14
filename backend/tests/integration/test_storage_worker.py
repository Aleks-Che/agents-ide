import os
import subprocess
import sys
import time

from sqlalchemy import text

from agents_ide.persistence.database import migrate
from agents_ide.worker.main import write_heartbeat


def test_migration_repeatable_and_sqlite_contract(client, settings):
    migrate(settings)
    with client.app.state.engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar() == 5000


def test_health_separates_worker_readiness(authenticated, settings):
    client, _ = authenticated
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/readiness").status_code == 503
    engine = client.app.state.engine
    write_heartbeat(engine, "test", time.time(), "running")
    assert client.get("/api/readiness").status_code == 200
    with engine.begin() as connection:
        connection.execute(text("UPDATE worker_heartbeat SET last_seen_at=0"))
    assert client.get("/api/system/status").json()["worker"]["status"] == "stale"
    assert client.get("/api/readiness").json()["code"] == "service_unavailable"


def test_real_worker_survives_no_browser_and_rejects_second_owner(client, settings):
    env = {
        **os.environ,
        "AGENTS_IDE_DATA_DIR": str(settings.data_dir),
        "AGENTS_IDE_HEARTBEAT_SECONDS": "0.1",
    }
    argv = [sys.executable, "-m", "agents_ide", "worker"]
    worker = subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 10
        first = None
        while time.monotonic() < deadline:
            with client.app.state.engine.connect() as connection:
                first = connection.execute(
                    text("SELECT last_seen_at FROM worker_heartbeat")
                ).scalar()
            if first:
                break
            time.sleep(0.05)
        assert first is not None
        time.sleep(0.3)
        with client.app.state.engine.connect() as connection:
            second = connection.execute(text("SELECT last_seen_at FROM worker_heartbeat")).scalar()
        assert second > first
        duplicate = subprocess.run(argv, env=env, capture_output=True, timeout=10)
        assert duplicate.returncode != 0
        assert b"service_busy" in duplicate.stderr
    finally:
        worker.terminate()
        worker.communicate(timeout=10)
