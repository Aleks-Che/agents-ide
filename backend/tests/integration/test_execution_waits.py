"""Slow work and SQLite contention must not turn into task failures."""

import json
import queue
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from agents_ide.adapters.base import ExternalOutcome
from agents_ide.engine.commands import execute_commands
from agents_ide.services.transactions import begin_write


def test_sqlite_writer_waits_past_old_five_second_limit(client, settings):
    factory = client.app.state.session_factory
    ready = threading.Event()

    def write():
        with factory() as session:
            ready.set()
            begin_write(session)
            session.connection().exec_driver_sql("CREATE TABLE waited_for_writer (value INTEGER)")
            session.commit()

    with ThreadPoolExecutor(max_workers=1) as executor:
        blocker = sqlite3.connect(settings.database_path)
        try:
            blocker.execute("BEGIN IMMEDIATE")
            future = executor.submit(write)
            assert ready.wait(2)
            time.sleep(5.3)
            assert not future.done(), "SQLite contention must keep waiting, not abort the task"
        finally:
            blocker.rollback()
            blocker.close()
        future.result(timeout=3)
    with client.app.state.engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT count(*) FROM waited_for_writer").scalar() == 0


def test_sqlite_wait_is_cancellable(client, settings):
    cancel = threading.Event()
    ready = threading.Event()

    def write():
        with client.app.state.session_factory() as session:
            ready.set()
            begin_write(session, cancel=cancel)

    with ThreadPoolExecutor(max_workers=1) as executor:
        blocker = sqlite3.connect(settings.database_path)
        try:
            blocker.execute("BEGIN IMMEDIATE")
            future = executor.submit(write)
            assert ready.wait(2)
            cancel.set()
            with pytest.raises(InterruptedError):
                future.result(timeout=2)
        finally:
            blocker.rollback()
            blocker.close()


def test_sqlite_stale_read_snapshot_is_not_retried_forever(client):
    from sqlalchemy.exc import OperationalError

    engine = client.app.state.engine
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE snapshot_test (value INTEGER)")
    with engine.connect() as reader:
        reader.exec_driver_sql("BEGIN")
        reader.exec_driver_sql("SELECT * FROM snapshot_test").all()
        with engine.begin() as writer:
            writer.exec_driver_sql("INSERT INTO snapshot_test VALUES (1)")
        with pytest.raises(OperationalError) as error:
            reader.exec_driver_sql("INSERT INTO snapshot_test VALUES (2)")
        assert error.value.orig.sqlite_errorcode == sqlite3.SQLITE_BUSY_SNAPSHOT


def test_command_ignores_saved_timeout_and_deadline(tmp_path):
    from test_stage7_llm_commands_evidence import _local_launcher, _spec

    result = execute_commands(
        [_spec(args=("-c", "import time; time.sleep(0.2); print('finished')"), timeout=0.01)],
        workspace=tmp_path,
        launcher=_local_launcher(),
        deadline_at=time.monotonic() - 1,
    )
    assert result.outcome == ExternalOutcome.SUCCEEDED
    command = json.loads(result.raw_text)["commands"][0]
    assert command["status"] == "completed"
    assert "finished" in command["stdout"]


def test_command_still_obeys_stop(tmp_path):
    from test_stage7_llm_commands_evidence import _local_launcher, _spec

    stop = threading.Event()
    timer = threading.Timer(0.4, stop.set)
    timer.start()
    try:
        result = execute_commands(
            [_spec(args=("-c", "import time; time.sleep(30)"))],
            workspace=tmp_path,
            launcher=_local_launcher(),
            stop_event=stop,
        )
    finally:
        timer.cancel()
    assert json.loads(result.raw_text)["commands"][0]["status"] == "interrupted"


def test_codex_rpc_waits_past_old_default_timeout(monkeypatch):
    from itertools import count
    from types import SimpleNamespace

    from agents_ide.adapters import codex

    clock = count(0, 60)
    monkeypatch.setattr(codex, "time", SimpleNamespace(monotonic=lambda: next(clock)))

    class Stream:
        lock = threading.RLock()
        calls = 0

        def next_id(self):
            return 1

        def is_alive(self):
            return True

        def send(self, message):
            pass

        def receive(self, timeout):
            self.calls += 1
            if self.calls == 1:
                raise queue.Empty
            return {"id": 1, "result": {"ready": True}}

    adapter = codex.CodexAdapter(stream=Stream(), session=codex.RunSession("", {}, None))
    assert adapter._request_response("initialize", {})["result"]["ready"]


def test_git_has_no_default_elapsed_time_limit(tmp_path, monkeypatch):
    from itertools import count
    from types import SimpleNamespace

    from agents_ide.engine import git_process

    clock = count(0, 60)
    monkeypatch.setattr(
        git_process, "time", SimpleNamespace(monotonic=lambda: next(clock), sleep=time.sleep)
    )
    assert b"git version" in git_process.run_git(tmp_path, ["--version"])


def test_maintenance_waits_for_live_owner_with_delayed_heartbeat(authenticated, tmp_path, settings):
    from test_stage4_review import make_run

    from agents_ide.engine.queue import abandon_job, claim_next_job
    from agents_ide.operations.maintenance import _busy
    from agents_ide.persistence.models import QueueJob

    _, factory = make_run(authenticated, tmp_path)
    claim = claim_next_job(factory, worker_id="live", lease_seconds=30)
    with factory() as session:
        session.get(QueueJob, claim.job_id).lease_expires_at = 0
        session.commit()
    assert _busy(settings)
    abandon_job(factory, job_id=claim.job_id, worker_id="live", generation=claim.generation)
    assert not _busy(settings)
