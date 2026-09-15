"""Regression tests for the stage 4 review; assert durable effects, not event presence."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select

from agents_ide.adapters.base import ExternalOutcome
from agents_ide.adapters.fake import FakeAgentAdapter, FakeLLMAdapter, FakeResponse, FakeScenario
from agents_ide.engine import artifacts, queue
from agents_ide.engine.events_stream import fetch_events_after, format_sse
from agents_ide.engine.runner import Runner
from agents_ide.errors import AppError
from agents_ide.persistence.models import (
    ArtifactManifest,
    QueueJob,
    StepAttempt,
    StepExecution,
)


def make_run(
    authenticated,
    tmp_path,
    *,
    graph=None,
    overrides=None,
    suffix="",
    group=False,
    disabled_first=False,
    **extra,
):
    client, headers = authenticated
    workspace = tmp_path / f"workspace{suffix}"
    workspace.mkdir(exist_ok=True)
    project = client.post(
        "/api/projects",
        headers=headers,
        json={"name": f"p{suffix}", "workspace_path": str(workspace)},
    ).json()
    connection = client.post(
        "/api/connections",
        headers=headers,
        json={"name": f"c{suffix}", "base_url": "http://127.0.0.1:9/v1"},
    ).json()
    profile = client.post(
        "/api/harness_profiles",
        headers=headers,
        json={"name": f"h{suffix}", "harness_kind": "codex", "settings": {}},
    ).json()
    if graph is None:
        graph = {
            "nodes": [
                {"id": "s", "type": "Start"},
                {
                    "id": "check",
                    "type": "LLMRequest",
                    "config": {
                        "prompt": "check",
                        "model_selection": {
                            "kind": "direct",
                            "model_id": "m",
                            "provider_connection_id": connection["id"],
                        },
                    },
                },
                {"id": "e", "type": "End"},
            ],
            "edges": [{"from": "s", "to": "check"}, {"from": "check", "to": "e"}],
        }
    else:
        graph = json.loads(
            json.dumps(graph)
            .replace("CONNECTION", connection["id"])
            .replace("PROFILE", profile["id"])
        )
    if group:
        other = client.post(
            "/api/connections",
            headers=headers,
            json={"name": f"other{suffix}", "base_url": "http://127.0.0.1:9/v1"},
        ).json()
        model_group = client.post(
            "/api/model_groups/llm",
            headers=headers,
            json={
                "name": f"g{suffix}",
                "members": [
                    {
                        "provider_connection_id": connection["id"],
                        "model_id": "alpha",
                        "enabled": not disabled_first,
                    },
                    {"provider_connection_id": other["id"], "model_id": "beta"},
                ],
            },
        ).json()
        next(node for node in graph["nodes"] if node["id"] == "check")["config"][
            "model_selection"
        ] = {"kind": "group", "group_id": model_group["id"]}
    template = client.post("/api/templates", headers=headers, json={"name": f"t{suffix}"}).json()
    version = client.post(
        f"/api/templates/{template['id']}/versions", headers=headers, json={"graph": graph}
    )
    assert version.status_code == 201, version.text
    binding = client.post(
        f"/api/versions/{version.json()['id']}/bindings",
        headers=headers,
        json={"project_id": project["id"], "name": "b"},
    ).json()
    response = client.post(
        "/api/runs",
        headers=headers,
        json={
            "execution_mode": "simulated",
            "project_id": project["id"],
            "binding_id": binding["id"],
            "message": "go",
            "idempotency_key": f"r{suffix}",
            "overrides": overrides or {},
            **extra,
        },
    )
    assert response.status_code == 201, response.text
    return response.json(), client.app.state.session_factory


def execute(run, factory, settings, monkeypatch, responses=()):
    scenario = FakeScenario(tuple(responses))
    monkeypatch.setattr(
        Runner, "_build_adapters", lambda *_: (FakeAgentAdapter(scenario), FakeLLMAdapter(scenario))
    )
    job = queue.claim_next_job(factory, worker_id="review", lease_seconds=30)
    assert job is not None
    runner = Runner(
        session_factory=factory,
        worker_id="review",
        generation=job.generation,
        data_dir=settings.data_dir,
        secret_store=None,
    )
    return runner.execute(run["id"]), job


def test_claim_is_durable_and_exclusive(authenticated, tmp_path):
    run, factory = make_run(authenticated, tmp_path)
    with ThreadPoolExecutor(2) as pool:
        jobs = list(
            pool.map(
                lambda owner: queue.claim_next_job(factory, worker_id=owner, lease_seconds=30),
                ["a", "b"],
            )
        )
    assert sum(job is not None for job in jobs) == 1
    with factory() as session:
        row = session.scalar(select(QueueJob).where(QueueJob.run_id == run["id"]))
        assert row.claimed_by in {"a", "b"}
        assert row.generation == 2


def test_refresh_rejects_expired_owner(authenticated, tmp_path):
    _, factory = make_run(authenticated, tmp_path)
    job = queue.claim_next_job(factory, worker_id="a", lease_seconds=1)
    with pytest.raises(AppError):
        queue.refresh_lease(
            factory,
            job_id=job.job_id,
            worker_id="a",
            expected_generation=job.generation,
            lease_seconds=30,
            now=job.lease_expires_at + 1,
        )


def test_attempt_and_execution_results_survive_new_session(
    authenticated, tmp_path, settings, monkeypatch
):
    run, factory = make_run(authenticated, tmp_path)
    result, _ = execute(run, factory, settings, monkeypatch)
    assert result.final_state == "completed"
    with factory() as session:
        steps = list(session.scalars(select(StepExecution)))
        assert all(s.status == "succeeded" and s.finished_at for s in steps)
        attempt = session.scalar(select(StepAttempt))
        assert attempt.status == "succeeded" and attempt.finished_at
        check = next(s for s in steps if s.node_id == "check")
        assert check.attempt_count == 1 and check.decision == "true"
        assert check.raw_result_ref and check.validated_result_json


def test_terminal_job_cannot_be_replayed(authenticated, tmp_path, settings, monkeypatch):
    run, factory = make_run(authenticated, tmp_path)
    _, job = execute(run, factory, settings, monkeypatch)
    queue.release_job(
        factory, job_id=job.job_id, worker_id="review", expected_generation=job.generation
    )
    assert queue.claim_next_job(factory, worker_id="next", lease_seconds=30) is None


@pytest.mark.parametrize(
    "outcome",
    [
        ExternalOutcome.UNKNOWN,
        ExternalOutcome.TRANSPORT_DROPPED,
        ExternalOutcome.PROCESS_DIED,
        ExternalOutcome.PERMISSION_DENIED,
        ExternalOutcome.INVALID_FORMAT,
    ],
)
def test_uncertain_or_policy_error_never_retries_or_falls_back(
    authenticated, tmp_path, settings, monkeypatch, outcome
):
    run, factory = make_run(authenticated, tmp_path)
    result, _ = execute(
        run, factory, settings, monkeypatch, [FakeResponse("check", 1, outcome=outcome)]
    )
    assert result.final_state == "waiting_input"
    with factory() as session:
        assert len(list(session.scalars(select(StepAttempt)))) == 1


def test_artifact_body_persists_and_nested_secrets_are_redacted(authenticated, tmp_path):
    run, factory = make_run(authenticated, tmp_path)
    with factory() as session:
        artifact = artifacts.record_artifact(
            session,
            run["id"],
            artifacts.ArtifactPayload(
                "test", body={"nested": {"api_key": "highly-private-credential"}, "text": "Привет"}
            ),
        )
        artifact_id = artifact.id
        session.commit()
    with factory() as session:
        row = session.get(ArtifactManifest, artifact_id)
        body = getattr(row, "body_json", None)
        assert body and "Привет" in body
        assert "highly-private-credential" not in body


def test_unknown_run_events_is_404(authenticated):
    client, headers = authenticated
    response = client.get("/api/runs/missing/events", headers=headers)
    assert response.status_code == 404


def test_sse_has_reconnect_id():
    chunks = "".join(format_sse([{"sequence": 7, "type": "run.completed"}]))
    assert "id: 7\n" in chunks


def test_ahead_cursor_requires_reset(authenticated, tmp_path):
    run, factory = make_run(authenticated, tmp_path)
    with factory() as session:
        batch = fetch_events_after(session, run["id"], after_sequence=999)
        assert batch.reset_required


def repair_graph(*, max_iterations=2):
    return {
        "nodes": [
            {"id": "s", "type": "Start"},
            {
                "id": "impl",
                "type": "AgentTask",
                "config": {
                    "prompt": "initial task",
                    "prompt_repair": "repair: {{ work.feedback }}",
                    "model_selection": {
                        "kind": "direct",
                        "model_id": "dev",
                        "harness_profile_id": "PROFILE",
                    },
                },
            },
            {
                "id": "check",
                "type": "LLMRequest",
                "config": {
                    "prompt": "verify {{ work.cycle_id }}",
                    "response_format": "json",
                    "output_schema": {
                        "type": "object",
                        "properties": {
                            "verdict": {
                                "type": "string",
                                "enum": ["passed", "failed", "inconclusive"],
                            },
                            "feedback": {"type": "string"},
                        },
                        "required": ["verdict", "feedback"],
                    },
                    "model_selection": {
                        "kind": "direct",
                        "model_id": "verifier",
                        "provider_connection_id": "CONNECTION",
                    },
                },
            },
            {
                "id": "route",
                "type": "Condition",
                "expression": {"ref": "steps.check.latest.decision"},
            },
            {
                "id": "unknown",
                "type": "LLMRequest",
                "config": {
                    "prompt": "inconclusive path",
                    "model_selection": {
                        "kind": "direct",
                        "model_id": "verifier",
                        "provider_connection_id": "CONNECTION",
                    },
                },
            },
            {"id": "e", "type": "End"},
        ],
        "edges": [
            {"from_node": "s", "to_node": "impl"},
            {"from": "impl", "to": "check"},
            {"from": "check", "to": "route"},
            {"id": "pass", "from": "route", "to": "e", "when": "true"},
            {
                "id": "repair",
                "from": "route",
                "to": "impl",
                "when": "false",
                "loop": {"id": "repair", "max_iterations": max_iterations},
                "assignments": {
                    "work.mode": {"const": "repair"},
                    "work.feedback": {"ref": "steps.check.latest.validated_result.feedback"},
                },
            },
            {"id": "unknown", "from": "route", "to": "unknown", "when": "unknown"},
            {"from": "unknown", "to": "e"},
        ],
    }


def verdict(value, visit=1):
    body = {"verdict": value, "feedback": "fix the assertion"}
    return FakeResponse(
        "check",
        1,
        visit_index=visit,
        raw_text=json.dumps(body),
        validated_result=body,
        decision=value,
    )


def test_actual_repair_cycle_persists_prompts_and_decisions(
    authenticated, tmp_path, settings, monkeypatch
):
    run, factory = make_run(authenticated, tmp_path, graph=repair_graph())
    result, _ = execute(
        run, factory, settings, monkeypatch, [verdict("failed"), verdict("passed", 2)]
    )
    assert result.final_state == "completed"
    with factory() as session:
        checks = list(
            session.scalars(
                select(StepExecution)
                .where(StepExecution.node_id == "check")
                .order_by(StepExecution.visit_index)
            )
        )
        assert [(s.visit_index, s.cycle_id, s.decision) for s in checks] == [
            (1, 1, "false"),
            (2, 2, "true"),
        ]
        assert all(s.status == "succeeded" and s.attempt_count == 1 for s in checks)
        bodies = [
            json.loads(a.body_json)
            for a in session.scalars(
                select(ArtifactManifest)
                .where(ArtifactManifest.schema_type == "attempt_input")
                .order_by(ArtifactManifest.created_at)
            )
        ]
        assert [b["prompt"] for b in bodies] == [
            "initial task",
            "verify 1",
            "repair: fix the assertion",
            "verify 2",
        ]
    client, headers = authenticated
    snapshot = client.get(f"/api/runs/{run['id']}/snapshot", headers=headers).json()
    assert snapshot["run"]["runtime"]["external_calls"] == 4
    assert snapshot["run"]["runtime"]["backward_transitions"] == 1
    assert snapshot["run"]["simulated"] is True
    rows = client.get(f"/api/runs/{run['id']}/artifacts", headers=headers).json()
    body = client.get(f"/api/runs/{run['id']}/artifacts/{rows[0]['id']}", headers=headers).json()
    assert body["body"] and body["source_kind"] == "simulated"


def test_inconclusive_takes_unknown_not_repair(authenticated, tmp_path, settings, monkeypatch):
    run, factory = make_run(authenticated, tmp_path, graph=repair_graph())
    result, _ = execute(run, factory, settings, monkeypatch, [verdict("inconclusive")])
    assert result.final_state == "completed"
    with factory() as session:
        nodes = [s.node_id for s in session.scalars(select(StepExecution))]
        assert nodes.count("impl") == 1 and "unknown" in nodes


@pytest.mark.parametrize(
    "limits,loop_limit,expected,calls",
    [
        ({"max_calls": 3}, 2, "max_calls", 3),
        ({}, 1, "loop:repair", 4),
        ({"max_backward_transitions": 1}, 5, "max_backward_transitions", 4),
        ({"max_node_visits": 2}, 2, "max_node_visits", 1),
    ],
)
def test_real_limits_stop_calls_and_do_not_reset_on_second_execute(
    authenticated, tmp_path, settings, monkeypatch, limits, loop_limit, expected, calls
):
    run, factory = make_run(
        authenticated,
        tmp_path,
        graph=repair_graph(max_iterations=loop_limit),
        overrides={"limit_overrides": limits},
    )
    result, job = execute(
        run, factory, settings, monkeypatch, [verdict("failed", 1), verdict("failed", 2)]
    )
    assert result.final_state == "waiting_input"
    assert result.waiting_reason.code == "limit_exceeded"
    assert result.waiting_reason.details["limit"] == expected
    runner = Runner(
        session_factory=factory,
        worker_id="review",
        generation=job.generation,
        data_dir=settings.data_dir,
        secret_store=None,
    )
    assert runner.execute(run["id"]).final_state == "waiting_input"
    with factory() as session:
        assert len(list(session.scalars(select(StepAttempt)))) == calls
        assert (
            json.loads(
                session.get(
                    __import__("agents_ide.persistence.models", fromlist=["Run"]).Run, run["id"]
                ).runtime_json
            )["external_calls"]
            == calls
        )
    assert queue.claim_next_job(factory, worker_id="later", lease_seconds=30) is None


@pytest.mark.parametrize(
    "text", ["not JSON", '{"verdict": "passed"}', '{"verdict": [], "feedback": "x"}']
)
def test_output_schema_is_validated_by_server(authenticated, tmp_path, settings, monkeypatch, text):
    run, factory = make_run(authenticated, tmp_path, graph=repair_graph())
    result, _ = execute(
        run,
        factory,
        settings,
        monkeypatch,
        [
            FakeResponse(
                "check",
                1,
                raw_text=text,
                validated_result={"verdict": "passed", "feedback": "spoofed"},
                decision="passed",
            )
        ],
    )
    assert (
        result.final_state == "waiting_input"
        and result.waiting_reason.code == "invalid_response_format"
    )
    with factory() as session:
        check = session.scalar(select(StepExecution).where(StepExecution.node_id == "check"))
        assert check.decision is None and check.validated_result_json is None


def test_retry_stays_on_candidate_then_falls_forward(
    authenticated, tmp_path, settings, monkeypatch
):
    run, factory = make_run(authenticated, tmp_path, group=True)
    failures = [
        FakeResponse(
            "check",
            index,
            outcome=ExternalOutcome.RETRYABLE_FAILURE,
            retry_safety="safe",
            no_effect=True,
        )
        for index in (1, 2, 3)
    ]
    result, _ = execute(run, factory, settings, monkeypatch, failures)
    assert result.final_state == "completed"
    with factory() as session:
        attempts = list(session.scalars(select(StepAttempt).order_by(StepAttempt.attempt_index)))
        assert [json.loads(a.selection_json)["model_id"] for a in attempts] == [
            "alpha",
            "alpha",
            "alpha",
            "beta",
        ]
        assert len({a.operation_id for a in attempts}) == 4
        assert all(
            a.finished_at and a.request_artifact_id and a.result_artifact_id for a in attempts
        )
        from agents_ide.persistence.models import RunEvent

        retries = list(
            session.scalars(select(RunEvent).where(RunEvent.type == "attempt.retry_scheduled"))
        )
        assert len(retries) == 2
        # SQLite/event persistence can outlast a short backoff under load.
        # The behavioral contract is that the next attempt never starts early.
        for event in retries:
            payload = json.loads(event.payload_json)
            assert attempts[payload["retry_index"]].started_at >= payload["retry_at"]


def test_disabled_candidate_does_not_consume_call_budget(
    authenticated, tmp_path, settings, monkeypatch
):
    run, factory = make_run(
        authenticated,
        tmp_path,
        group=True,
        disabled_first=True,
        overrides={"limit_overrides": {"max_calls": 1}},
    )
    result, _ = execute(run, factory, settings, monkeypatch)
    assert result.final_state == "completed"
    with factory() as session:
        attempts = list(session.scalars(select(StepAttempt)))
        assert len(attempts) == 1 and json.loads(attempts[0].selection_json)["model_id"] == "beta"


def test_group_exhaustion_is_durable_and_diagnoses_every_candidate(
    authenticated, tmp_path, settings, monkeypatch
):
    run, factory = make_run(authenticated, tmp_path, group=True)
    responses = [
        FakeResponse(
            "check", index, outcome=ExternalOutcome.UNAVAILABLE, retry_safety="safe", no_effect=True
        )
        for index in (1, 2)
    ]
    result, _ = execute(run, factory, settings, monkeypatch, responses)
    assert (
        result.final_state == "waiting_input"
        and result.waiting_reason.code == "model_group_exhausted"
    )
    assert [c["model_id"] for c in result.waiting_reason.details["candidates"]] == ["alpha", "beta"]
    assert queue.claim_next_job(factory, worker_id="again", lease_seconds=30) is None


def test_ordinary_run_never_silently_uses_fake(authenticated, tmp_path, settings):
    from agents_ide.worker.main import dispatch_once

    run, factory = make_run(authenticated, tmp_path, execution_mode="real")
    assert dispatch_once(settings, "review")
    with factory() as session:
        from agents_ide.persistence.models import Run

        row = session.get(Run, run["id"])
        assert row.state == "waiting_input"
        # Stage 7: a real LLM call reaches the provider instead of silently
        # simulating. The loopback connection is unreachable, so the run waits
        # for the model to become available; no attempt claims success.
        assert json.loads(row.waiting_reason_json)["code"] == "model_unavailable"
        attempts = list(session.scalars(select(StepAttempt)))
        assert attempts
        assert all(a.status != "succeeded" for a in attempts)


def test_scenario_edits_are_confined_and_snapshot_is_immutable(authenticated, tmp_path, settings):
    from agents_ide.worker.main import dispatch_once

    run, factory = make_run(
        authenticated,
        tmp_path,
        fake_scenario={
            "responses": [{"node_id": "check", "files": {"src/fake.txt": "simulated content"}}]
        },
    )
    assert dispatch_once(settings, "review")
    assert (
        settings.data_dir / "simulated" / run["id"] / "src/fake.txt"
    ).read_text() == "simulated content"
    assert not (tmp_path / "workspace/src/fake.txt").exists()
    client, headers = authenticated
    after = client.get(f"/api/runs/{run['id']}", headers=headers).json()
    assert after["state"] == "completed" and after["snapshot_hash"] == run["snapshot_hash"]


def test_stale_lease_fences_result_and_new_calls(authenticated, tmp_path, settings, monkeypatch):
    from agents_ide.adapters.base import LLMResult
    from agents_ide.persistence.models import Run
    from agents_ide.worker.main import dispatch_once

    run, factory = make_run(authenticated, tmp_path)
    called = []

    def steal(_adapter, _request):
        called.append(True)
        with factory() as session:
            job = session.scalar(select(QueueJob))
            job.lease_expires_at = 1
            session.commit()
        return LLMResult(ExternalOutcome.SUCCEEDED, "{}", {}, "passed")

    monkeypatch.setattr("agents_ide.engine.runner.call_adapter", steal)
    with pytest.raises(AppError, match="Владение"):
        dispatch_once(settings, "review")
    recovery = queue.claim_next_job(factory, worker_id="new", lease_seconds=30)
    assert recovery is not None
    with factory() as session:
        assert session.get(Run, run["id"]).state == "recovering"
        assert session.scalar(select(StepAttempt)).status == "running"
    # Stage 5 may acquire a recovery lease, but it cannot dispatch while the
    # previous owner/outcome remains unconfirmed.
    runner = Runner(
        session_factory=factory,
        worker_id="new",
        generation=recovery.generation,
        data_dir=settings.data_dir,
        secret_store=None,
    )
    assert runner.execute(run["id"]).final_state == "waiting_input"
    with factory() as session:
        assert session.scalar(select(StepAttempt)).status == "unknown"
    assert called == [True]


def test_claim_skips_reserved_workspace_and_limits_two_active_runs(authenticated, tmp_path):
    import subprocess

    for suffix in ("1", "2", "3"):
        workspace = tmp_path / f"workspace{suffix}"
        workspace.mkdir()
        subprocess.run(["git", "init", str(workspace)], check=True, capture_output=True)
    first, factory = make_run(authenticated, tmp_path, suffix="1")
    second, _ = make_run(authenticated, tmp_path, suffix="2")
    third, _ = make_run(authenticated, tmp_path, suffix="3")
    assert queue.claim_next_job(factory, worker_id="a", lease_seconds=30).run_id == first["id"]
    assert queue.claim_next_job(factory, worker_id="b", lease_seconds=30).run_id == second["id"]
    assert queue.claim_next_job(factory, worker_id="c", lease_seconds=30) is None
    assert third["state"] == "queued"


def test_large_utf8_events_use_artifact_and_unique_sequence(authenticated, tmp_path):
    from agents_ide.engine.events import MAX_PAYLOAD_BYTES, append_event
    from agents_ide.persistence.models import RunEvent
    from agents_ide.services.transactions import begin_write

    run, factory = make_run(authenticated, tmp_path)

    def write(_):
        with factory() as session:
            begin_write(session)
            for _ in range(5):
                append_event(session, run["id"], "attempt.progress", {"text": "😀" * 20000})
            session.commit()

    with ThreadPoolExecutor(2) as pool:
        list(pool.map(write, (1, 2)))
    with factory() as session:
        rows = list(session.scalars(select(RunEvent).order_by(RunEvent.sequence)))
        assert [r.sequence for r in rows] == list(range(1, 12))
        assert all(len(r.payload_json.encode("utf-8")) <= MAX_PAYLOAD_BYTES for r in rows)
        assert all("artifact_id" in json.loads(r.payload_json) for r in rows[1:])


def test_sse_last_event_id_drains_more_than_one_page(
    authenticated, tmp_path, settings, monkeypatch
):
    from agents_ide.engine.events import append_event
    from agents_ide.services.transactions import begin_write

    run, factory = make_run(authenticated, tmp_path)
    with factory() as session:
        begin_write(session)
        for index in range(450):
            append_event(session, run["id"], "attempt.progress", {"index": index})
        session.commit()
    execute(run, factory, settings, monkeypatch)
    client, headers = authenticated
    response = client.get(
        f"/api/runs/{run['id']}/stream?after=1", headers={**headers, "Last-Event-ID": "10"}
    )
    ids = [int(line[4:]) for line in response.text.splitlines() if line.startswith("id: ")]
    latest = client.get(f"/api/runs/{run['id']}/snapshot", headers=headers).json()["last_sequence"]
    assert ids == list(range(11, latest + 1))
    assert "stream.closed" in response.text


def test_retention_reset_and_replay_pagination(authenticated, tmp_path):
    from sqlalchemy import delete

    from agents_ide.engine.events import append_event
    from agents_ide.persistence.models import RunEvent
    from agents_ide.services.transactions import begin_write

    run, factory = make_run(authenticated, tmp_path)
    with factory() as session:
        begin_write(session)
        for index in range(10):
            append_event(session, run["id"], "attempt.progress", {"index": index})
        session.execute(delete(RunEvent).where(RunEvent.sequence < 5))
        session.commit()
    client, headers = authenticated
    response = client.get(f"/api/runs/{run['id']}/stream", headers=headers)
    assert "stream.reset_required" in response.text and "id: " not in response.text
    replay = client.get(f"/api/runs/{run['id']}/events/replay?limit=2", headers=headers).json()
    assert [e["sequence"] for e in replay["events"]] == [5, 6]
    assert replay["has_more"] and not replay["reset_required"]


def test_slow_subscriber_is_bounded():
    from agents_ide.engine.events_stream import MAX_BUFFER_BYTES, EventBatch, Subscription

    subscription = Subscription()
    for index in range(20):
        subscription.push(EventBatch([{"sequence": index, "text": "x" * 100000}], index))
    assert subscription.overflow and subscription.buffered_bytes <= MAX_BUFFER_BYTES
    assert subscription.queue.qsize() == 1 and subscription.queue.get_nowait().reset_required


def test_confirmed_terminal_failure_does_not_try_second_provider(
    authenticated, tmp_path, settings, monkeypatch
):
    run, factory = make_run(authenticated, tmp_path, group=True)
    result, _ = execute(
        run,
        factory,
        settings,
        monkeypatch,
        [
            FakeResponse(
                "check",
                1,
                outcome=ExternalOutcome.CONFIRMED_FAILURE,
                no_effect=True,
                retry_safety="safe",
                error_code="fatal_execution_error",
            )
        ],
    )
    assert result.final_state == "failed"
    with factory() as session:
        assert len(list(session.scalars(select(StepAttempt)))) == 1


def test_snapshot_candidate_availability_is_rechecked_before_dispatch(
    authenticated, tmp_path, settings, monkeypatch
):
    from agents_ide.persistence.models import ProviderConnection

    run, factory = make_run(authenticated, tmp_path, group=True)
    with factory() as session:
        first = session.scalar(select(ProviderConnection).where(ProviderConnection.name == "c"))
        first.archived_at = 1
        session.commit()
    result, _ = execute(run, factory, settings, monkeypatch)
    assert result.final_state == "completed"
    with factory() as session:
        assert json.loads(session.scalar(select(StepAttempt)).selection_json)["model_id"] == "beta"


def test_simulation_hash_matches_preflight_and_changes_with_scenario(authenticated, tmp_path):
    run, _ = make_run(
        authenticated,
        tmp_path,
        fake_scenario={"responses": [{"node_id": "check", "raw_text": "first"}]},
    )
    client, headers = authenticated
    body = {
        "execution_mode": "simulated",
        "fake_scenario": {"responses": [{"node_id": "check", "raw_text": "first"}]},
    }
    preview = client.post(
        f"/api/bindings/{run['binding_id']}/preflight", headers=headers, json=body
    ).json()
    assert preview["execution_hash"] == run["execution_hash"]
    assert preview["dispatch_ready"] and preview["permissions"]["network"] is False
    body["fake_scenario"]["responses"][0]["raw_text"] = "second"
    other = client.post(
        f"/api/bindings/{run['binding_id']}/preflight", headers=headers, json=body
    ).json()
    assert other["execution_hash"] != preview["execution_hash"]


@pytest.mark.parametrize("path", ["../outside", "C:/outside", r"\\server\share\outside"])
def test_scenario_rejects_escaping_paths(path):
    from pydantic import ValidationError

    from agents_ide.adapters.fake import FakeScenarioSpec

    with pytest.raises(ValidationError):
        FakeScenarioSpec.model_validate({"responses": [{"node_id": "check", "files": {path: "x"}}]})


def test_open_sse_closes_after_session_revocation(authenticated, tmp_path):
    import time

    from agents_ide.security.auth import COOKIE_NAME

    run, _ = make_run(authenticated, tmp_path)
    client, headers = authenticated
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(client.get, f"/api/runs/{run['id']}/stream", headers=headers)
        deadline = time.monotonic() + 5
        while not client.app.state.run_streams.subscribers and time.monotonic() < deadline:
            time.sleep(0.01)
        assert client.app.state.run_streams.subscribers
        auth_session = client.app.state.auth.authenticate(client.cookies.get(COOKIE_NAME))
        client.app.state.auth.revoke(auth_session)
        response = future.result(timeout=5)
    assert "event: auth.expired" in response.text
    assert client.app.state.run_streams.subscribers == {}


def test_cancel_before_retry_prevents_another_external_call(
    authenticated, tmp_path, settings, monkeypatch
):
    from agents_ide.adapters.base import AdapterError, LLMResult
    from agents_ide.worker.main import dispatch_once

    run, factory = make_run(authenticated, tmp_path, group=True)
    client, headers = authenticated

    def fail_and_cancel(_adapter, _request):
        current = client.get(f"/api/runs/{run['id']}", headers=headers).json()
        response = client.post(
            f"/api/runs/{run['id']}/commands",
            headers=headers,
            json={
                "command_id": "cancel-now",
                "command_type": "cancel",
                "expected_state_version": current["state_version"],
            },
        )
        assert response.status_code == 200, response.text
        return LLMResult(
            ExternalOutcome.RETRYABLE_FAILURE,
            "",
            None,
            None,
            error=AdapterError("temporary", "temporary", "safe"),
            no_effect=True,
        )

    monkeypatch.setattr("agents_ide.engine.runner.call_adapter", fail_and_cancel)
    assert dispatch_once(settings, "review")
    assert client.get(f"/api/runs/{run['id']}", headers=headers).json()["state"] == "cancelled"
    with factory() as session:
        assert len(list(session.scalars(select(StepAttempt)))) == 1


def test_timeout_does_not_apply_late_simulated_edits(authenticated, tmp_path, settings):
    from agents_ide.worker.main import dispatch_once

    graph = repair_graph()
    graph["nodes"][1].update({"timeout_seconds": 1, "max_retries": 0})
    run, factory = make_run(
        authenticated,
        tmp_path,
        graph=graph,
        fake_scenario={
            "responses": [{"node_id": "impl", "delay_seconds": 2, "files": {"late.txt": "late"}}]
        },
    )
    assert dispatch_once(settings, "review")
    with factory() as session:
        attempts = list(session.scalars(select(StepAttempt)))
        assert len(attempts) == 1 and attempts[0].error_code == "timeout"
    assert not (settings.data_dir / "simulated" / run["id"] / "late.txt").exists()


def test_legacy_visit_without_checkpoint_is_never_replayed(authenticated, tmp_path, settings):
    from agents_ide.domain.common import new_id
    from agents_ide.worker.main import dispatch_once

    run, factory = make_run(authenticated, tmp_path)
    with factory() as session:
        session.add(
            StepExecution(
                id=new_id(),
                run_id=run["id"],
                node_id="check",
                visit_index=1,
                cycle_id=1,
                status="running",
            )
        )
        session.commit()
    assert dispatch_once(settings, "review")
    client, headers = authenticated
    current = client.get(f"/api/runs/{run['id']}", headers=headers).json()
    assert (
        current["state"] == "waiting_input"
        and current["waiting_reason"]["code"] == "unknown_external_result"
    )
    with factory() as session:
        assert list(session.scalars(select(StepAttempt))) == []


def test_simulated_text_deltas_are_persisted_before_result(authenticated, tmp_path, settings):
    from agents_ide.persistence.models import RunEvent
    from agents_ide.worker.main import dispatch_once

    run, factory = make_run(
        authenticated,
        tmp_path,
        fake_scenario={"responses": [{"node_id": "check", "deltas": ["first", "second"]}]},
    )
    assert dispatch_once(settings, "review")
    with factory() as session:
        rows = list(session.scalars(select(RunEvent).order_by(RunEvent.sequence)))
        deltas = [e for e in rows if e.type == "attempt.text_delta"]
        assert [json.loads(e.payload_json)["text"] for e in deltas] == ["first", "second"]
        finished = next(e for e in rows if e.type == "attempt.finished")
        assert all(
            e.sequence < finished.sequence and e.step_attempt_id == finished.step_attempt_id
            for e in deltas
        )
        assert all(json.loads(e.payload_json)["source"] == "simulated" for e in deltas)
