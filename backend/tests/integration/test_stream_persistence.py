import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import event, select
from test_planning_native import setup_native
from test_stage4_review import make_run
from test_stage5_review import command, runner_for, wait_for

from agents_ide.adapters.base import AdapterError, ExternalOutcome, LLMResult
from agents_ide.domain.common import to_json
from agents_ide.engine.planning_native import PlanningSupervisor
from agents_ide.engine.planning_worker import _prepare, claim_planning_job
from agents_ide.engine.runner import Runner
from agents_ide.engine.stream_buffer import StreamEventBuffer
from agents_ide.persistence.models import PlanningAttempt, PlanningEvent, RunEvent
from agents_ide.worker.processes import ProcessRegistry


def native(delta):
    return {
        "harness": "codex",
        "native_type": "item/agentMessage/delta",
        "session_id": "s",
        "payload": {
            "method": "item/agentMessage/delta",
            "params": {"threadId": "s", "turnId": "t", "itemId": "i", "delta": delta},
        },
    }


@pytest.mark.parametrize("kind", ["attempt.text_delta", "agent.native_event", "agent.output_delta"])
def test_full_64k_block_including_unicode_stays_inline_in_history(
    authenticated, tmp_path, settings, monkeypatch, kind
):
    run, factory = make_run(authenticated, tmp_path)
    runner, _ = runner_for(run, factory, settings)

    def payload(text):
        if kind == "agent.native_event":
            return native(text)
        if kind == "agent.output_delta":
            return {"native_type": "item/commandExecution/outputDelta", "details": {"delta": text}}
        return {"text": text}

    # Fill exactly 64 KiB before Runner adds late/source. This catches both the
    # old 32 KiB batch ceiling and accidental replacement by an artifact link.
    prefix = "😀" * 1000
    overhead = len(to_json({**payload(""), "coalesced_chunks": 17}).encode("utf-8"))
    tail = "x" * (64 * 1024 - 16 * len(prefix.encode("utf-8")) - overhead)
    expected = prefix * 16 + tail

    class Adapter:
        def run(self, request):
            for chunk in [prefix] * 16 + [tail]:
                request.emit_event(kind, payload(chunk))
            return LLMResult(ExternalOutcome.SUCCEEDED, "ok", {"text": "ok"}, None)

    monkeypatch.setattr(Runner, "_build_adapters", lambda *_: (None, Adapter()))
    assert runner.execute(run["id"]).final_state == "completed"
    with factory() as session:
        rows = list(session.scalars(select(RunEvent).where(RunEvent.type == kind)))
        if kind in {"agent.native_event", "agent.output_delta"}:
            assert not rows
            return
        assert len(rows) == 1
        assert 64 * 1024 < len(rows[0].payload_json.encode("utf-8")) <= 65 * 1024
    client, _ = authenticated
    response = client.get(f"/api/runs/{run['id']}/history")
    assert response.status_code == 200
    body = next(row["payload"] for row in response.json()["events"] if row["type"] == kind)
    assert "artifact_id" not in body and body["coalesced_chunks"] == 17
    if kind == "agent.native_event":
        text = body["payload"]["params"]["delta"]
    elif kind == "agent.output_delta":
        text = body["details"]["delta"]
    else:
        text = body["text"]
    assert text == expected


@pytest.mark.parametrize("failed", [False, True])
def test_stream_transactions_are_batched_and_tail_precedes_result(
    authenticated, tmp_path, settings, monkeypatch, failed
):
    run, factory = make_run(authenticated, tmp_path)
    runner, _ = runner_for(run, factory, settings)
    commits, observed = [], {}

    def committed(connection):
        if threading.current_thread().name == "adapter-call":
            commits.append(True)

    class Adapter:
        def run(self, request):
            before = len(commits)
            for _ in range(1000):
                request.emit_event("agent.native_event", native("x"))
                request.emit_event("attempt.text_delta", {"text": "x"})
            observed["during_chunks"] = len(commits) - before
            request.emit_event("agent.tool_call", {"call_id": "tool", "status": "completed"})
            observed["after_boundary"] = len(commits) - before
            for _ in range(100):
                request.emit_event("attempt.text_delta", {"text": "y"})
            if failed:
                raise OSError("provider disconnected")
            return LLMResult(ExternalOutcome.SUCCEEDED, "ok", {"text": "ok"}, None)

    monkeypatch.setattr(Runner, "_build_adapters", lambda *_: (None, Adapter()))
    engine = factory.kw["bind"]
    event.listen(engine, "commit", committed)
    try:
        result = runner.execute(run["id"])
    finally:
        event.remove(engine, "commit", committed)
    assert result.final_state == ("waiting_input" if failed else "completed")
    assert observed == {"during_chunks": 0, "after_boundary": 1}
    assert len(commits) == 2  # 2,101 callbacks, including native archive and tool boundary.
    with factory() as session:
        rows = list(session.scalars(select(RunEvent).order_by(RunEvent.sequence)))
        texts = [row for row in rows if row.type == "attempt.text_delta"]
        assert [json.loads(row.payload_json)["text"] for row in texts] == ["x" * 1000, "y" * 100]
        assert not any(row.type in {"agent.native_event", "agent.tool_call"} for row in rows)
        finished = next(row for row in rows if row.type == "attempt.finished")
        assert texts[0].sequence < texts[1].sequence < finished.sequence
        assert all(row.step_attempt_id == finished.step_attempt_id for row in texts)


def test_stalled_stream_is_visible_and_stop_flushes_pending_tail(
    authenticated, tmp_path, settings, monkeypatch
):
    run, factory = make_run(authenticated, tmp_path)
    runner, _ = runner_for(run, factory, settings)
    release, add_tail, tail_ready = threading.Event(), threading.Event(), threading.Event()

    class Adapter:
        def run(self, request):
            request.emit_event("attempt.text_delta", {"text": "visible within two seconds"})
            while not release.wait(0.01):
                if add_tail.is_set() and not tail_ready.is_set():
                    request.emit_event("attempt.text_delta", {"text": " tail on stop"})
                    tail_ready.set()
                if request.stop_event.is_set():
                    break
            return LLMResult(
                ExternalOutcome.RETRYABLE_FAILURE,
                "",
                None,
                None,
                error=AdapterError("interrupted", "Stopped", "safe"),
                no_effect=True,
            )

    monkeypatch.setattr(Runner, "_build_adapters", lambda *_: (None, Adapter()))
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(runner.execute, run["id"])
        try:
            wait_for(
                factory,
                lambda session: session.scalar(
                    select(RunEvent).where(RunEvent.type == "attempt.text_delta")
                ),
                timeout=5,
            )
            assert not future.done()
            add_tail.set()
            assert tail_ready.wait(2)
            assert command(authenticated, run, "stop").status_code == 200
            assert future.result(timeout=5).final_state == "stopped"
        finally:
            release.set()
    with factory() as session:
        rows = list(session.scalars(select(RunEvent).order_by(RunEvent.sequence)))
        texts = [row for row in rows if row.type == "attempt.text_delta"]
        assert [json.loads(row.payload_json)["text"] for row in texts] == [
            "visible within two seconds",
            " tail on stop",
        ]
        finished = next(row for row in rows if row.type == "attempt.finished")
        assert texts[-1].sequence < finished.sequence


def test_planning_stream_batch_commits_once_and_retains_session_identity(
    authenticated, tmp_path, monkeypatch
):
    client, job, _ = setup_native(authenticated, tmp_path)
    factory = client.app.state.session_factory
    claim = claim_planning_job(factory, "stream-test", job["id"])
    attempt_id, _, _ = _prepare(factory, claim)
    supervisor = PlanningSupervisor(
        factory, ProcessRegistry(), job["id"], claim.owner, claim.generation
    )
    buffer = StreamEventBuffer(lambda batch: supervisor.emit_batch(attempt_id, batch))
    commits = []
    engine = factory.kw["bind"]

    def committed(connection):
        commits.append(True)

    event.listen(engine, "commit", committed)
    try:
        buffer.emit("agent.session_created", {"session_id": "s"})
        for _ in range(1000):
            buffer.emit("agent.native_event", native("x"))
            buffer.emit("attempt.text_delta", {"text": "x"})
        assert len(commits) == 1
        buffer.close()
        assert len(commits) == 2
    finally:
        event.remove(engine, "commit", committed)
    with factory() as session:
        runtime = json.loads(session.get(PlanningAttempt, attempt_id).runtime_json)
        assert runtime["session_id"] == "s" and runtime["event_count"] == 3
        text = session.scalar(
            select(PlanningEvent).where(
                PlanningEvent.job_id == job["id"],
                PlanningEvent.type == "planning.attempt.text_delta",
            )
        )
        assert json.loads(text.payload_json)["text"] == "x" * 1000
