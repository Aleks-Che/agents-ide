"""Real detached launcher, built static frontend, and managed backup/restart."""

import json

import httpx
from test_launcher import available_port

from agents_ide import launcher
from agents_ide.config import Settings
from agents_ide.operations.backup import backup, verify_backup
from agents_ide.security.filesystem import prepare_data_dir


def test_managed_backup_restarts_services_and_serves_static_ui(tmp_path):
    from pathlib import Path

    frontend = Path(__file__).resolve().parents[3] / "frontend/dist"
    if not (frontend / "index.html").is_file():
        # Backend-only CI does not build frontend; still test static serving with
        # a local file. The release/frontend job uses the actual built UI.
        frontend = tmp_path / "ui"
        frontend.mkdir()
        (frontend / "index.html").write_text("<!doctype html><title>Agents IDE</title>")
    settings = Settings(
        data_dir=tmp_path / "services", port=available_port(), frontend_dir=frontend
    )
    prepare_data_dir(settings.data_dir)
    try:
        first = launcher.start(settings)
        assert first["status"] == "running"
        with httpx.Client(base_url=settings.origin, trust_env=False) as client:
            response = client.get("/")
            assert response.status_code == 200 and "<!doctype html>" in response.text.lower()
        result = backup(settings, tmp_path / "snapshot")
        assert result["files"] >= 1
        assert verify_backup(tmp_path / "snapshot")["format"] == 1
        second = launcher.status(settings)
        assert second["status"] == "running" and second["launch_id"] != first["launch_id"]
        # A full stop/start models process loss independently of an API restart.
        # Actual Windows reboot remains a separate manual release gate.
        launcher.stop(settings)
        third = launcher.start(settings)
        assert third["status"] == "running"
        assert third["launch_id"] != second["launch_id"]
        assert (
            json.loads((settings.data_dir / "runtime/launcher.json").read_text())["status"]
            == "running"
        )
    finally:
        launcher.stop(settings)
