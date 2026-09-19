"""Provider failures advance the group without replaying failed candidates."""

import json
import threading
from dataclasses import replace

import httpx
import pytest
from sqlalchemy import select
from test_stage4_review import make_run
from test_stage5_review import run_now, runner_for
from test_stage7_llm_commands_evidence import _request

from agents_ide.adapters.llm_http import HttpLLMAdapter, _Interrupted
from agents_ide.persistence.models import QueueJob, Run, RunEvent, StepAttempt, StepExecution


def mock_provider(monkeypatch, case, *, fail_all=False):
    original = httpx.AsyncClient
    calls = []

    async def handler(request):
        model = json.loads(request.content)["model"]
        calls.append(model)
        if model == "alpha" or fail_all:
            if isinstance(case, int):
                return httpx.Response(case, json={"error": {"message": "Request failed"}})
            if case == "disconnect":
                raise httpx.RemoteProtocolError("connection closed before response")
            if case == "timeout":
                raise httpx.ReadTimeout("read failed")
            if case == "invalid_json":
                return httpx.Response(200, text="invalid JSON")
            if case == "native_error":
                return httpx.Response(200, json={"error": {"message": "Unknown provider error"}})
            if case == "truncated":
                return httpx.Response(
                    200,
                    json={
                        "choices": [{"message": {"content": "partial"}, "finish_reason": "length"}]
                    },
                )
            raise AssertionError(case)
        return httpx.Response(200, json={"choices": [{"message": {"content": "completed"}}]})

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw)
    )
    return calls


def attempts_for(factory, run_id):
    with factory() as session:
        return list(
            session.scalars(
                select(StepAttempt)
                .join(StepExecution, StepExecution.id == StepAttempt.execution_id)
                .where(StepExecution.run_id == run_id)
                .order_by(StepAttempt.started_at)
            )
        )


@pytest.mark.parametrize(
    "case",
    [
        400,
        401,
        403,
        408,
        422,
        429,
        500,
        503,
        "disconnect",
        "timeout",
        "invalid_json",
        "native_error",
        "truncated",
    ],
)
def test_llm_group_advances_on_every_provider_failure(
    authenticated, tmp_path, settings, monkeypatch, case
):
    calls = mock_provider(monkeypatch, case)
    run, factory = make_run(authenticated, tmp_path, group=True, execution_mode="real")
    result = run_now(run, factory, settings)
    assert result.final_state == "completed", result
    assert calls == ["alpha", "beta"]
    attempts = attempts_for(factory, run["id"])
    assert [a.status for a in attempts] == ["failed", "succeeded"]
    assert json.loads(attempts[0].error_details_json)["can_fallback"]
    assert [json.loads(a.selection_json)["model_id"] for a in attempts] == calls


def test_llm_group_stops_only_after_all_candidates_fail(
    authenticated, tmp_path, settings, monkeypatch
):
    calls = mock_provider(monkeypatch, "disconnect", fail_all=True)
    run, factory = make_run(authenticated, tmp_path, group=True, execution_mode="real")
    result = run_now(run, factory, settings)
    assert result.waiting_reason.code == "model_group_exhausted"
    assert calls == ["alpha", "beta"]
    assert len(result.waiting_reason.details["candidates"]) == 2
    assert all(
        not json.loads(a.error_details_json)["no_effect"] for a in attempts_for(factory, run["id"])
    )


def test_llm_fallback_cursor_survives_worker_restart(
    authenticated, tmp_path, settings, monkeypatch
):
    calls = mock_provider(monkeypatch, "disconnect")
    run, factory = make_run(authenticated, tmp_path, group=True, execution_mode="real")
    runner, _ = runner_for(run, factory, settings)
    original = runner._save_attempt

    def save(*args):
        original(*args)
        raise RuntimeError("crash after saving provider failure")

    monkeypatch.setattr(runner, "_save_attempt", save)
    with pytest.raises(RuntimeError, match="crash after saving"):
        runner.execute(run["id"])
    with factory() as session:
        runtime = json.loads(session.get(Run, run["id"]).runtime_json)
        assert runtime["next_candidate_index"] == 1
        assert runtime["candidate_history"][-1]["reason"] == "provider_transport_error"
        job = session.scalar(select(QueueJob).where(QueueJob.run_id == run["id"]))
        job.lease_expires_at = 0
        job.owner_pid, job.owner_create_time = 99999999, 1
        session.commit()
    assert run_now(run, factory, settings).final_state == "completed"
    assert calls == ["alpha", "beta"]
    with factory() as session:
        switches = list(
            session.scalars(
                select(RunEvent).where(
                    RunEvent.run_id == run["id"], RunEvent.type == "model_group.candidate_switched"
                )
            )
        )
        assert len(switches) == 1
        assert json.loads(switches[0].payload_json)["reason"] == "provider_transport_error"


@pytest.mark.parametrize("stopped_before_dispatch", [False, True])
def test_user_stop_never_allows_llm_fallback(monkeypatch, stopped_before_dispatch):
    stop = threading.Event()
    if stopped_before_dispatch:
        stop.set()

    async def interrupted(*args):
        stop.set()
        raise _Interrupted

    adapter = HttpLLMAdapter()
    monkeypatch.setattr(adapter, "_receive", interrupted)
    result = adapter.run(replace(_request("http://127.0.0.1:9/v1"), stop_event=stop))
    assert result.error.code == "interrupted"
    assert not result.can_fallback
