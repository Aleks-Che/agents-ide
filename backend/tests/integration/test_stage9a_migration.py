from pathlib import Path

from alembic import command
from alembic.config import Config
from council_support import create, setup

from agents_ide.engine.planning_worker import claim_planning_job
from agents_ide.persistence import database


def test_stage13_history_survives_upgrade_and_legacy_job_is_not_replayed(authenticated, tmp_path):
    client, headers = authenticated
    *_, payload = setup(client, headers, tmp_path)
    job = create(client, headers, payload)
    with client.app.state.engine.connect() as connection:
        config = Config()
        config.set_main_option(
            "script_location", str(Path(database.__file__).parent / "migrations")
        )
        config.attributes["connection"] = connection
        command.downgrade(config, "0013_planning_council")
        connection.commit()
        before = connection.exec_driver_sql(
            "SELECT task_text, context_snapshot_json FROM planning_jobs"
        ).one()
        command.upgrade(config, "head")
        connection.commit()
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert (
            connection.exec_driver_sql(
                "SELECT task_text, context_snapshot_json FROM planning_jobs"
            ).one()
            == before
        )
    assert claim_planning_job(client.app.state.session_factory, "migration-test", job["id"]) is None
    result = client.get(f"/api/planning_jobs/{job['id']}").json()
    assert result["last_error"]["code"] == "legacy_planning_unverifiable"
    assert result["usage"]["external_calls"] == 0
