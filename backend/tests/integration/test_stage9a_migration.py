from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from council_support import ANSWERS, QUESTIONS, confirm, create, dispatch, setup
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError

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


def test_retry_migration_preserves_pinned_candidates(authenticated, tmp_path):
    client, headers = authenticated
    *_, payload = setup(client, headers, tmp_path)
    create(client, headers, payload)
    with client.app.state.engine.connect() as connection:
        config = Config()
        config.set_main_option(
            "script_location", str(Path(database.__file__).parent / "migrations")
        )
        config.attributes["connection"] = connection
        command.downgrade(config, "0014_council_safety")
        connection.commit()
        before = connection.exec_driver_sql(
            "SELECT id, candidates_json, candidate_index FROM planning_members ORDER BY id"
        ).all()
        command.upgrade(config, "head")
        connection.commit()
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert (
            connection.exec_driver_sql(
                "SELECT id, candidates_json, candidate_index FROM planning_members ORDER BY id"
            ).all()
            == before
        )
        assert (
            connection.exec_driver_sql("SELECT access_overrides_json FROM planning_members")
            .scalars()
            .all()
            == ["{}"] * 3
        )
        assert connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar() == (
            database.SCHEMA_REVISION
        )


def test_single_member_migration_allows_degraded_author(authenticated, tmp_path):
    client, headers = authenticated
    *_, payload = setup(client, headers, tmp_path)
    job = dispatch(client, create(client, headers, payload), questions=QUESTIONS)
    response = client.post(
        f"/api/planning_jobs/{job['id']}/answers",
        headers=headers,
        json={"expected_revision": 1, "answers": ANSWERS},
    )
    assert response.status_code == 200, response.text
    confirm(client, headers, client.get(f"/api/planning_jobs/{job['id']}").json())
    with client.app.state.engine.connect() as connection:
        config = Config()
        config.set_main_option(
            "script_location", str(Path(database.__file__).parent / "migrations")
        )
        config.attributes["connection"] = connection
        command.downgrade(config, "0015_council_retry")
        connection.commit()
        before = connection.exec_driver_sql("SELECT * FROM planning_revisions ORDER BY id").all()
        answers = connection.exec_driver_sql("SELECT * FROM planning_answers ORDER BY id").all()
        assert len(before) == 2 and len(answers) == 3
        command.upgrade(config, "head")
        connection.commit()
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert (
            connection.exec_driver_sql("SELECT * FROM planning_revisions ORDER BY id").all()
            == before
        )
        assert (
            connection.exec_driver_sql("SELECT * FROM planning_answers ORDER BY id").all()
            == answers
        )
        # The new author type is accepted; downgrade would reject it on insert.
        connection.exec_driver_sql(
            "INSERT INTO planning_revisions (id, job_id, revision_number, author,"
            " parse_status, readiness, plan_json, questions_json, answers_json,"
            " questions_hash, confirmation_hash, answered_hash, body_text,"
            " body_truncated, confirmed_at, created_at)"
            " SELECT id || '-x', job_id, revision_number + 100, 'single_member',"
            " parse_status, readiness, plan_json, questions_json, answers_json,"
            " questions_hash, NULL, answered_hash, body_text, body_truncated,"
            " NULL, created_at FROM planning_revisions"
        )
        connection.commit()
        assert (
            connection.exec_driver_sql(
                "SELECT count(*) FROM planning_revisions WHERE author = 'single_member'"
            ).scalar()
            == 2
        )
        with pytest.raises(RuntimeError, match="Cannot downgrade"):
            command.downgrade(config, "0015_council_retry")
        connection.rollback()
        assert connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar() == (
            database.SCHEMA_REVISION
        )
        confirmed = connection.exec_driver_sql(
            "SELECT id FROM planning_revisions WHERE confirmed_at IS NOT NULL"
        ).scalar_one()
        for statement in (
            "UPDATE planning_revisions SET body_text = 'tampered' WHERE id = ?",
            "DELETE FROM planning_revisions WHERE id = ?",
        ):
            with pytest.raises(IntegrityError, match="confirmed_planning_revision_immutable"):
                connection.exec_driver_sql(statement, (confirmed,))
            connection.rollback()
        connection.exec_driver_sql("DELETE FROM planning_revisions WHERE author = 'single_member'")
        connection.commit()
        assert connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar() == (
            database.SCHEMA_REVISION
        )


def test_single_member_migration_failure_rolls_back_history_and_guards(
    authenticated, tmp_path, settings
):
    client, headers = authenticated
    *_, payload = setup(client, headers, tmp_path)
    job = dispatch(client, create(client, headers, payload), questions=QUESTIONS)
    assert (
        client.post(
            f"/api/planning_jobs/{job['id']}/answers",
            headers=headers,
            json={
                "expected_revision": 1,
                "answers": ANSWERS,
            },
        ).status_code
        == 200
    )
    confirm(client, headers, client.get(f"/api/planning_jobs/{job['id']}").json())
    with client.app.state.engine.connect() as connection:
        config = Config()
        config.set_main_option(
            "script_location", str(Path(database.__file__).parent / "migrations")
        )
        config.attributes["connection"] = connection
        command.downgrade(config, "0015_council_retry")
        connection.commit()
        before = connection.exec_driver_sql("SELECT * FROM planning_revisions ORDER BY id").all()
        answers = connection.exec_driver_sql("SELECT * FROM planning_answers ORDER BY id").all()

        def fail_restore(conn, cursor, statement, parameters, context, executemany):
            if statement.startswith("INSERT INTO planning_answers SELECT"):
                raise RuntimeError("injected restore failure")

        event.listen(connection, "before_cursor_execute", fail_restore)
        try:
            with pytest.raises(RuntimeError, match="injected restore failure"):
                command.upgrade(config, "head")
        finally:
            event.remove(connection, "before_cursor_execute", fail_restore)
        connection.rollback()
        assert (
            connection.exec_driver_sql("SELECT * FROM planning_revisions ORDER BY id").all()
            == before
        )
        assert (
            connection.exec_driver_sql("SELECT * FROM planning_answers ORDER BY id").all()
            == answers
        )
        assert (
            connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar()
            == "0015_council_retry"
        )
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        assert (
            len(
                connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='trigger' "
                    "AND name LIKE 'planning_confirmed_%'"
                ).all()
            )
            == 2
        )
    # Retry via the production launcher/API entry point, on a fresh connection.
    database.migrate(settings)
    assert database.check_database(client.app.state.engine)
    with client.app.state.engine.connect() as connection:
        assert (
            connection.exec_driver_sql("SELECT * FROM planning_revisions ORDER BY id").all()
            == before
        )
        assert (
            connection.exec_driver_sql("SELECT * FROM planning_answers ORDER BY id").all()
            == answers
        )
