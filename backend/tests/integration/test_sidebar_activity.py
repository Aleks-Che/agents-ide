from agents_ide.domain.common import new_id, to_json, utc_now
from agents_ide.engine.events import append_event
from agents_ide.persistence.models import Chat, PlanningJob, Project, Run
from tests.integration.test_stage5_review import command as run_command
from tests.integration.test_stage9_run_chat_filter import (
    _project,
    _start_run,
    _template_version_binding,
)

RUNNING = {"running": True, "attention": False}
ATTENTION = {"running": False, "attention": True}


def activity(client):
    response = client.get("/api/sidebar/activity")
    assert response.status_code == 200, response.text
    return response.json()


def make_chat_run(authenticated, tmp_path, *, suffix="run"):
    client, headers = authenticated
    project = _project(client, headers, tmp_path, suffix)
    binding = _template_version_binding(client, headers, project["id"])
    response = client.post(
        f"/api/projects/{project['id']}/chats", headers=headers, json={"title": "Run chat"}
    )
    assert response.status_code == 201, response.text
    run = _start_run(
        client,
        headers,
        project_id=project["id"],
        binding_id=binding["id"],
        chat_id=response.json()["id"],
    )
    return run, client.app.state.session_factory


def test_run_states_and_manual_stops(authenticated, tmp_path):
    client, _ = authenticated
    run, factory = make_chat_run(authenticated, tmp_path)
    for state, expected in [
        ("queued", RUNNING),
        ("running", RUNNING),
        ("retry_wait", RUNNING),
        ("recovering", RUNNING),
        ("pause_requested", RUNNING),
        ("stop_requested", RUNNING),
        ("waiting_input", ATTENTION),
        ("failed", ATTENTION),
        ("paused", None),
        ("stopped", None),
        ("completed", None),
        ("cancelled", None),
    ]:
        with factory.begin() as session:
            session.get(Run, run["id"]).state = state
        result = activity(client)
        assert result["projects"].get(run["project_id"]) == expected, state
        assert result["chats"].get(run["chat_id"]) == expected, state

    with factory.begin() as session:
        row = session.get(Run, run["id"])
        row.state = "stopped"
        row.waiting_reason_json = to_json({"code": "process_not_responding"})
    assert activity(client)["chats"][run["chat_id"]] == ATTENTION


def test_projects_aggregate_all_chats_and_ignore_archived_items(authenticated, tmp_path):
    client, headers = authenticated
    first, factory = make_chat_run(authenticated, tmp_path, suffix="first")
    second, _ = make_chat_run(authenticated, tmp_path, suffix="second")
    chat = client.post(
        f"/api/projects/{first['project_id']}/chats", headers=headers, json={"title": "Other chat"}
    ).json()
    response = client.post(
        "/api/runs",
        headers=headers,
        json={
            "project_id": first["project_id"],
            "binding_id": first["binding_id"],
            "chat_id": chat["id"],
            "execution_mode": "simulated",
            "message": "another chat",
            "idempotency_key": "other-chat",
        },
    )
    assert response.status_code == 201, response.text
    third = response.json()
    with factory.begin() as session:
        session.get(Run, first["id"]).state = "running"
        session.get(Run, second["id"]).state = "waiting_input"
        session.get(Run, third["id"]).state = "failed"
    result = activity(client)
    assert result["projects"] == {
        first["project_id"]: {"running": True, "attention": True},
        second["project_id"]: ATTENTION,
    }
    assert result["chats"] == {
        first["chat_id"]: RUNNING,
        second["chat_id"]: ATTENTION,
        third["chat_id"]: ATTENTION,
    }
    with factory.begin() as session:
        session.get(Chat, third["chat_id"]).archived_at = utc_now()
        session.get(Project, second["project_id"]).archived_at = utc_now()
    assert activity(client) == {
        "projects": {first["project_id"]: RUNNING},
        "chats": {first["chat_id"]: RUNNING},
    }
    with factory.begin() as session:
        session.get(Run, first["id"]).state = "completed"
    _start_run(
        client,
        headers,
        project_id=first["project_id"],
        binding_id=first["binding_id"],
        chat_id=None,
    )
    assert activity(client) == {"projects": {first["project_id"]: RUNNING}, "chats": {}}


def test_agent_prompts_survive_large_payloads_and_clear_only_after_all_replies(
    authenticated, tmp_path
):
    client, _ = authenticated
    run, factory = make_chat_run(authenticated, tmp_path)
    with factory.begin() as session:
        row = session.get(Run, run["id"])
        row.state = "running"
        row.current_attempt_id = "current"
        for attempt_id, question_id in [
            ("old", "stale"),
            ("current", "q"),
            ("current", "permission:p"),
        ]:
            append_event(
                session,
                run["id"],
                "agent.input_requested",
                {"question_id": question_id, "questions": [{"question": "Q" * 20000}]},
                attempt_id=attempt_id,
            )
    assert activity(client)["chats"][run["chat_id"]] == ATTENTION
    with factory.begin() as session:
        append_event(
            session, run["id"], "agent.input_closed", {"question_id": "q"}, attempt_id="current"
        )
    assert activity(client)["projects"][run["project_id"]] == ATTENTION
    with factory.begin() as session:
        append_event(
            session,
            run["id"],
            "agent.input_closed",
            {"question_id": "permission:p"},
            attempt_id="current",
        )
    assert activity(client)["chats"][run["chat_id"]] == RUNNING
    with factory.begin() as session:
        append_event(
            session, run["id"], "agent.input_requested", {"question_id": "q2"}, attempt_id="current"
        )
        session.get(Run, run["id"]).current_attempt_id = "next"
    assert activity(client)["chats"][run["chat_id"]] == RUNNING


def test_old_errors_do_not_survive_resuming_the_current_run(authenticated, tmp_path):
    client, headers = authenticated
    first, factory = make_chat_run(authenticated, tmp_path)
    runs = [first]
    for _ in range(3):
        runs.append(
            _start_run(
                client,
                headers,
                project_id=first["project_id"],
                binding_id=first["binding_id"],
                chat_id=first["chat_id"],
            )
        )
    latest = runs[-1]
    with factory.begin() as session:
        for index, (run, state) in enumerate(
            zip(runs, ["failed", "waiting_input", "stopped", "stopped"], strict=True)
        ):
            row = session.get(Run, run["id"])
            row.created_at = float(index)
            row.updated_at = 100 - index  # Updating history must not make it current.
            row.state = state
            row.waiting_reason_json = to_json(
                {"code": "configuration_invalid", "allowed_actions": ["resume", "cancel"]}
            )
    assert activity(client) == {
        "projects": {first["project_id"]: ATTENTION},
        "chats": {first["chat_id"]: ATTENTION},
    }
    response = run_command(authenticated, latest, "resume")
    assert response.status_code == 200, response.text
    assert activity(client) == {
        "projects": {first["project_id"]: RUNNING},
        "chats": {first["chat_id"]: RUNNING},
    }
    for state, expected in [
        ("running", RUNNING),
        ("completed", None),
        ("cancelled", None),
        ("paused", None),
        ("failed", ATTENTION),
        ("waiting_input", ATTENTION),
    ]:
        with factory.begin() as session:
            session.get(Run, latest["id"]).state = state
        result = activity(client)
        assert result["projects"].get(first["project_id"]) == expected, state
        assert result["chats"].get(first["chat_id"]) == expected, state

    # An older process that is actually running (or asking a question) still counts.
    with factory.begin() as session:
        session.get(Run, latest["id"]).state = "completed"
        row = session.get(Run, first["id"])
        row.state = "running"
        row.current_attempt_id = "active"
    assert activity(client)["chats"][first["chat_id"]] == RUNNING
    with factory.begin() as session:
        append_event(
            session,
            first["id"],
            "agent.input_requested",
            {"question_id": "live"},
            attempt_id="active",
        )
    assert activity(client)["projects"][first["project_id"]] == ATTENTION


def test_planning_activity_is_not_limited_by_completed_history(authenticated, tmp_path):
    client, headers = authenticated
    run, factory = make_chat_run(authenticated, tmp_path)
    history_chat = client.post(
        f"/api/projects/{run['project_id']}/chats", headers=headers, json={"title": "History"}
    ).json()
    job_id = new_id()
    with factory.begin() as session:
        session.get(Run, run["id"]).state = "completed"
        for index in range(105):
            session.add(
                PlanningJob(
                    id=job_id if index == 0 else new_id(),
                    idempotency_key=new_id(),
                    project_id=run["project_id"],
                    chat_id=run["chat_id"] if index == 0 else history_chat["id"],
                    state="drafting" if index == 0 else "confirmed",
                    task_text="Plan",
                    n_participants_requested=1,
                    created_at=float(index),
                )
            )
    for state, expected in [
        ("drafting", RUNNING),
        ("merging", RUNNING),
        ("needs_answers", ATTENTION),
        ("ready_for_confirmation", ATTENTION),
        ("failed", ATTENTION),
        ("confirmed", None),
        ("cancelled", None),
    ]:
        with factory.begin() as session:
            session.get(PlanningJob, job_id).state = state
        result = activity(client)
        assert result["projects"].get(run["project_id"]) == expected, state
        assert result["chats"].get(run["chat_id"]) == expected, state


def test_new_planning_job_supersedes_old_errors(authenticated, tmp_path):
    client, _ = authenticated
    run, factory = make_chat_run(authenticated, tmp_path)
    latest_id = new_id()
    with factory.begin() as session:
        session.get(Run, run["id"]).state = "completed"
        for index, state in enumerate(["failed", "needs_answers", "ready_for_confirmation"]):
            session.add(
                PlanningJob(
                    id=latest_id if index == 2 else new_id(),
                    idempotency_key=new_id(),
                    project_id=run["project_id"],
                    chat_id=run["chat_id"],
                    state=state,
                    task_text="Plan",
                    n_participants_requested=1,
                    created_at=float(index),
                )
            )
    assert activity(client)["chats"][run["chat_id"]] == ATTENTION
    for state, expected in [("drafting", RUNNING), ("confirmed", None), ("cancelled", None)]:
        with factory.begin() as session:
            session.get(PlanningJob, latest_id).state = state
        result = activity(client)
        assert result["projects"].get(run["project_id"]) == expected, state
        assert result["chats"].get(run["chat_id"]) == expected, state
