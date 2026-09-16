import json
from uuid import uuid4

from agents_ide.engine.planning_worker import dispatch_planning_job


def document(text="A complete plan", questions=None):
    return {
        "body_text": text,
        "steps": [{"title": "Implement the change", "acceptance_criteria": ["Tests pass"]}],
        "questions": questions or [],
    }


QUESTIONS = [
    {
        "id": "q1",
        "kind": "single",
        "prompt": "Database?",
        "options": [{"id": "a", "label": "SQLite"}, {"id": "b", "label": "Postgres"}],
    },
    {
        "id": "q2",
        "kind": "multi",
        "prompt": "Checks?",
        "options": [{"id": "a", "label": "Unit"}, {"id": "b", "label": "Browser"}],
    },
    {"id": "q3", "kind": "text", "prompt": "Constraints?"},
]
ANSWERS = [
    {"question_id": "q1", "kind": "single", "selected_option_ids": ["a"]},
    {"question_id": "q2", "kind": "multi", "selected_option_ids": ["a", "b"]},
    {"question_id": "q3", "kind": "text", "free_text": "Offline only"},
]


def setup(
    client,
    headers,
    tmp_path,
    *,
    count=2,
    base_url="http://127.0.0.1:9/v1",
    secret=None,
    budget=None,
):
    suffix = uuid4().hex
    workspace = tmp_path / suffix
    workspace.mkdir()
    project = client.post(
        "/api/projects", headers=headers, json={"name": suffix, "workspace_path": str(workspace)}
    ).json()
    chat = client.post(
        f"/api/projects/{project['id']}/chats", headers=headers, json={"title": "Council"}
    ).json()
    connection = client.post(
        "/api/connections",
        headers=headers,
        json={"name": suffix, "base_url": base_url, "secret": secret},
    ).json()
    assert "id" in connection, connection
    participants = [
        {
            "role": "participant",
            "selection": {
                "kind": "direct",
                "model_id": f"model-{i}",
                "provider_connection_id": connection["id"],
            },
        }
        for i in range(count)
    ]
    merger = {
        "role": "merger",
        "selection": {
            "kind": "direct",
            "model_id": "merger",
            "provider_connection_id": connection["id"],
        },
    }
    payload = {
        "project_id": project["id"],
        "chat_id": chat["id"],
        "task_text": "Plan a feature",
        "context_text": "Pinned context marker",
        "participants": [*participants, merger],
        "idempotency_key": suffix,
    }
    if budget:
        payload["budget"] = budget
    return project, chat, connection, payload


def create(client, headers, payload):
    response = client.post("/api/planning_jobs", headers=headers, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def dispatch(client, job, *, questions=None, responses=None):
    if responses is None:
        responses = [
            {
                "node_id": f"council_participant_{i}",
                "raw_text": json.dumps(document(f"Draft marker {i}")),
            }
            for i in range(job["n_participants_requested"])
        ]
        responses.append(
            {
                "node_id": "council_merger",
                "raw_text": json.dumps(document("Unified plan", questions)),
            }
        )
    dispatch_planning_job(
        client.app.state.session_factory,
        job["id"],
        simulated=True,
        fake_scenario={"responses": responses},
    )
    result = client.get(f"/api/planning_jobs/{job['id']}")
    assert result.status_code == 200, result.text
    return result.json()


def confirm(client, headers, job):
    revision = job["revisions"][-1]
    response = client.post(
        f"/api/planning_jobs/{job['id']}/confirm",
        headers=headers,
        json={
            "expected_revision": revision["revision_number"],
            "confirmation_hash": revision["confirmation_hash"],
        },
    )
    assert response.status_code == 200, response.text
    return {
        "job_id": job["id"],
        "revision_number": revision["revision_number"],
        "confirmation_hash": revision["confirmation_hash"],
    }
