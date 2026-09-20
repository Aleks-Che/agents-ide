"""Resume interrupted agents after their own workspace edits, including old runs."""

import json
from pathlib import Path

import pytest
from sqlalchemy import select
from test_agent_recovery import command
from test_stage6a_review import runner_for, seed

from agents_ide.adapters.base import AdapterError, AgentAdapter, AgentResult, ExternalOutcome
from agents_ide.engine.runner import Runner
from agents_ide.persistence.models import Run, StepAttempt, StepExecution


def pause_after_edit(authenticated, settings, tmp_path, monkeypatch, control="pause"):
    run, _ = seed(authenticated, tmp_path, roles=("reviewer",), group=True)
    calls = []

    class EditingAgent(AgentAdapter):
        def run(self, request):
            calls.append(request)
            path = Path(request.workspace_path) / "partial.txt"
            request.emit_event(
                "agent.session_resumed" if request.resume_session_id else "agent.session_created",
                {"session_id": request.resume_session_id or "saved-session"},
            )
            if len(calls) == 1:
                path.write_text("agent work", encoding="utf-8")
                response = command(authenticated, run, control)
                assert response.status_code == 200, response.text
                return AgentResult(
                    ExternalOutcome.UNKNOWN,
                    "partial work",
                    None,
                    None,
                    error=AdapterError(
                        "interrupted", "interrupted", details={"interruption_confirmed": True}
                    ),
                )
            assert path.read_text(encoding="utf-8") == "agent work"
            return AgentResult(
                ExternalOutcome.SUCCEEDED, '{"verdict":"passed"}', {"verdict": "passed"}, "passed"
            )

    agent = EditingAgent()
    monkeypatch.setattr(Runner, "_agent_adapter_for", lambda *_: agent)
    result = runner_for(authenticated[0], settings).execute(run["id"])
    assert result.final_state == ("paused" if control == "pause" else "stopped"), result
    return run, calls


@pytest.mark.parametrize("control", ["pause", "stop"])
def test_resume_after_agent_edit_preserves_stage_and_session(
    authenticated, settings, tmp_path, monkeypatch, control
):
    run, calls = pause_after_edit(authenticated, settings, tmp_path, monkeypatch, control)
    factory = authenticated[0].app.state.session_factory
    with factory() as db:
        execution_id = db.get(Run, run["id"]).current_execution_id
    response = command(authenticated, run, "resume")
    assert response.status_code == 200, response.text
    assert runner_for(authenticated[0], settings).execute(run["id"]).final_state == "completed"
    assert len(calls) == 2
    assert calls[1].resume_session_id == ("saved-session" if control == "pause" else None)
    assert calls[1].resume_required == (control == "pause")
    assert calls[1].model_id == calls[0].model_id
    with factory() as db:
        assert (
            len(list(db.scalars(select(StepExecution).where(StepExecution.node_id == "a0")))) == 1
        )
        attempts = list(
            db.scalars(select(StepAttempt).where(StepAttempt.execution_id == execution_id))
        )
        assert sorted(a.status for a in attempts) == ["interrupted", "succeeded"]


@pytest.mark.parametrize("blocked", [False, True])
def test_resume_repairs_legacy_pause_checkpoint(
    authenticated, settings, tmp_path, monkeypatch, blocked
):
    run, calls = pause_after_edit(authenticated, settings, tmp_path, monkeypatch)
    factory = authenticated[0].app.state.session_factory
    with factory() as db:
        row = db.get(Run, run["id"])
        runtime = json.loads(row.runtime_json)
        runtime.pop("interrupted_evidence_attempt_id")
        runtime["logical_evidence_hash"] = "old-input-before-agent-edits"
        if blocked:
            row.state = "waiting_input"
            waiting = {
                "code": "external_change_detected",
                "details": {"reason": "evidence_changed_between_candidates"},
                "allowed_actions": ["resolve", "resume", "pause", "stop", "cancel"],
            }
            row.waiting_reason_json = json.dumps(waiting)
            runtime["waiting_reason"] = waiting
            target = json.loads(row.resume_target_json)
            target["blockers"] = ["external_change_detected"]
            row.resume_target_json = json.dumps(target)
        row.runtime_json = json.dumps(runtime)
        db.commit()
    response = command(authenticated, run, "resume")
    assert response.status_code == 200, response.text
    assert runner_for(authenticated[0], settings).execute(run["id"]).final_state == "completed"
    assert calls[1].resume_session_id == "saved-session"


def test_edits_during_pause_still_block_dispatch(authenticated, settings, tmp_path, monkeypatch):
    run, calls = pause_after_edit(authenticated, settings, tmp_path, monkeypatch)
    (tmp_path / "workspace" / "partial.txt").write_text("external edit", encoding="utf-8")
    assert command(authenticated, run, "resume").status_code == 200
    result = runner_for(authenticated[0], settings).execute(run["id"])
    assert result.waiting_reason.code == "external_change_detected"
    assert command(authenticated, run, "resume", command_id="retry").status_code == 409
    assert len(calls) == 1


@pytest.mark.parametrize("cause", ["unknown_attempt", "unconfirmed", "missing_details"])
def test_legacy_repair_requires_confirmed_interruption(
    authenticated, settings, tmp_path, monkeypatch, cause
):
    run, calls = pause_after_edit(authenticated, settings, tmp_path, monkeypatch)
    with authenticated[0].app.state.session_factory() as db:
        row = db.get(Run, run["id"])
        runtime = json.loads(row.runtime_json)
        runtime.pop("interrupted_evidence_attempt_id")
        row.runtime_json = json.dumps(runtime)
        row.state = "waiting_input"
        row.waiting_reason_json = json.dumps(
            {
                "code": "external_change_detected",
                "details": {"reason": "evidence_changed_between_candidates"},
                "allowed_actions": ["resolve", "resume", "cancel"],
            }
        )
        target = json.loads(row.resume_target_json)
        target["blockers"] = ["external_change_detected"]
        row.resume_target_json = json.dumps(target)
        attempt = db.get(StepAttempt, row.current_attempt_id)
        if cause == "unknown_attempt":
            attempt.status = "unknown"
        else:
            attempt.error_details_json = None if cause == "missing_details" else "{}"
        db.commit()
    assert command(authenticated, run, "resume").status_code == 409
    assert len(calls) == 1
