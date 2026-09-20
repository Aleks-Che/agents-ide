"""Disk work stays bounded while polling long histories and renewing leases."""

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack

import pytest
from council_support import create, document, setup
from sqlalchemy import event, insert
from test_stage4_review import make_run
from test_stage5_review import command

from agents_ide.adapters.base import ExternalOutcome, LLMResult
from agents_ide.engine import planning_worker
from agents_ide.engine.events_stream import fetch_events_after
from agents_ide.engine.runner import Runner
from agents_ide.persistence.database import create_database, migrate
from agents_ide.persistence.models import Run, RunEvent
from agents_ide.security.filesystem import prepare_data_dir
from agents_ide.worker.main import dispatch_once


def test_database_reuses_connections_without_capping_concurrency(settings):
    prepare_data_dir(settings.data_dir)
    migrate(settings)
    engine = create_database(settings)
    opened, closed = [], []
    event.listen(engine, "connect", lambda *args: opened.append(True))
    event.listen(engine, "close", lambda *args: closed.append(True))
    try:
        for _ in range(50):
            with engine.connect() as connection:
                assert connection.exec_driver_sql("PRAGMA synchronous").scalar() == 2
                assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        assert len(opened) == 1 and not closed
        with ExitStack() as stack:
            for _ in range(20):
                connection = stack.enter_context(engine.connect())
                assert connection.exec_driver_sql("SELECT 1").scalar() == 1
        assert len(opened) == 20 and len(closed) == 15
    finally:
        engine.dispose()
    assert len(closed) == len(opened)
    # No retained Windows database handles after shutdown/maintenance disposal.
    settings.database_path.rename(settings.database_path.with_suffix(".closed"))


@pytest.mark.parametrize("history_view", [False, True])
def test_idle_history_poll_seeks_index_without_reading_large_run_payloads(
    authenticated, tmp_path, history_view
):
    run, factory = make_run(authenticated, tmp_path)
    count = 20_000
    with factory() as session:
        row = session.get(Run, run["id"])
        runtime = json.loads(row.runtime_json)
        runtime["padding"] = "x" * 1024**2
        row.runtime_json = json.dumps(runtime)
        session.execute(
            insert(RunEvent),
            [
                {
                    "id": f"event-{index}",
                    "run_id": run["id"],
                    "sequence": index,
                    "type": "attempt.progress",
                    "occurred_at": 1,
                    "persisted_at": 1,
                }
                for index in range(2, count + 1)
            ],
        )
        session.commit()

    steps, statements = [], []
    with factory() as session:
        connection = session.connection()
        if not history_view:
            connection.exec_driver_sql("BEGIN")
        driver = connection.connection.driver_connection
        driver.set_progress_handler(lambda: steps.append(True) or 0, 100)
        driver.set_trace_callback(statements.append)
        try:
            if history_view:
                from agents_ide.services.run_observation import read_history

                page = read_history(
                    session,
                    run["id"],
                    before=count + 1,
                    limit=1,
                    category="all",
                    node_id=None,
                    execution_id=None,
                )
                assert len(page.events) == 1 and page.last_sequence == count
            else:
                batch = fetch_events_after(session, run["id"], after_sequence=count)
                assert batch.events == [] and batch.last_sequence == count and not batch.has_more
        finally:
            driver.set_progress_handler(None, 0)
            driver.set_trace_callback(None)
    assert len(steps) * 100 < 2000
    assert all("snapshot_json" not in sql and "runtime_json" not in sql for sql in statements)


def test_run_lease_writes_follow_heartbeat_interval(authenticated, tmp_path, settings, monkeypatch):
    run, factory = make_run(authenticated, tmp_path)
    entered, release = threading.Event(), threading.Event()
    renewals = []

    class Adapter:
        def run(self, request):
            entered.set()
            assert release.wait(15)
            return LLMResult(ExternalOutcome.SUCCEEDED, "ok", {"text": "ok"}, None)

    def observe(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("UPDATE queue_jobs SET lease_expires_at="):
            renewals.append(time.monotonic())

    monkeypatch.setattr(Runner, "_build_adapters", lambda *_: (None, Adapter()))
    engine = factory.kw["bind"]
    event.listen(engine, "before_cursor_execute", observe)
    try:
        with ThreadPoolExecutor(1) as executor:
            future = executor.submit(dispatch_once, settings, "disk-test", factory)
            try:
                assert entered.wait(5)
                assert not release.wait(5.4)
                assert 1 <= len(renewals) <= 2
                assert command(authenticated, run, "pause").status_code == 200
            finally:
                release.set()
            assert future.result(timeout=5)
    finally:
        event.remove(engine, "before_cursor_execute", observe)


def test_successful_gets_only_log_at_debug_but_errors_and_writes_remain_visible(
    authenticated, caplog
):
    client, headers = authenticated
    with caplog.at_level(logging.INFO):
        client.get("/api/health")
        assert not [row for row in caplog.records if row.msg == "request.finished"]
        client.get("/api/does-not-exist")
        client.post("/api/templates", headers=headers, json={"name": "logging test"})
    records = [row for row in caplog.records if row.msg == "request.finished"]
    assert len(records) == 2 and all(row.levelno == logging.INFO for row in records)
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        client.get("/api/health")
    assert any(
        row.msg == "request.finished" and row.levelno == logging.DEBUG for row in caplog.records
    )


def test_planning_renews_lease_without_writing_on_every_control_poll(
    authenticated, tmp_path, monkeypatch
):
    client, headers = authenticated
    *_, payload = setup(client, headers, tmp_path)
    job = create(client, headers, payload)
    factory = client.app.state.session_factory
    entered, release = threading.Event(), threading.Event()
    renewals = []

    def invoke(*args, **kwargs):
        entered.set()
        assert release.wait(15)
        return LLMResult(ExternalOutcome.SUCCEEDED, json.dumps(document()), None, None)

    def observe(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("UPDATE planning_jobs SET lease_expires_at="):
            renewals.append(time.monotonic())

    monkeypatch.setattr(planning_worker, "_invoke_external", invoke)
    engine = factory.kw["bind"]
    event.listen(engine, "before_cursor_execute", observe)
    try:
        with ThreadPoolExecutor(1) as executor:
            future = executor.submit(
                planning_worker.dispatch_planning_job, factory, job["id"], simulated=True
            )
            try:
                assert entered.wait(5)
                assert not release.wait(5.4)
                assert 1 <= len(renewals) <= 2
            finally:
                release.set()
            assert future.result(timeout=5).final_state == "ready_for_confirmation"
    finally:
        event.remove(engine, "before_cursor_execute", observe)
