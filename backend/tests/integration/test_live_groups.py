"""Existing runs follow external group edits at model request boundaries."""

import json

from sqlalchemy import select
from test_loop_controls import command
from test_stage4_review import execute, make_run

from agents_ide.adapters.base import AdapterError, ExternalOutcome, LLMResult
from agents_ide.adapters.fake import FakeLLMAdapter
from agents_ide.engine.runner import Runner
from agents_ide.persistence.models import Run, StepAttempt


def group_for(authenticated, run, factory):
    client, headers = authenticated
    with factory() as session:
        snapshot = json.loads(session.get(Run, run["id"]).snapshot_json)
    group_id = snapshot["dependencies"]["nodes"]["check"]["model_group_id"]
    return client.get(f"/api/model_groups/{group_id}", headers=headers).json()


def replace_members(authenticated, group, members):
    client, headers = authenticated
    response = client.put(
        f"/api/model_groups/{group['id']}/llm/members",
        headers=headers,
        json={"expected_revision": group["revision"], "members": members},
    )
    assert response.status_code == 200, response.text
    return response.json()


def member(source, model_id=None):
    return {
        "id": source["id"],
        "provider_connection_id": source["provider_connection_id"],
        "model_id": model_id or source["model_id"],
        "params": source["params"],
        "enabled": source["enabled"],
    }


def unavailable():
    return LLMResult(
        ExternalOutcome.UNAVAILABLE,
        "",
        None,
        None,
        error=AdapterError("unavailable", "unavailable", "safe"),
        no_effect=True,
    )


def test_queued_run_uses_new_members_order_parameters_and_keeps_original_audit(
    authenticated, settings, tmp_path, monkeypatch
):
    run, factory = make_run(authenticated, tmp_path, group=True)
    group = group_for(authenticated, run, factory)
    with factory() as session:
        original_snapshot = session.get(Run, run["id"]).snapshot_json
    updated = replace_members(
        authenticated,
        group,
        [
            {**member(group["members"][1], "gamma"), "params": {"temperature": 0.25}},
            {**member(group["members"][0]), "enabled": False},
        ],
    )
    client, headers = authenticated
    summary = client.get(f"/api/runs/{run['id']}/snapshot", headers=headers).json()["selection"]
    assert not summary["group_changes_apply_only_to_new_runs"]
    assert [m["model_id"] for m in summary["nodes"][0]["candidates"]] == ["gamma", "alpha"]
    seen = []
    original = FakeLLMAdapter.run

    def record(self, request):
        seen.append((request.model_id, request.params))
        return original(self, request)

    monkeypatch.setattr(FakeLLMAdapter, "run", record)
    result, _ = execute(run, factory, settings, monkeypatch)
    assert result.final_state == "completed", result
    assert seen == [("gamma", {"temperature": 0.25})]
    with factory() as session:
        assert session.get(Run, run["id"]).snapshot_json == original_snapshot
        actual = json.loads(session.scalar(select(StepAttempt)).selection_json)
        assert actual["model_id"] == "gamma" and actual["group_revision"] == updated["revision"]


def test_group_change_during_request_applies_to_fallback_without_replaying_current_model(
    authenticated, settings, tmp_path, monkeypatch
):
    run, factory = make_run(authenticated, tmp_path, group=True)
    group = group_for(authenticated, run, factory)
    seen = []
    original = FakeLLMAdapter.run

    def record(self, request):
        seen.append(request.model_id)
        if request.model_id == "alpha":
            assert len(seen) == 1  # the completed rejection must not be replayed
            replace_members(
                authenticated,
                group,
                [member(group["members"][0]), member(group["members"][1], "gamma")],
            )
            return unavailable()
        return original(self, request)

    monkeypatch.setattr(FakeLLMAdapter, "run", record)
    result, _ = execute(run, factory, settings, monkeypatch)
    assert result.final_state == "completed", result
    assert seen == ["alpha", "gamma"]
    with factory() as session:
        attempts = [
            json.loads(a.selection_json)["model_id"]
            for a in session.scalars(select(StepAttempt).order_by(StepAttempt.started_at))
        ]
        assert attempts == seen


def test_exhausted_run_resumes_with_replaced_external_group(
    authenticated, settings, tmp_path, monkeypatch
):
    run, factory = make_run(authenticated, tmp_path, group=True)
    group = group_for(authenticated, run, factory)
    original = FakeLLMAdapter.run
    monkeypatch.setattr(FakeLLMAdapter, "run", lambda *_: unavailable())
    result, _ = execute(run, factory, settings, monkeypatch)
    assert result.waiting_reason.code == "model_group_exhausted"
    replace_members(authenticated, group, [member(group["members"][1], "gamma")])
    monkeypatch.setattr(FakeLLMAdapter, "run", original)
    assert command(authenticated, run, "resume").status_code == 200
    result, _ = execute(run, factory, settings, monkeypatch)
    assert result.final_state == "completed", result
    with factory() as session:
        assert [
            json.loads(a.selection_json)["model_id"]
            for a in session.scalars(select(StepAttempt).order_by(StepAttempt.started_at))
        ] == ["alpha", "beta", "gamma"]


def test_archived_group_never_dispatches_saved_members(
    authenticated, settings, tmp_path, monkeypatch
):
    run, factory = make_run(authenticated, tmp_path, group=True)
    group = group_for(authenticated, run, factory)
    client, headers = authenticated
    archived = client.post(
        f"/api/model_groups/{group['id']}/archive?expected_revision={group['revision']}",
        headers=headers,
    )
    assert archived.status_code == 200
    result, _ = execute(run, factory, settings, monkeypatch)
    assert result.final_state == "waiting_input"
    assert result.waiting_reason.code == "model_unavailable"
    with factory() as session:
        assert list(session.scalars(select(StepAttempt))) == []


def test_group_edit_before_dispatch_intent_reselects_without_calling_old_model(
    authenticated, settings, tmp_path, monkeypatch
):
    run, factory = make_run(authenticated, tmp_path, group=True)
    group = group_for(authenticated, run, factory)
    original_evidence = Runner._evidence_package
    changed = False

    def evidence(self, session):
        nonlocal changed
        if not changed:
            changed = True
            replace_members(authenticated, group, [member(group["members"][1], "gamma")])
        return original_evidence(self, session)

    monkeypatch.setattr(Runner, "_evidence_package", evidence)
    result, _ = execute(run, factory, settings, monkeypatch)
    assert result.final_state == "completed", result
    with factory() as session:
        assert [
            json.loads(a.selection_json)["model_id"] for a in session.scalars(select(StepAttempt))
        ] == ["gamma"]
