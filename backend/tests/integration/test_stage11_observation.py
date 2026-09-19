"""Observation stays durable, bounded and separate from execution/control."""

import asyncio
import json

from sqlalchemy import delete, select
from test_stage4_review import execute, make_run, repair_graph, verdict

from agents_ide.adapters.fake import FakeAgentAdapter
from agents_ide.engine.artifacts import ArtifactPayload, record_artifact
from agents_ide.engine.events import append_event
from agents_ide.engine.events_stream import MAX_BUFFER_BYTES, StreamHub
from agents_ide.engine.runner import Runner
from agents_ide.persistence.models import RunEvent, StepExecution


def test_loop_resets_only_its_body_and_preserves_history(
    authenticated, tmp_path, settings, monkeypatch
):
    client, _ = authenticated
    run, factory = make_run(authenticated, tmp_path, graph=repair_graph())
    url = f"/api/runs/{run['id']}"
    finish_visit = Runner._finish_visit
    call_agent = FakeAgentAdapter.run
    resets, active_cycles = [], []

    def observe_return(self, node, visit, result):
        outcome = finish_visit(self, node, visit, result)
        if self.runtime.get("last_transition", {}).get("backward"):
            observation = client.get(url + "/snapshot").json()["observation"]
            nodes = {node["id"]: node for node in observation["nodes"]}
            assert observation["current_node_id"] == "impl"
            assert observation["current_execution_id"] is None
            assert nodes["s"]["status"] == "succeeded"
            for node_id in ("impl", "check", "route"):
                assert nodes[node_id]["status"] == "pending"
                assert nodes[node_id]["execution_id"] is None
                assert nodes[node_id]["attempt_count"] == 0
                assert nodes[node_id]["result_ref"] is None
            # Snapshot state survives reopening and event retention.
            with factory() as session:
                session.execute(delete(RunEvent).where(RunEvent.run_id == run["id"]))
                session.commit()
            assert client.get(url + "/snapshot").json()["observation"] == observation
            resets.append(observation["cycle_id"])
        return outcome

    def observe_active(self, request):
        observation = client.get(url + "/snapshot").json()["observation"]
        nodes = {node["id"]: node for node in observation["nodes"]}
        assert nodes["impl"]["status"] == "running"
        assert nodes["check"]["status"] == "pending"
        assert nodes["route"]["status"] == "pending"
        assert nodes["s"]["status"] == "succeeded"
        active_cycles.append(observation["cycle_id"])
        return call_agent(self, request)

    monkeypatch.setattr(Runner, "_finish_visit", observe_return)
    monkeypatch.setattr(FakeAgentAdapter, "run", observe_active)
    result, _ = execute(
        run,
        factory,
        settings,
        monkeypatch,
        [verdict("failed", 1), verdict("failed", 2), verdict("passed", 3)],
    )
    assert result.final_state == "completed"
    assert resets == [2, 3]
    assert active_cycles == [1, 2, 3]
    nodes = {
        node["id"]: node for node in client.get(url + "/snapshot").json()["observation"]["nodes"]
    }
    assert all(
        nodes[node]["status"] == "succeeded" for node in ("s", "impl", "check", "route", "e")
    )
    with factory() as session:
        executions = list(
            session.scalars(
                select(StepExecution).where(
                    StepExecution.run_id == run["id"], StepExecution.node_id == "check"
                )
            )
        )
        assert len(executions) == 3
        assert all(row.status == "succeeded" and row.validated_result_json for row in executions)


def test_snapshot_and_history_survive_reopen_and_retention(
    authenticated, tmp_path, settings, monkeypatch
):
    client, _ = authenticated
    run, factory = make_run(authenticated, tmp_path, group=True, disabled_first=True)
    assert execute(run, factory, settings, monkeypatch)[0].final_state.value == "completed"
    url = f"/api/runs/{run['id']}"
    snapshot = client.get(url + "/snapshot").json()
    observation = snapshot["observation"]
    assert observation["current_node_id"] == "e"
    check = next(node for node in observation["nodes"] if node["id"] == "check")
    assert check["status"] == "succeeded" and check["attempt_count"] == 1
    assert check["model_id"] == "beta" and check["resource_id"]
    assert check["finished_at"] >= check["started_at"]
    assert check["result_ref"]
    assert observation["last_transition"]["source_node_id"] == "check"
    first = client.get(url + "/history?limit=3").json()
    assert first["has_more"] and len(first["events"]) == 3
    found = first["events"][:]
    page = first
    while page["has_more"]:
        page = client.get(url + f"/history?limit=3&before={page['next_before']}").json()
        found.extend(page["events"])
    assert [e["sequence"] for e in found] == list(range(snapshot["last_sequence"], 0, -1))
    models = client.get(url + "/history?category=models&node_id=check").json()["events"]
    assert any(e["type"] == "model_group.candidate_skipped" for e in models)
    assert all(e["node_id"] == "check" for e in models)
    # A stale cursor resets, but projections do not depend on retained SSE events.
    with factory() as session:
        session.execute(
            delete(RunEvent).where(
                RunEvent.run_id == run["id"], RunEvent.sequence < snapshot["last_sequence"]
            )
        )
        session.commit()
    assert client.get(url + "/events?after=1").json()["reset_required"]
    restored = client.get(url + "/snapshot").json()
    assert restored["observation"] == observation
    assert restored["selection"] == snapshot["selection"]
    assert client.get(url + "/history").json()["min_retained_sequence"] == snapshot["last_sequence"]
    assert client.get(url + "/commands").json() == []


def test_history_filters_and_keyset_pagination_with_concurrent_append(authenticated, tmp_path):
    client, _ = authenticated
    run, factory = make_run(authenticated, tmp_path)
    url = f"/api/runs/{run['id']}"
    with factory() as session:
        for i in range(40):
            append_event(
                session, run["id"], "agent.message_delta", {"text": str(i)}, node_id="check"
            )
        append_event(session, run["id"], "agent.tool_call", {"name": "read"}, node_id="tool")
        session.commit()
    page = client.get(url + "/history?category=messages&node_id=check&limit=20").json()
    with factory() as session:
        append_event(session, run["id"], "agent.message_delta", {"text": "new"}, node_id="check")
        session.commit()
    next_page = client.get(
        url + f"/history?category=messages&limit=20&before={page['next_before']}"
    ).json()
    assert len(next_page["events"]) == 20 and not next_page["has_more"]
    assert not set(e["sequence"] for e in page["events"]) & set(
        e["sequence"] for e in next_page["events"]
    )
    assert (
        client.get(url + "/history?category=tools").json()["events"][0]["payload"]["name"] == "read"
    )
    assert client.get(url + "/history?execution_id=missing").json()["events"] == []
    for query in ("limit=201", "before=0", "category=invalid"):
        assert client.get(url + "/history?" + query).status_code == 422
    assert client.get("/api/runs/missing/history").status_code == 404


def test_history_byte_bound_and_artifact_chunks(authenticated, tmp_path):
    client, _ = authenticated
    run, factory = make_run(authenticated, tmp_path)
    url = f"/api/runs/{run['id']}"
    with factory() as session:
        for _ in range(70):
            append_event(session, run["id"], "agent.message_delta", {"text": "x" * 15000})
        artifact = record_artifact(
            session,
            run["id"],
            ArtifactPayload(
                "diff", body={"diff": "-old\n+new\n" * 6000, "api_key": "private-value"}
            ),
        )
        artifact_id = artifact.id
        session.commit()
    page = client.get(url + "/history").json()
    assert len(page["events"]) < 70 and page["has_more"]
    assert len(json.dumps(page["events"]).encode()) < MAX_BUFFER_BYTES // 2
    result = client.get(url + f"/artifacts/{artifact_id}/content").json()
    assert len(result["text"]) == 16000 and result["total_chars"] > 16000
    assert "-old\n+new" in result["text"]
    assert result["artifact"]["body"] is None and result["artifact"]["redaction"]
    parts = [result["text"]]
    for offset in range(16000, result["total_chars"], 16000):
        parts.append(
            client.get(url + f"/artifacts/{artifact_id}/content?offset={offset}").json()["text"]
        )
    assert "private-value" not in "".join(parts) and "[REDACTED]" in "".join(parts)
    assert client.get(f"/api/runs/wrong/artifacts/{artifact_id}/content").status_code == 404
    assert client.get(url + "/artifacts?limit=1&offset=1").json() == []


def test_slow_subscriber_is_bounded_without_affecting_other_observer(authenticated, tmp_path):
    _, factory = make_run(authenticated, tmp_path)
    with factory() as session:
        run_id = session.scalar(select(RunEvent.run_id))

    async def scenario():
        hub = StreamHub(factory)
        slow = await hub.subscribe(run_id)
        fast = await hub.subscribe(run_id)
        try:
            with factory() as session:
                for _ in range(100):
                    append_event(session, run_id, "agent.message_delta", {"text": "x" * 15000})
                session.commit()
            received = []
            while len(received) < 100:
                batch = await asyncio.wait_for(fast.get(), 10)
                assert not batch.reset_required
                received.extend(batch.events)
            assert len({e["sequence"] for e in received}) == 100
            assert slow.overflow and slow.buffered_bytes == 0 and slow.queue.qsize() == 1
            assert (await slow.get()).reset_required
        finally:
            await hub.close()

    asyncio.run(scenario())
