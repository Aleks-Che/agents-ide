import json
import subprocess

import pytest

from agents_ide.adapters.base import ExternalOutcome, LLMResult
from agents_ide.domain.common import new_id, to_json
from agents_ide.engine import git_commit as git
from agents_ide.persistence.models import Chat, Project, Run, StepAttempt, StepExecution
from agents_ide.services import assistance
from tests.integration.test_sidebar_activity import make_chat_run
from tests.integration.test_stage9_run_chat_filter import _start_run


def stopped_frontend(authenticated, tmp_path):
    run, factory = make_chat_run(authenticated, tmp_path)
    with factory.begin() as session:
        session.get(Project, run["project_id"]).name = "math-portal"
        session.get(Chat, run["chat_id"]).title = "Фронтенд"
        row = session.get(Run, run["id"])
        row.state = "waiting_input"
        row.current_node_id = "gitcommit_1"
        row.waiting_reason_json = to_json(
            {
                "code": "external_change_detected",
                "allowed_actions": ["resolve", "resume", "stop"],
                "details": {"reason": "external_change_detected", "token": "must-not-leak"},
            }
        )
        execution = StepExecution(
            id=new_id(),
            run_id=row.id,
            node_id="gitcommit_1",
            visit_index=1,
            cycle_id=0,
            status="failed",
        )
        session.add(execution)
        session.flush()
        attempt = StepAttempt(
            id=new_id(),
            execution_id=execution.id,
            attempt_index=1,
            status="failed",
            error_code="external_change_detected",
            error_details_json=to_json(
                {
                    "message": "Files outside the allowlist changed",
                    "secret": "must-not-leak",
                }
            ),
        )
        session.add(attempt)
        row.current_attempt_id = attempt.id
    return run, factory


def target(run):
    return {"zone": "chat", "project_id": run["project_id"], "chat_id": run["chat_id"]}


def test_math_portal_diagnostic_is_scoped_and_explains_real_git_error(authenticated, tmp_path):
    client, headers = authenticated
    run, _ = stopped_frontend(authenticated, tmp_path)
    response = client.get("/api/assistance/context", params=target(run))
    assert response.status_code == 200, response.text
    context = response.json()
    assert context["title"] == "math-portal — Фронтенд"
    assert context["guide"]["allowed_mutations"] == []
    (finding,) = context["findings"]
    assert finding["source_id"] == run["id"]
    assert finding["code"] == "external_change_detected"
    assert finding["node_id"] == "gitcommit_1"
    assert "защищённые файлы" in finding["explanation"]
    assert context["questions"] == []  # Questions now come from model generation.
    assert len(context["diagnostic_revision"]) == 64
    assert "must-not-leak" not in response.text
    assert "git_check" not in finding["evidence"]  # polling never scans files
    other = client.post(
        f"/api/projects/{run['project_id']}/chats", headers=headers, json={"title": "Бэкэнд"}
    ).json()
    assert not client.get(
        "/api/assistance/context", params={**target(run), "chat_id": other["id"]}
    ).json()["findings"]


def test_newer_completion_removes_old_warning_from_assistance(authenticated, tmp_path):
    client, headers = authenticated
    run, factory = stopped_frontend(authenticated, tmp_path)
    newer = _start_run(
        client,
        headers,
        project_id=run["project_id"],
        binding_id=run["binding_id"],
        chat_id=run["chat_id"],
    )
    with factory.begin() as session:
        session.get(Run, newer["id"]).state = "completed"
    assert client.get("/api/assistance/context", params=target(run)).json()["findings"] == []


def test_resumed_run_does_not_report_its_previous_error_as_current(authenticated, tmp_path):
    client, _ = authenticated
    run, factory = stopped_frontend(authenticated, tmp_path)
    with factory.begin() as session:
        row = session.get(Run, run["id"])
        row.state = "running"
        row.waiting_reason_json = "null"
    (finding,) = client.get("/api/assistance/context", params=target(run)).json()["findings"]
    assert not finding["attention"]
    assert finding["code"] is None
    assert finding["evidence"]["error_code"] is None
    assert "активно" in finding["explanation"]


def test_target_validation_and_authentication(authenticated, tmp_path):
    client, _ = authenticated
    first, _ = make_chat_run(authenticated, tmp_path, suffix="one")
    second, _ = make_chat_run(authenticated, tmp_path, suffix="two")
    assert client.get("/api/assistance/context", params={"zone": "chat"}).status_code == 422
    assert (
        client.get(
            "/api/assistance/context", params={**target(first), "chat_id": second["chat_id"]}
        ).status_code
        == 422
    )
    assert client.get("/api/assistance/context", params={"zone": "unknown"}).status_code == 422
    assert (
        client.post(
            "/api/assistance/messages",
            headers={"Origin": str(client.base_url).rstrip("/")},
            json={},
        ).status_code
        == 403
    )
    client.cookies.clear()
    assert client.get("/api/assistance/catalog").status_code == 401
    assert client.get("/api/assistance/context", params=target(first)).status_code == 401


@pytest.mark.parametrize("thinking", ["", "<think>" + "private-reasoning " * 800 + "</think>\n"])
def test_ai_receives_fresh_diagnosis_history_and_guide_without_mutating_run(
    authenticated, tmp_path, monkeypatch, thinking
):
    client, headers = authenticated
    run, factory = stopped_frontend(authenticated, tmp_path)
    connection = client.post(
        "/api/connections",
        headers=headers,
        json={
            "name": "Help model",
            "provider_kind": "openai_compatible",
            "base_url": "http://127.0.0.1:9999/v1",
            "manual_models": ["diagnostic-model"],
        },
    )
    assert connection.status_code == 201, connection.text
    seen = []

    def fake_run(self, request):
        seen.append(request)
        return LLMResult(
            ExternalOutcome.SUCCEEDED, thinking + "Проверьте защищённые файлы.", None, None
        )

    monkeypatch.setattr(assistance.HttpLLMAdapter, "run", fake_run)
    payload = {
        "target": target(run),
        "connection_id": connection.json()["id"],
        "model_id": "diagnostic-model",
        "message": "Почему остановился проект?",
        "history": [
            {"role": "user", "content": "Что значит !?"},
            {"role": "assistant", "content": "<think>private-history</think>Проверим запуск."},
            {"role": "assistant", "content": "<think>private-truncated-history"},
        ],
    }
    response = client.post("/api/assistance/messages", headers=headers, json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["content"] == "Проверьте защищённые файлы."
    assert len(seen) == 1
    evidence = seen[0].context_package["evidence"]
    assert evidence["title"] == "math-portal — Фронтенд"
    assert evidence["guide"]["allowed_mutations"] == []
    assert evidence["findings"][0]["evidence"]["git_check"]["status"] == "unavailable"
    policy = evidence["findings"][0]["evidence"]["git_policy"]
    assert policy["requires_reviewed_git_changes_resolution"]
    assert any("payload git_changes" in rule for rule in policy["recovery_rules"])
    assert "Что значит !?" in seen[0].prompt
    assert "Проверим запуск." in seen[0].prompt
    assert "private-history" not in seen[0].prompt
    assert "private-truncated-history" not in seen[0].prompt
    assert "must-not-leak" not in json.dumps(evidence)
    with factory() as session:
        assert session.get(Run, run["id"]).state == "waiting_input"
    # Every question reads current state, even with prior conversational history.
    with factory.begin() as session:
        session.get(Run, run["id"]).state = "completed"
    response = client.post("/api/assistance/messages", headers=headers, json=payload)
    assert response.json()["context"]["findings"] == []
    assert not seen[-1].context_package["evidence"]["findings"]
    assert (
        client.post(
            "/api/assistance/messages",
            headers=headers,
            json={**payload, "model_id": "unconfigured"},
        ).status_code
        == 422
    )
    for outcome, raw in [
        (ExternalOutcome.UNAVAILABLE, ""),
        (ExternalOutcome.SUCCEEDED, "<think>private-only</think>"),
        (ExternalOutcome.SUCCEEDED, "<think>private-unclosed"),
    ]:
        provider_result = LLMResult(outcome, raw, None, None)
        monkeypatch.setattr(
            assistance.HttpLLMAdapter,
            "run",
            lambda *args, result=provider_result: result,
        )
        failed = client.post("/api/assistance/messages", headers=headers, json=payload)
        assert failed.status_code == 502
        assert "private-" not in failed.text


@pytest.mark.parametrize("separate_workspace", [False, True])
def test_git_inspection_identifies_protected_changes_and_preserves_index(
    authenticated, tmp_path, separate_workspace
):
    run, factory = stopped_frontend(authenticated, tmp_path)
    workspace = tmp_path / "git-inspection"
    workspace.mkdir()

    def command(*args):
        return subprocess.check_output(["git", "-C", str(workspace), *args])

    command("init", "--quiet")
    (workspace / "README.md").write_text("original", encoding="utf-8")
    (workspace / ".gitignore").write_text(".env\n", encoding="utf-8")
    command("add", ".")
    command("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "init")
    (workspace / ".env").write_text("private-before", encoding="utf-8")
    baseline = git.capture_baseline(workspace, run["id"], ["src/**"])
    (workspace / "README.md").write_text("modified", encoding="utf-8")
    (workspace / ".env").write_text("private-after", encoding="utf-8")
    index = (workspace / ".git/index").read_bytes()
    with factory() as session:
        row = session.get(Run, run["id"])
        session.expunge(row)
        row.snapshot_json = to_json({"workspace": {"workspace_path": str(workspace)}})
        if separate_workspace:
            # Original project location must not be inspected when a run uses a worktree.
            row.snapshot_json = to_json({"workspace": {"workspace_path": str(tmp_path / "absent")}})
        row.runtime_json = to_json(
            {
                **({"workspace": {"workspace_path": str(workspace)}} if separate_workspace else {}),
                "git": {
                    "baseline": baseline.to_dict(),
                    "allowlist": ["src/**"],
                    "head": baseline.head_sha,
                    "branch": baseline.branch,
                },
            }
        )
        evidence = assistance.inspect_git(row)
    assert evidence["status"] == "checked"
    assert evidence["workspace_source"] == (
        "run_workspace" if separate_workspace else "project_workspace"
    )
    assert {item["path"] for item in evidence["protected_changes"]} == {"README.md"}
    assert evidence["checks_performed"] == ["head", "branch", "protected_files"]
    assert "index" in evidence["checks_not_performed"]
    assert all(item["content_changed"] for item in evidence["protected_changes"])
    assert all(item["baseline_fingerprint_saved"] for item in evidence["protected_changes"])
    assert all(
        not item["baseline_record_contains_file_bytes"] for item in evidence["protected_changes"]
    )
    assert "private-after" not in json.dumps(evidence)
    assert (workspace / ".git/index").read_bytes() == index
    assert (workspace / "README.md").read_text() == "modified"


@pytest.mark.parametrize(
    "state,code",
    [("running", "agent_input_requested"), ("waiting_input", "unknown_external_result")],
)
def test_other_attention_reasons_are_not_misreported_as_git_errors(
    authenticated, tmp_path, state, code
):
    from agents_ide.engine.events import append_event

    client, _ = authenticated
    run, factory = make_chat_run(authenticated, tmp_path)
    with factory.begin() as session:
        row = session.get(Run, run["id"])
        row.state = state
        row.current_attempt_id = "current"
        if state == "running":
            append_event(
                session, row.id, "agent.input_requested", {"question_id": "q"}, attempt_id="current"
            )
        else:
            row.waiting_reason_json = to_json({"code": code})
    (finding,) = client.get("/api/assistance/context", params=target(run)).json()["findings"]
    assert finding["code"] == code
    assert "Git-коммит" not in finding["question"]


def test_guide_uses_current_signals_and_run_history_is_bounded_and_scoped(authenticated, tmp_path):
    from agents_ide.engine.events import append_event

    client, _ = authenticated
    run, factory = stopped_frontend(authenticated, tmp_path)
    other, _ = make_chat_run(authenticated, tmp_path, suffix="other")
    with factory.begin() as session:
        row = session.get(Run, run["id"])
        snapshot = json.loads(row.snapshot_json)
        snapshot["graph"]["nodes"].append(
            {"id": "gitcommit_1", "type": "GitCommit", "label": "Сохранить изменения"}
        )
        runtime = json.loads(row.runtime_json)
        runtime["template_configuration"] = {"graph": snapshot["graph"]}
        row.runtime_json = to_json(runtime)
        for _ in range(12):
            append_event(
                session,
                row.id,
                "attempt.started",
                {"prompt": "private-event-payload"},
                attempt_id=row.current_attempt_id,
                node_id="gitcommit_1",
            )
        append_event(session, row.id, "attempt.text_delta", {"text": "private-agent-text"})
        for _ in range(12):
            append_event(session, row.id, "control.applied", {"data": "private-control-payload"})
        append_event(session, other["id"], "run.failed", {"secret": "private-other-run"})
    response = client.get("/api/assistance/context", params=target(run))
    assert response.status_code == 200, response.text
    context = response.json()
    cases = {case["id"] for case in context["guide"]["diagnostic_cases"]}
    assert "git_guard" in cases
    assert "unknown_result" not in cases
    details = context["findings"][0]["evidence"]["run_details"]
    assert details["stage"] == {
        "id": "gitcommit_1",
        "type": "GitCommit",
        "label": "Сохранить изменения",
    }
    assert len(details["recent_events"]) == 10
    assert details["earlier_events_omitted"] is True
    sequences = [event["sequence"] for event in details["recent_events"]]
    assert sequences == sorted(sequences)
    assert all(event["type"] == "control.applied" for event in details["recent_events"])
    assert details["last_recorded_event"]["type"] == "control.applied"
    assert len(details["current_attempt_events"]) == 10
    assert details["earlier_attempt_events_omitted"] is True
    assert all(event["type"] == "attempt.started" for event in details["current_attempt_events"])
    assert all(
        event["attempt_id"] == details["attempt"]["id"]
        for event in details["current_attempt_events"]
    )
    assert "model_id=null" in details["stage_semantics"]
    assert "private-" not in response.text
    catalog = client.get("/api/assistance/catalog").json()
    entry = next(entry for entry in catalog if entry["zone"] == "chat")
    assert {"git_guard", "unknown_result", "agent_recovery"} <= {
        case["id"] for case in entry["diagnostic_cases"]
    }
    with factory.begin() as session:
        row = session.get(Run, run["id"])
        row.state = "running"
        row.waiting_reason_json = "null"
    refreshed = client.get("/api/assistance/context", params=target(run)).json()
    assert "git_guard" not in {case["id"] for case in refreshed["guide"]["diagnostic_cases"]}
