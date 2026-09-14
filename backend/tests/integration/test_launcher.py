import json
import socket
import subprocess
import sys
import time

import httpx
import pytest

from agents_ide import launcher
from agents_ide.config import Settings
from agents_ide.errors import AppError
from agents_ide.security.filesystem import prepare_data_dir
from agents_ide.worker.processes import wait_stopped


def available_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def test_busy_port_is_not_killed(settings):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        settings.port = listener.getsockname()[1]
        with pytest.raises(AppError) as error:
            launcher.check_port(settings)
        assert error.value.code == "port_unavailable"
        assert listener.getsockname()[1] == settings.port


def test_detached_launcher_sse_revoke_and_stop(tmp_path):
    settings = Settings(data_dir=tmp_path / "services", port=available_port())
    prepare_data_dir(settings.data_dir)
    command = [
        sys.executable,
        "-m",
        "agents_ide",
        "--data-dir",
        str(settings.data_dir),
        "--port",
        str(settings.port),
    ]
    processes = []
    try:
        result = subprocess.run([*command, "start"], capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        started = json.loads(result.stdout)
        assert started["status"] == "running"
        processes = [launcher.resolve_process(record) for record in started["children"].values()]
        assert all(processes)
        old_api = launcher.resolve_process(started["children"]["api"])
        old_api.kill()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            current = launcher.status(settings)
            if (
                current.get("status") == "running"
                and current["children"]["api"]["pid"] != old_api.pid
            ):
                break
            time.sleep(0.1)
        assert current["children"]["api"]["pid"] != old_api.pid
        assert current["children"]["worker"] == started["children"]["worker"]
        processes.append(launcher.resolve_process(current["children"]["api"]))
        # The invoking console process has exited. Services still respond.
        with httpx.Client(base_url=settings.origin, timeout=5, trust_env=False) as client:
            assert client.get("/api/health").status_code == 200
            code = (settings.data_dir / "runtime/pair-code").read_text()
            paired = client.post(
                "/api/auth/pair", json={"code": code}, headers={"Origin": settings.origin}
            )
            assert paired.status_code == 200
            headers = {"Origin": settings.origin, "X-CSRF-Token": paired.json()["csrf_token"]}
            assert client.get("/api/readiness").status_code == 200
            with client.stream("GET", "/api/system/events") as stream:
                lines = stream.iter_lines()
                assert next(lines) == "event: system.status"
                assert next(lines).startswith("data: ")
                assert client.post("/api/auth/logout", headers=headers).status_code == 204
                assert "event: auth.expired" in list(lines)
        again = subprocess.run([*command, "start"], capture_output=True, text=True, timeout=15)
        assert json.loads(again.stdout)["launch_id"] == started["launch_id"]
    finally:
        result = subprocess.run([*command, "stop"], capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
    assert wait_stopped([process for process in processes if process], 5)
    assert launcher.status(settings)["status"] == "stopped"
