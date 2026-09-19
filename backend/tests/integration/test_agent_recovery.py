"""Continue legacy unknown AgentTask results through explicit UI resolutions."""

import json
from dataclasses import replace

import pytest
import test_stage6a_review as opencode_review
from sqlalchemy import select
from test_stage6a_review import runner_for, seed

from agents_ide.adapters.base import AdapterError, ExternalOutcome
from agents_ide.adapters.opencode import OpenCodeAdapter
from agents_ide.persistence.models import (
    ArtifactManifest,
    HarnessProfile,
    Run,
    StepAttempt,
    StepExecution,
)

launch_fixture = opencode_review.launch_fixture


def legacy_wait(authenticated, settings, tmp_path, monkeypatch, model="recoverable-quota"):
    run, _ = seed(
        authenticated,
        tmp_path,
        roles=("reviewer",),
        group=True,
        first_group_model=model,
    )
    original = OpenCodeAdapter.run

    def old_adapter(self, request):
        result = original(self, request)
        if result.can_handoff:
            return replace(
                result,
                outcome=ExternalOutcome.UNKNOWN,
                can_handoff=False,
                raw_text="",
                error=AdapterError("provider_result_unknown", "provider_result_unknown"),
            )
        return result

    monkeypatch.setattr(OpenCodeAdapter, "run", old_adapter)
    result = runner_for(authenticated[0], settings).execute(run["id"])
    assert result.waiting_reason.code == "unknown_external_result"
    monkeypatch.setattr(OpenCodeAdapter, "run", original)
    return run


def command(authenticated, run, kind, payload=None, command_id=None):
    client, headers = authenticated
    current = client.get(f"/api/runs/{run['id']}", headers=headers).json()
    return client.post(
        f"/api/runs/{run['id']}/commands",
        headers=headers,
        json={
            "command_type": kind,
            "command_id": command_id or kind,
            "expected_state_version": current["state_version"],
            "payload": payload or {},
        },
    )


@pytest.mark.parametrize("action", ["continue_session", "next_candidate"])
def test_resolve_legacy_attempt_keeps_stage_files_and_native_conversation(
    authenticated, settings, tmp_path, launch_fixture, monkeypatch, action
):
    run = legacy_wait(authenticated, settings, tmp_path, monkeypatch)
    client, headers = authenticated
    current = client.get(f"/api/runs/{run['id']}", headers=headers).json()
    options = current["waiting_reason"]["resolution_schema"]["agent_recovery"]
    assert options["actions"] == ["continue_session", "next_candidate"]
    payload = {"agent_recovery": {"attempt_id": options["attempt_id"], "action": action}}
    response = command(authenticated, run, "resolve", payload)
    assert response.status_code == 200, response.text
    with client.app.state.session_factory() as db:
        saved = db.get(Run, run["id"])
        execution_id = saved.current_execution_id
        assert saved.state == "waiting_input"  # saving a decision never dispatches
        assert json.loads(saved.runtime_json)["pending_agent_recovery"] == {
            **payload["agent_recovery"],
            "execution_id": execution_id,
        }
        snapshot = json.loads(saved.snapshot_json)
        profile_id, pinned = next(iter(snapshot["dependencies"]["harness_profiles"].items()))
        assert db.get(StepAttempt, options["attempt_id"]).status == "unknown"
        artifact = db.scalar(
            select(ArtifactManifest).where(
                ArtifactManifest.run_id == run["id"],
                ArtifactManifest.schema_type == "resolution_data",
            )
        )
        assert json.loads(artifact.body_json)["agent_recovery"] == payload["agent_recovery"]
    # A real catalog probe bumps the profile version without changing execution
    # settings. It must not strand either continuation or model handoff.
    probe = client.post(f"/api/harness_profiles/{profile_id}/test", headers=headers)
    assert probe.status_code == 200, probe.text
    assert probe.json()["status"] == "ok"
    with client.app.state.session_factory() as db:
        profile = db.get(HarnessProfile, profile_id)
        assert profile.version > pinned["version"]
        assert json.loads(profile.settings_json) == pinned["settings"]
    assert command(authenticated, run, "resume").status_code == 200
    with client.app.state.session_factory() as db:
        assert "pending_agent_recovery" not in json.loads(db.get(Run, run["id"]).runtime_json)
    result = runner_for(client, settings).execute(run["id"])
    assert result.final_state == "completed", result
    rows = [json.loads(line) for line in launch_fixture[1].read_text().splitlines()]
    messages = [r for r in rows if r["path"].endswith("/message")]
    assert len(messages) == 2
    assert messages[0]["path"] == messages[1]["path"]  # full native history, same session
    # The probe itself can create a temporary health-check session.
    assert len({m["path"] for m in messages}) == 1
    assert messages[1]["body"]["model"]["modelID"] == (
        "recoverable-quota" if action == "continue_session" else "claude-sonnet-4-20250514"
    )
    assert "Continue" in messages[1]["body"]["parts"][0]["text"]
    if action == "next_candidate":
        context = json.loads(messages[1]["body"]["parts"][1]["text"].split("\n", 1)[1])
        assert context["agent_handoff"]["tool_calls"][0]["summary"] == "partial-work.txt"
    assert (tmp_path / "workspace" / "partial-work.txt").read_text() == "preserve this work"
    with client.app.state.session_factory() as db:
        attempts = list(
            db.scalars(
                select(StepAttempt)
                .where(StepAttempt.execution_id == execution_id)
                .order_by(StepAttempt.started_at)
            )
        )
        assert [a.status for a in attempts] == ["unknown", "succeeded"]
        assert (
            len(
                list(
                    db.scalars(
                        select(StepExecution).where(
                            StepExecution.run_id == run["id"], StepExecution.node_id == "a0"
                        )
                    )
                )
            )
            == 1
        )


def test_failed_continuation_before_dispatch_can_choose_another_model(
    authenticated, settings, tmp_path, launch_fixture, monkeypatch
):
    run = legacy_wait(authenticated, settings, tmp_path, monkeypatch)
    client, headers = authenticated
    with client.app.state.session_factory() as db:
        saved = db.get(Run, run["id"])
        attempt_id, execution_id = saved.current_attempt_id, saved.current_execution_id
    assert (
        command(
            authenticated,
            run,
            "resolve",
            {"agent_recovery": {"attempt_id": attempt_id, "action": "continue_session"}},
        ).status_code
        == 200
    )
    assert command(authenticated, run, "resume").status_code == 200
    runner = runner_for(client, settings)
    monkeypatch.setattr(runner, "_availability", lambda _: "resource_changed")
    result = runner.execute(run["id"])
    assert result.waiting_reason.code == "session_resume_unavailable"
    assert result.waiting_reason.details["reason"] == "resource_changed"
    current = client.get(f"/api/runs/{run['id']}", headers=headers).json()
    assert (
        current["waiting_reason"]["resolution_schema"]["agent_recovery"]["attempt_id"] == attempt_id
    )
    assert "pending_agent_recovery" not in current["runtime"]
    assert current["runtime"]["agent_recovery_attempts"][attempt_id] == "continue_session"
    assert current["waiting_reason"]["resolution_schema"]["agent_recovery"]["actions"] == [
        "continue_session",
        "next_candidate",
    ]
    assert (
        command(
            authenticated,
            run,
            "resolve",
            {"agent_recovery": {"attempt_id": attempt_id, "action": "next_candidate"}},
            "switch-after-failure",
        ).status_code
        == 200
    )
    assert command(authenticated, run, "resume", command_id="resume-next").status_code == 200
    result = runner_for(client, settings).execute(run["id"])
    assert result.final_state == "completed", result
    rows = [json.loads(line) for line in launch_fixture[1].read_text().splitlines()]
    messages = [r for r in rows if r["path"].endswith("/message")]
    assert len(messages) == 2
    assert messages[0]["path"] == messages[1]["path"]
    assert messages[1]["body"]["model"]["modelID"] == "claude-sonnet-4-20250514"
    with client.app.state.session_factory() as db:
        assert (
            len(
                list(
                    db.scalars(select(StepAttempt).where(StepAttempt.execution_id == execution_id))
                )
            )
            == 2
        )


@pytest.mark.parametrize("action", ["continue_session", "next_candidate"])
def test_missing_native_session_never_replays_original_task(
    authenticated, settings, tmp_path, launch_fixture, monkeypatch, action
):
    run = legacy_wait(authenticated, settings, tmp_path, monkeypatch)
    with authenticated[0].app.state.session_factory() as db:
        attempt_id = db.get(Run, run["id"]).current_attempt_id
    response = command(
        authenticated,
        run,
        "resolve",
        {
            "agent_recovery": {
                "attempt_id": attempt_id,
                "action": action,
            }
        },
    )
    assert response.status_code == 200, response.text
    (tmp_path / "sessions.json").write_text("{}")
    assert command(authenticated, run, "resume").status_code == 200
    result = runner_for(authenticated[0], settings).execute(run["id"])
    rows = [json.loads(line) for line in launch_fixture[1].read_text().splitlines()]
    messages = [r for r in rows if r["path"].endswith("/message")]
    if action == "continue_session":
        assert result.waiting_reason.code == "session_resume_unavailable"
        assert len(messages) == 1
        with authenticated[0].app.state.session_factory() as db:
            missing_attempt = db.get(Run, run["id"]).current_attempt_id
        response = command(
            authenticated,
            run,
            "resolve",
            {
                "agent_recovery": {
                    "attempt_id": missing_attempt,
                    "action": "next_candidate",
                }
            },
            "switch-after-missing",
        )
        assert response.status_code == 200, response.text
        assert command(authenticated, run, "resume", command_id="resume-next").status_code == 200
        result = runner_for(authenticated[0], settings).execute(run["id"])
        assert result.final_state == "completed", result
        rows = [json.loads(line) for line in launch_fixture[1].read_text().splitlines()]
        messages = [r for r in rows if r["path"].endswith("/message")]
        assert "Original task:\nforce_decision=passed" in messages[-1]["body"]["parts"][0]["text"]
        context = json.loads(messages[-1]["body"]["parts"][1]["text"].split("\n", 1)[1])
        assert context["agent_handoff"]["tool_calls"][0]["summary"] == "partial-work.txt"
    else:
        assert result.final_state == "completed", result
        assert len(messages) == 2
        assert messages[0]["path"] != messages[1]["path"]
        assert "Preserve completed work" in messages[1]["body"]["parts"][0]["text"]


def test_recovery_rejects_stale_attempt_and_active_owner(
    authenticated, settings, tmp_path, launch_fixture, monkeypatch
):
    run = legacy_wait(authenticated, settings, tmp_path, monkeypatch)
    wrong = command(
        authenticated,
        run,
        "resolve",
        {
            "agent_recovery": {
                "attempt_id": "wrong",
                "action": "next_candidate",
            }
        },
        "wrong",
    )
    assert wrong.status_code == 409
    with authenticated[0].app.state.session_factory() as db:
        attempt_id = db.get(Run, run["id"]).current_attempt_id
    monkeypatch.setattr("agents_ide.worker.processes.stored_processes_stopped", lambda *_: False)
    active = command(
        authenticated,
        run,
        "resolve",
        {
            "agent_recovery": {
                "attempt_id": attempt_id,
                "action": "next_candidate",
            }
        },
        "active",
    )
    assert active.status_code == 409
    assert active.json()["code"] == "reconciliation_required"


def test_continuation_that_hits_quota_again_transfers_the_same_history(
    authenticated, settings, tmp_path, launch_fixture, monkeypatch
):
    run = legacy_wait(authenticated, settings, tmp_path, monkeypatch, "quota-exhausted")
    with authenticated[0].app.state.session_factory() as db:
        attempt_id = db.get(Run, run["id"]).current_attempt_id
    response = command(
        authenticated,
        run,
        "resolve",
        {
            "agent_recovery": {
                "attempt_id": attempt_id,
                "action": "continue_session",
            }
        },
    )
    assert response.status_code == 200, response.text
    assert command(authenticated, run, "resume").status_code == 200
    result = runner_for(authenticated[0], settings).execute(run["id"])
    assert result.final_state == "completed", result
    rows = [json.loads(line) for line in launch_fixture[1].read_text().splitlines()]
    messages = [r for r in rows if r["path"].endswith("/message")]
    assert len(messages) == 3
    assert len({m["path"] for m in messages}) == 1
    assert messages[-1]["body"]["model"]["modelID"] == "claude-sonnet-4-20250514"
    assert "Original task:\nforce_decision=passed" in messages[-1]["body"]["parts"][0]["text"]


@pytest.mark.parametrize("action", ["continue_session", "next_candidate"])
def test_recovery_uses_changed_group_without_losing_native_history(
    authenticated, settings, tmp_path, launch_fixture, monkeypatch, action
):
    run = legacy_wait(authenticated, settings, tmp_path, monkeypatch)
    client, headers = authenticated
    with client.app.state.session_factory() as db:
        saved = db.get(Run, run["id"])
        attempt_id = saved.current_attempt_id
        selection = json.loads(db.get(StepAttempt, attempt_id).selection_json)
    group = client.get(f"/api/model_groups/{selection['group_id']}", headers=headers).json()
    members = (
        list(reversed(group["members"])) if action == "continue_session" else group["members"][1:]
    )
    updated = client.put(
        f"/api/model_groups/{group['id']}/agent/members",
        headers=headers,
        json={
            "expected_revision": group["revision"],
            "members": [
                {k: m[k] for k in ("id", "harness_profile_id", "model_id", "enabled", "params")}
                for m in members
            ],
        },
    )
    assert updated.status_code == 200, updated.text
    current = client.get(f"/api/runs/{run['id']}", headers=headers).json()
    options = current["waiting_reason"]["resolution_schema"]["agent_recovery"]
    assert options["actions"] == [action]
    resolved = command(
        authenticated,
        run,
        "resolve",
        {"agent_recovery": {"attempt_id": attempt_id, "action": action}},
    )
    assert resolved.status_code == 200, resolved.text
    assert command(authenticated, run, "resume").status_code == 200
    result = runner_for(client, settings).execute(run["id"])
    assert result.final_state == "completed", result
    messages = [
        r
        for r in [json.loads(line) for line in launch_fixture[1].read_text().splitlines()]
        if r["path"].endswith("/message")
    ]
    assert len(messages) == 2
    assert messages[0]["path"] == messages[1]["path"]
    assert messages[1]["body"]["model"]["modelID"] == (
        "recoverable-quota" if action == "continue_session" else "claude-sonnet-4-20250514"
    )
