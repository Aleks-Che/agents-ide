import json
import logging
import subprocess
import sys
from datetime import UTC, datetime

from agents_ide.logging import JsonFormatter
from agents_ide.persistence.database import SCHEMA_REVISION, check_database
from agents_ide.services.application_logs import (
    MAX_FILE_BYTES,
    current_launch_started_at,
    read_application_logs,
)


def test_native_fault_output_is_persisted_without_a_console(tmp_path):
    script = (
        "import faulthandler\n"
        "from pathlib import Path\n"
        "from agents_ide import logging as logs\n"
        f"logs.configure_logging(Path({str(tmp_path)!r}), 'worker', 'INFO')\n"
        "assert faulthandler.is_enabled()\n"
        "faulthandler.dump_traceback(file=logs._fault_file)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    assert 'File "<string>"' in (tmp_path / "worker-fault.log").read_text(encoding="utf-8")


def test_crash_exit_code_is_available_in_application_logs(tmp_path):
    record = logging.LogRecord(
        "launcher", logging.WARNING, "", 0, "launcher.child_exited", (), None
    )
    record.exit_code = 0xC0000005
    record.exit_code_hex = "0xc0000005"
    (tmp_path / "launcher.jsonl").write_text(JsonFormatter().format(record), encoding="utf-8")
    report = read_application_logs(tmp_path)
    assert report.entries[0].details["exit_code"] == 0xC0000005
    assert report.entries[0].details["exit_code_hex"] == "0xc0000005"


def test_migrated_database_is_ready_and_mismatch_is_explained(client, caplog):
    engine = client.app.state.engine
    assert check_database(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("UPDATE alembic_version SET version_num='outdated'")
    with caplog.at_level(logging.ERROR):
        assert not check_database(engine)
    record = next(record for record in caplog.records if record.msg == "database.schema_mismatch")
    details = json.loads(JsonFormatter().format(record))
    assert details["expected_schema"] == SCHEMA_REVISION
    assert details["actual_schema"] == ["outdated"]
    with engine.begin() as connection:
        connection.exec_driver_sql("UPDATE alembic_version SET version_num=?", (SCHEMA_REVISION,))


def test_log_api_requires_session_and_validates_filters(client):
    assert client.get("/api/system/logs").status_code == 401


def test_log_api_reads_rotations_filters_and_redacts(authenticated, settings):
    client, _ = authenticated
    directory = settings.data_dir / "logs"
    rows = [
        {"at": "2026-09-17T04:00:00+00:00", "level": "INFO", "message": "worker.started"},
        {
            "at": "2026-09-17T04:01:00+00:00",
            "level": "ERROR",
            "message": "worker.failed",
            "failures": 3,
            "prompt": "must not be returned",
        },
    ]
    (directory / "worker.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + '\n{"incomplete":', encoding="utf-8"
    )
    (directory / "worker.jsonl.1").write_text(
        json.dumps(
            {
                "at": "2026-09-17T03:00:00+00:00",
                "level": "WARNING",
                "message": "token=hidden",
            }
        ),
        encoding="utf-8",
    )
    response = client.get("/api/system/logs")
    assert response.status_code == 200
    report = response.json()
    assert report["directory"] == str(directory)
    assert [entry["level"] for entry in report["entries"]] == ["ERROR", "WARNING"]
    assert report["entries"][0]["details"] == {"failures": 3}
    assert "hidden" not in response.text and "must not be returned" not in response.text
    assert len(client.get("/api/system/logs?role=worker&level=INFO").json()["entries"]) == 3
    assert client.get("/api/system/logs?role=api").json()["entries"] == []
    assert client.get("/api/system/logs?limit=1").json()["truncated"]
    for query in ("role=../secrets", "limit=0", "limit=501", "level=invalid", "scope=invalid"):
        assert client.get(f"/api/system/logs?{query}").status_code == 422


def test_current_launch_excludes_history_but_keeps_new_errors(authenticated, settings, monkeypatch):
    client, _ = authenticated
    monkeypatch.delenv("AGENTS_IDE_LAUNCH_ID", raising=False)
    boundary = datetime(2026, 9, 17, 4, 40, tzinfo=UTC)
    client.app.state.started_at = boundary
    rows = [
        {"at": "2026-09-17T09:26:00+05:00", "level": "ERROR", "message": "old.failure"},
        {"at": "2026-09-17T04:40:00Z", "level": "ERROR", "message": "current.failure"},
        {"at": "2026-09-17T09:41:00+05:00", "level": "ERROR", "message": "latest.failure"},
    ]
    (settings.data_dir / "logs/worker.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows), encoding="utf-8"
    )
    current = client.get("/api/system/logs?scope=current").json()
    assert current["scope"] == "current"
    assert datetime.fromisoformat(current["current_started_at"]) == boundary
    assert {row["message"] for row in current["entries"]} == {
        "current.failure",
        "latest.failure",
    }
    history = client.get("/api/system/logs?scope=all").json()
    assert len(history["entries"]) == 3 and history["scope"] == "all"
    assert client.get("/api/system/logs?scope=current&role=api").json()["entries"] == []


def test_current_launch_boundary_survives_api_restart_and_ignores_stale_registry(
    tmp_path, monkeypatch
):
    fallback = datetime(2026, 9, 17, 4, 40, tzinfo=UTC)
    started = datetime(2026, 9, 17, 4, 30, tzinfo=UTC)
    (tmp_path / "runtime").mkdir()
    registry = tmp_path / "runtime/launcher.json"
    registry.write_text(
        json.dumps({"launch_id": "current", "launcher": {"created_at": started.timestamp()}})
    )
    monkeypatch.setenv("AGENTS_IDE_LAUNCH_ID", "current")
    assert current_launch_started_at(tmp_path, fallback) == started
    monkeypatch.setenv("AGENTS_IDE_LAUNCH_ID", "different")
    assert current_launch_started_at(tmp_path, fallback) == fallback
    monkeypatch.setenv("AGENTS_IDE_LAUNCH_ID", "current")
    registry.write_text("invalid json")
    assert current_launch_started_at(tmp_path, fallback) == fallback
    monkeypatch.delenv("AGENTS_IDE_LAUNCH_ID")
    assert current_launch_started_at(tmp_path, fallback) == fallback


def test_large_logs_are_bounded_and_partial_records_do_not_break_reader(tmp_path):
    entry = {"at": "2026-09-17T04:00:00+00:00", "level": "ERROR", "message": "latest.failure"}
    (tmp_path / "api.jsonl").write_text(
        "x" * (MAX_FILE_BYTES + 10) + "\n" + json.dumps(entry) + "\n", encoding="utf-8"
    )
    report = read_application_logs(tmp_path)
    assert report.truncated
    assert [row.message for row in report.entries] == ["latest.failure"]


def test_unexpected_cli_failure_is_persisted_and_readable_offline(tmp_path):
    data_dir = tmp_path / "app"
    program = """
import sys
from agents_ide import cli
def fail(_settings):
    raise RuntimeError('private prompt and credential that must not be logged')
cli.run_worker = fail
sys.argv = ['agents-ide', '--data-dir', sys.argv[1], 'worker']
cli.main()
"""
    crashed = subprocess.run(
        [sys.executable, "-c", program, str(data_dir)], capture_output=True, timeout=20
    )
    assert crashed.returncode == 1
    output = subprocess.run(
        [sys.executable, "-m", "agents_ide", "--data-dir", str(data_dir), "logs"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )
    assert output.returncode == 0, output.stderr
    assert "service.unexpected_error" in output.stdout and "RuntimeError" in output.stdout
    assert '"function": "fail"' in output.stdout
    assert "private prompt" not in output.stdout
    assert not (data_dir / "db/agents-ide.db").exists()
