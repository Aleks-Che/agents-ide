"""Live loop budgets retain completed work and survive worker checkpoints."""

import json
from uuid import uuid4

import pytest
from sqlalchemy import select
from test_stage4_review import execute, make_run, repair_graph, verdict

from agents_ide.adapters.fake import FakeLLMAdapter
from agents_ide.persistence.models import Run, RunPolicyRevision, StepAttempt


def command(authenticated, run, kind, payload=None, command_id=None):
    client, headers = authenticated
    current = client.get(f"/api/runs/{run['id']}", headers=headers).json()
    return client.post(
        f"/api/runs/{run['id']}/commands",
        headers=headers,
        json={
            "command_id": command_id or uuid4().hex,
            "command_type": kind,
            "expected_state_version": current["state_version"],
            "payload": payload or {},
        },
    )


def observed(authenticated, run):
    client, headers = authenticated
    return client.get(f"/api/runs/{run['id']}/snapshot", headers=headers).json()["observation"][
        "loops"
    ][0]


def test_loop_exhaustion_add_and_resume_does_not_repeat_finished_attempt(
    authenticated, settings, tmp_path, monkeypatch
):
    assert not settings.enforce_execution_limits
    run, factory = make_run(authenticated, tmp_path, graph=repair_graph(max_iterations=1))
    result, _ = execute(
        run, factory, settings, monkeypatch, [verdict("failed", 1), verdict("failed", 2)]
    )
    assert result.final_state == "waiting_input"
    assert result.waiting_reason.details["limit"] == "loop:repair"
    loop = observed(authenticated, run)
    assert (loop["completed"], loop["remaining"], loop["waiting"]) == (1, 0, True)
    with factory() as session:
        snapshot = session.get(Run, run["id"]).snapshot_json
        attempts = {row.id for row in session.scalars(select(StepAttempt))}
    assert command(authenticated, run, "resume").status_code == 409
    payload = {"loop_key": "repair", "delta": 2}
    response = command(authenticated, run, "adjust_loop", payload, "add-two")
    assert response.status_code == 200, response.text
    client, headers = authenticated
    duplicate = client.post(
        f"/api/runs/{run['id']}/commands",
        headers=headers,
        json=json.loads(response.request.content),
    )
    assert duplicate.json() == response.json()
    loop = observed(authenticated, run)
    assert (loop["completed"], loop["remaining"], loop["max_iterations"]) == (1, 2, 3)
    assert command(authenticated, run, "resume").status_code == 200
    result, _ = execute(run, factory, settings, monkeypatch, [verdict("passed", 3)])
    assert result.final_state == "completed", result
    loop = observed(authenticated, run)
    assert (loop["completed"], loop["remaining"], loop["can_adjust"]) == (2, 1, False)
    assert command(authenticated, run, "adjust_loop", payload).status_code == 409
    with factory() as session:
        assert session.get(Run, run["id"]).snapshot_json == snapshot
        all_attempts = {row.id for row in session.scalars(select(StepAttempt))}
        assert attempts < all_attempts
        assert len(all_attempts - attempts) == 2  # implement + verify only
        assert len(list(session.scalars(select(RunPolicyRevision)))) == 1


@pytest.mark.parametrize("delta,expected", [(-2, 0), (1, 3)])
def test_adjust_while_model_runs_is_not_lost_by_worker_writes(
    authenticated, settings, tmp_path, monkeypatch, delta, expected
):
    run, factory = make_run(authenticated, tmp_path, graph=repair_graph(max_iterations=2))
    original = FakeLLMAdapter.run
    changed = False

    def edit_during_call(self, request):
        nonlocal changed
        if not changed:
            changed = True
            response = command(
                authenticated, run, "adjust_loop", {"loop_key": "repair", "delta": delta}
            )
            assert response.status_code == 200, response.text
            if request.emit_event:
                request.emit_event("attempt.text_delta", {"text": "progress after loop adjustment"})
        return original(self, request)

    monkeypatch.setattr(FakeLLMAdapter, "run", edit_during_call)
    result, _ = execute(
        run, factory, settings, monkeypatch, [verdict("failed", n) for n in range(1, 5)]
    )
    assert changed
    assert result.final_state == "waiting_input"
    loop = observed(authenticated, run)
    assert (loop["completed"], loop["remaining"], loop["max_iterations"]) == (expected, 0, expected)


def test_item_budgets_are_separate_and_old_scope_commands_are_rejected(authenticated, tmp_path):
    graph = repair_graph(max_iterations=5)
    next(e["loop"] for e in graph["edges"] if e.get("loop"))["scope"] = "item"
    run, factory = make_run(authenticated, tmp_path, graph=graph)
    with factory() as session:
        row = session.get(Run, run["id"])
        runtime = json.loads(row.runtime_json)
        runtime.update(
            work={"scope": "item:one"}, loop_counts={"repair:item:one": 2, "repair:item:two": 1}
        )
        row.runtime_json = json.dumps(runtime)
        session.commit()
    loop = observed(authenticated, run)
    assert (loop["completed"], loop["remaining"], loop["total_completed"]) == (2, 3, 3)
    assert (
        command(
            authenticated, run, "adjust_loop", {"loop_key": loop["key"], "delta": -1}
        ).status_code
        == 200
    )
    assert observed(authenticated, run)["remaining"] == 2
    with factory() as session:
        row = session.get(Run, run["id"])
        runtime = json.loads(row.runtime_json)
        runtime["work"]["scope"] = "item:two"
        row.runtime_json = json.dumps(runtime)
        session.commit()
    assert observed(authenticated, run)["remaining"] == 4
    assert (
        command(
            authenticated, run, "adjust_loop", {"loop_key": loop["key"], "delta": 1}
        ).status_code
        == 409
    )
    assert (
        command(
            authenticated, run, "adjust_loop", {"loop_key": "repair:item:two", "delta": -5}
        ).status_code
        == 422
    )
    assert observed(authenticated, run)["remaining"] == 4


@pytest.mark.parametrize("delta", [True, 0, 1.5, "1", -3])
def test_invalid_loop_changes_do_not_mutate_budget(authenticated, tmp_path, delta):
    run, _ = make_run(authenticated, tmp_path, graph=repair_graph(max_iterations=2))
    response = command(authenticated, run, "adjust_loop", {"loop_key": "repair", "delta": delta})
    assert response.status_code == 422
    assert observed(authenticated, run)["remaining"] == 2


def test_legacy_overrun_can_add_one_more_iteration(authenticated, tmp_path):
    run, factory = make_run(authenticated, tmp_path, graph=repair_graph(max_iterations=1))
    with factory() as session:
        row = session.get(Run, run["id"])
        runtime = json.loads(row.runtime_json)
        runtime["loop_counts"] = {"repair": 9}
        row.runtime_json = json.dumps(runtime)
        session.commit()
    assert (
        command(authenticated, run, "adjust_loop", {"loop_key": "repair", "delta": 1}).status_code
        == 200
    )
    loop = observed(authenticated, run)
    assert (loop["completed"], loop["remaining"], loop["max_iterations"]) == (9, 1, 10)
