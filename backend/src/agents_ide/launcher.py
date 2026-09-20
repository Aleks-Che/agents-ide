import json
import logging
import os
import socket
import subprocess
import sys
import time
import uuid
from typing import Any

import httpx
import portalocker
import psutil
from sqlalchemy import text

from agents_ide.config import Settings
from agents_ide.errors import AppError
from agents_ide.persistence.database import create_database, migrate
from agents_ide.security.filesystem import atomic_write
from agents_ide.worker.processes import ProcessGroup, is_running, wait_stopped


def stop_requested(settings: Settings, launch_id: str | None) -> bool:
    path = settings.data_dir / "runtime/stop-request"
    return bool(launch_id and path.exists() and path.read_text(encoding="utf-8") == launch_id)


def process_record(process: psutil.Process) -> dict[str, Any]:
    return {"pid": process.pid, "created_at": process.create_time(), "executable": process.exe()}


def resolve_process(record: dict[str, Any]) -> psutil.Process | None:
    try:
        process = psutil.Process(record["pid"])
        if (
            process.create_time() == record["created_at"]
            and process.exe() == record["executable"]
            and is_running(process)
        ):
            return process
    except (psutil.Error, KeyError, TypeError):
        pass
    return None


def status(settings: Settings) -> dict[str, Any]:
    path = settings.data_dir / "runtime/launcher.json"
    if not path.exists():
        return {"status": "stopped"}
    try:
        state: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        if resolve_process(state.get("launcher", {})) is None:
            if state.get("status") not in {"stopped", "failed"}:
                state["status"] = "stale"
        elif state.get("status") == "running" and any(
            resolve_process(child) is None for child in state.get("children", {}).values()
        ):
            state["status"] = "recovering"
        return state
    except (OSError, ValueError):
        return {"status": "unknown"}


def check_port(settings: Settings) -> None:
    try:
        with socket.socket() as listener:
            if sys.platform == "win32":
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            listener.bind((settings.host, settings.port))
    except OSError:
        raise AppError(
            "port_unavailable", f"Порт {settings.port} занят; настройте другой", 503
        ) from None


def start(settings: Settings) -> dict[str, Any]:
    from agents_ide.operations.maintenance import ensure_available

    with portalocker.Lock(str(settings.data_dir / "runtime/start.lock"), timeout=5):
        ensure_available(settings)
        current = status(settings)
        if resolve_process(current.get("launcher", {})):
            return current
        check_port(settings)
        env = child_environment(settings)
        launch_id = uuid.uuid4().hex
        env["AGENTS_IDE_LAUNCH_ID"] = launch_id
        argv = [sys.executable, "-m", "agents_ide", "_launcher"]
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        # Deliberately detached: no context manager that waits for service shutdown.
        child = subprocess.Popen(
            argv,
            cwd=settings.data_dir,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            start_new_session=sys.platform != "win32",
        )
        while True:
            current = status(settings)
            if current.get("launch_id") == launch_id and current.get("status") == "running":
                return current
            if child.poll() is not None:
                break
            time.sleep(0.1)
        current = status(settings)
        if current.get("launch_id") == launch_id:
            atomic_write(settings.data_dir / "runtime/stop-request", current["launch_id"].encode())
            process = resolve_process(current.get("launcher", {}))
            if process:
                wait_stopped([process], 15)
        logging.error("launcher.startup_failed", extra={"status": current.get("status")})
        raise AppError(
            "startup_failed",
            f"Службы не запустились. Журнал: {settings.data_dir / 'logs'}. "
            "Просмотр: agents-ide logs",
            503,
        )


def child_environment(settings: Settings) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "AGENTS_IDE_DATA_DIR": str(settings.data_dir),
            "AGENTS_IDE_PORT": str(settings.port),
            "AGENTS_IDE_HOST": settings.host,
            "AGENTS_IDE_FRONTEND_DIR": str(settings.frontend_dir),
        }
    )
    if settings.dev_origin:
        env["AGENTS_IDE_DEV_ORIGIN"] = settings.dev_origin
    return env


def stop(settings: Settings) -> dict[str, Any]:
    with portalocker.Lock(str(settings.data_dir / "runtime/start.lock"), timeout=5):
        state = status(settings)
        process = resolve_process(state.get("launcher", {}))
        if process is None:
            return state
        atomic_write(settings.data_dir / "runtime/stop-request", state["launch_id"].encode())
        if not wait_stopped([process], 20):
            raise AppError("process_not_responding", "Launcher не подтвердил остановку", 503)
        return status(settings)


def run_launcher(settings: Settings) -> None:
    with (
        portalocker.Lock(str(settings.data_dir / "runtime/launcher.lock"), timeout=0),
        httpx.Client(timeout=0.5, trust_env=False) as health_client,
    ):
        migrate(settings)
        engine = create_database(settings)
        groups: dict[str, ProcessGroup] = {}
        launch_id = os.environ.get("AGENTS_IDE_LAUNCH_ID") or uuid.uuid4().hex
        env = child_environment(settings)
        env["AGENTS_IDE_LAUNCH_ID"] = launch_id
        state: dict[str, Any] = {
            "status": "starting",
            "launch_id": launch_id,
            "launcher": process_record(psutil.Process()),
            "children": {},
        }
        registry = settings.data_dir / "runtime/launcher.json"
        children: dict[str, psutil.Process] = {}
        retries = {"api": 0, "worker": 0}
        failed = False
        try:
            saved_state = json.dumps(state).encode()
            atomic_write(registry, saved_state)
            while not stop_requested(settings, launch_id):
                for role in ("api", "worker"):
                    if role in children and is_running(children[role]):
                        continue
                    if role in children:
                        # A venv wrapper can die before its Python child. Close the
                        # old role's Job before restarting it; other roles stay alive.
                        exit_code = groups[role].exit_code()
                        groups[role].close()
                        retries[role] += 1
                        logging.warning(
                            "launcher.child_exited",
                            extra={
                                "service": role,
                                "pid": children[role].pid,
                                "retries": retries[role],
                                "exit_code": exit_code,
                                "exit_code_hex": f"0x{exit_code & 0xFFFFFFFF:08x}"
                                if exit_code is not None
                                else None,
                            },
                        )
                        if retries[role] > 3:
                            raise AppError(
                                "startup_failed", f"{role}: исчерпан лимит перезапусков", 503
                            )
                        time.sleep(2 ** (retries[role] - 1))
                    groups[role] = ProcessGroup()
                    children[role] = groups[role].start(
                        [sys.executable, "-m", "agents_ide", role], settings.data_dir, env
                    )
                    state["children"][role] = process_record(children[role])
                ready = False
                try:
                    with engine.connect() as connection:
                        heartbeat = connection.execute(
                            text("SELECT last_seen_at FROM worker_heartbeat WHERE status='running'")
                        ).scalar()
                    response = health_client.get(settings.origin + "/api/health")
                    ready = bool(
                        response.status_code == 200
                        and heartbeat
                        and heartbeat >= children["worker"].create_time()
                    )
                except Exception:
                    pass
                state["status"] = "running" if ready else "starting"
                encoded_state = json.dumps(state).encode()
                if encoded_state != saved_state:
                    atomic_write(registry, encoded_state)
                    saved_state = encoded_state
                time.sleep(0.25)
        except Exception as error:
            failed = True
            state["error_code"] = error.code if isinstance(error, AppError) else "internal_error"
            logging.exception("launcher.failed")
        finally:
            atomic_write(settings.data_dir / "runtime/stop-request", launch_id.encode())
            wait_stopped(list(children.values()), 10)
            for group in groups.values():
                group.close()
            engine.dispose()
            state["status"] = "failed" if failed else "stopped"
            atomic_write(registry, json.dumps(state).encode())
