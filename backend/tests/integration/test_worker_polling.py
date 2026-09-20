"""A waiting adapter keeps responsive controls without continuous DB writes."""

import threading
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import event
from test_stage4_review import make_run
from test_stage5_review import command, runner_for

from agents_ide.adapters.base import AdapterError, ExternalOutcome, LLMResult
from agents_ide.engine.runner import Runner


def test_waiting_call_limits_heartbeat_writes_and_still_stops(
    authenticated, tmp_path, settings, monkeypatch
):
    run, factory = make_run(authenticated, tmp_path)
    runner, _ = runner_for(run, factory, settings)
    entered, release = threading.Event(), threading.Event()
    heartbeat_writes = []

    class Adapter:
        def run(self, request):
            entered.set()
            while not release.wait(0.01):
                if request.stop_event.is_set():
                    return LLMResult(
                        ExternalOutcome.RETRYABLE_FAILURE,
                        "",
                        None,
                        None,
                        error=AdapterError("interrupted", "Stopped", "safe"),
                        no_effect=True,
                    )
            return LLMResult(ExternalOutcome.SUCCEEDED, "ok", {"text": "ok"}, None)

    def observe(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("UPDATE step_attempts SET heartbeat_at="):
            heartbeat_writes.append(statement)

    monkeypatch.setattr(Runner, "_build_adapters", lambda *_: (None, Adapter()))
    engine = factory.kw["bind"]
    with ThreadPoolExecutor(1) as executor:
        event.listen(engine, "before_cursor_execute", observe)
        future = executor.submit(runner.execute, run["id"])
        try:
            assert entered.wait(5)
            assert not release.wait(1.3)
            assert 1 <= len(heartbeat_writes) <= 2
            assert command(authenticated, run, "stop").status_code == 200
            assert future.result(timeout=3).final_state == "stopped"
        finally:
            release.set()
            event.remove(engine, "before_cursor_execute", observe)
