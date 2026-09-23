import json

import test_stage6a_review as opencode_review

from agents_ide.adapters.base import ExternalOutcome, LLMResult
from agents_ide.domain.common import new_id, to_json
from agents_ide.persistence.models import ArtifactManifest, Run, StepAttempt
from agents_ide.services import assistance, assistance_recovery
from tests.integration.test_agent_recovery import legacy_wait

launch_fixture = opencode_review.launch_fixture


def test_assistant_receives_actual_agent_recovery_options_and_conditions(
    authenticated, settings, tmp_path, launch_fixture, monkeypatch
):
    run = legacy_wait(authenticated, settings, tmp_path, monkeypatch)
    client, headers = authenticated
    target = {"zone": "project", "project_id": run["project_id"]}
    context = client.get("/api/assistance/context", params=target)
    assert context.status_code == 200, context.text
    (finding,) = context.json()["findings"]
    recovery = finding["evidence"]["recovery"]
    assert recovery["harness"] == "opencode"
    assert recovery["native_session_saved"]
    assert recovery["request_reference_saved"]
    assert recovery["resume_checkpoint_saved"]
    assert recovery["processes_stopped"] is None  # polling never probes processes
    actions = {item["action"]: item for item in recovery["actions"]}
    assert actions["continue_session"]["available"] is None
    assert actions["next_candidate"]["available"] is None
    assert actions["accept_result"]["available"] is False
    assert "позднего результата" in actions["accept_result"]["blocked_by"][0]
    assert "Сохранить решение»" in finding["next_step"]
    assert "отдельно «Продолжить»" in finding["next_step"]
    assert any("не откатывают" in rule for rule in recovery["rules"])

    # Exercise the real chat endpoint, substituting only the external model.
    connection = client.post(
        "/api/connections",
        headers=headers,
        json={
            "name": "Assistance recovery test",
            "provider_kind": "openai_compatible",
            "base_url": "http://127.0.0.1:9999/v1",
            "manual_models": ["test-model"],
        },
    )
    assert connection.status_code == 201, connection.text
    seen = []

    def model_reply(self, request):
        seen.append(request)
        return LLMResult(ExternalOutcome.SUCCEEDED, "Продолжите сохранённую сессию.", None, None)

    monkeypatch.setattr(assistance.HttpLLMAdapter, "run", model_reply)
    payload = {
        "target": target,
        "connection_id": connection.json()["id"],
        "model_id": "test-model",
        "message": "Как продолжить?",
    }
    reply = client.post("/api/assistance/messages", headers=headers, json=payload)
    assert reply.status_code == 200, reply.text
    sent = seen[0].context_package["evidence"]["findings"][0]["evidence"]["recovery"]
    assert sent["processes_stopped"] is True
    actions = {item["action"]: item for item in sent["actions"]}
    assert actions["continue_session"]["available"] is True
    assert actions["next_candidate"]["available"] is True
    assert actions["accept_result"]["available"] is False
    assert "resolve и resume" in seen[0].prompt
    context = seen[0].context_package["evidence"]
    assert "unknown_result" in {case["id"] for case in context["guide"]["diagnostic_cases"]}
    assert "git_guard" not in {case["id"] for case in context["guide"]["diagnostic_cases"]}
    assert context["findings"][0]["evidence"]["run_details"]["stage"]["type"] == "AgentTask"
    with client.app.state.session_factory() as session:
        saved = session.get(Run, run["id"])
        assert saved.state == "waiting_input"
        assert session.get(StepAttempt, saved.current_attempt_id).status == "unknown"

    # Late results must belong to this run AND this attempt, and be validated.
    with client.app.state.session_factory.begin() as session:
        saved = session.get(Run, run["id"])
        artifact = ArtifactManifest(
            id=new_id(),
            run_id=saved.id,
            step_attempt_id="another-attempt",
            schema_type="late_result",
            source_kind="test",
            byte_length=0,
            content_hash="0" * 64,
            body_json=to_json({"validated_by_server": True, "raw_text": "private-result"}),
        )
        session.add(artifact)
        runtime = json.loads(saved.runtime_json)
        runtime["late_result_refs"] = {saved.current_attempt_id: artifact.id}
        saved.runtime_json = to_json(runtime)
        artifact_id = artifact.id
    response = client.get("/api/assistance/context", params=target)
    assert not response.json()["findings"][0]["evidence"]["recovery"]["validated_late_result"]
    assert "private-result" not in response.text
    with client.app.state.session_factory.begin() as session:
        saved = session.get(Run, run["id"])
        session.get(ArtifactManifest, artifact_id).step_attempt_id = saved.current_attempt_id
    reply = client.post("/api/assistance/messages", headers=headers, json=payload)
    assert reply.status_code == 200, reply.text
    sent = seen[-1].context_package["evidence"]["findings"][0]["evidence"]["recovery"]
    assert sent["validated_late_result"]
    assert next(item for item in sent["actions"] if item["action"] == "accept_result")["available"]

    monkeypatch.setattr(assistance_recovery, "stored_processes_stopped", lambda *args: False)
    reply = client.post("/api/assistance/messages", headers=headers, json=payload)
    assert reply.status_code == 200, reply.text
    sent = seen[-1].context_package["evidence"]["findings"][0]["evidence"]["recovery"]
    assert all(item["available"] is False for item in sent["actions"])
    assert all("Остановка" in item["blocked_by"][-1] for item in sent["actions"])
