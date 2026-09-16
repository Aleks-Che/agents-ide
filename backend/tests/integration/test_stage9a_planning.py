import json

import pytest
from council_support import ANSWERS, QUESTIONS, confirm, create, dispatch, document, setup
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from agents_ide.persistence.models import PlanningRevision


def test_answers_create_new_revision_show_answers_and_confirm_idempotently(authenticated, tmp_path):
    client, headers = authenticated
    *_, payload = setup(client, headers, tmp_path)
    job = dispatch(client, create(client, headers, payload), questions=QUESTIONS)
    assert job["state"] == "needs_answers"
    before = job["revisions"][0]
    response = client.post(
        f"/api/planning_jobs/{job['id']}/answers",
        headers=headers,
        json={"expected_revision": 1, "answers": ANSWERS[:1]},
    )
    assert response.status_code == 422
    response = client.post(
        f"/api/planning_jobs/{job['id']}/answers",
        headers=headers,
        json={"expected_revision": 1, "answers": ANSWERS},
    )
    assert response.status_code == 200, response.text
    job = client.get(f"/api/planning_jobs/{job['id']}").json()
    assert job["state"] == "ready_for_confirmation"
    assert job["revisions"][0] == before
    current = job["revisions"][-1]
    assert current["revision_number"] == 2
    assert "Offline only" in current["body_text"] and "SQLite" in current["body_text"]
    assert len(current["answers"]) == 3
    assert current["confirmation_hash"] != before["confirmation_hash"]
    stale = client.post(
        f"/api/planning_jobs/{job['id']}/answers",
        headers=headers,
        json={"expected_revision": 1, "answers": ANSWERS},
    )
    assert stale.status_code == 409
    confirm(client, headers, job)
    confirm(client, headers, job)
    with client.app.state.session_factory() as session:
        revision = session.scalar(
            select(PlanningRevision).where(
                PlanningRevision.job_id == job["id"], PlanningRevision.revision_number == 2
            )
        )
        revision.body_text = "Cannot alter an already confirmed document"
        with pytest.raises(IntegrityError, match="confirmed_planning_revision_immutable"):
            session.commit()
    assert (
        client.post(
            f"/api/planning_jobs/{job['id']}/answers",
            headers=headers,
            json={"expected_revision": 2, "user_body_text": json.dumps(document("changed"))},
        ).status_code
        == 409
    )


def test_manual_edit_without_questions_is_validated_and_hash_binds_text_and_steps(
    authenticated, tmp_path
):
    client, headers = authenticated
    *_, payload = setup(client, headers, tmp_path)
    job = dispatch(client, create(client, headers, payload))
    assert job["state_version"] != job["revisions"][0]["revision_number"]
    original = job["revisions"][0]
    for bad in ("", "not JSON", '{"body_text":"x","steps":[]}'):
        assert (
            client.post(
                f"/api/planning_jobs/{job['id']}/answers",
                headers=headers,
                json={"expected_revision": 1, "user_body_text": bad},
            ).status_code
            == 422
        )
    result = client.post(
        f"/api/planning_jobs/{job['id']}/answers",
        headers=headers,
        json={"expected_revision": 1, "user_body_text": json.dumps(document("Manually revised"))},
    )
    assert result.status_code == 200, result.text
    job = client.get(f"/api/planning_jobs/{job['id']}").json()
    assert job["revisions"][0] == original
    assert job["revisions"][1]["confirmation_hash"] != original["confirmation_hash"]
    with client.app.state.session_factory() as session:
        revision = session.scalar(
            select(PlanningRevision).where(
                PlanningRevision.job_id == job["id"], PlanningRevision.revision_number == 2
            )
        )
        revision.body_text = "Changed behind the review"
        session.commit()
    response = client.post(
        f"/api/planning_jobs/{job['id']}/confirm",
        headers=headers,
        json={
            "expected_revision": 2,
            "confirmation_hash": job["revisions"][1]["confirmation_hash"],
        },
    )
    assert response.status_code == 409


@pytest.mark.parametrize("count", [2, 3, 4])
def test_count_default_and_merger_reuse(authenticated, tmp_path, count):
    client, headers = authenticated
    *_, payload = setup(client, headers, tmp_path, count=count)
    payload["participants"][-1]["selection"] = payload["participants"][0]["selection"]
    job = create(client, headers, payload)
    assert len(job["members"]) == count + 1
    assert job["n_participants_actual"] == 0
    assert len(job["read_manifest_hash"]) == 64


def test_validation_idempotency_chat_auth_and_cancel(authenticated, tmp_path):
    client, headers = authenticated
    *_, payload = setup(client, headers, tmp_path)
    assert client.post("/api/planning_jobs", json=payload).status_code == 403
    job = create(client, headers, payload)
    assert create(client, headers, payload)["id"] == job["id"]
    assert (
        client.post(
            "/api/planning_jobs", headers=headers, json={**payload, "task_text": "different"}
        ).status_code
        == 409
    )
    for change in (
        {"chat_id": "missing"},
        {"task_text": "   "},
        {"participants": payload["participants"][:1]},
        {"participants": payload["participants"][1:]},
    ):
        response = client.post(
            "/api/planning_jobs",
            headers=headers,
            json={**payload, "idempotency_key": str(change), **change},
        )
        assert response.status_code in (409, 422), response.text
    assert (
        client.post(
            f"/api/planning_jobs/{job['id']}/cancel",
            headers=headers,
            json={"expected_state_version": 999},
        ).status_code
        == 409
    )
    response = client.post(
        f"/api/planning_jobs/{job['id']}/cancel",
        headers=headers,
        json={"expected_state_version": job["state_version"]},
    )
    assert response.json()["state"] == "cancelled"
    assert dispatch(client, job)["state"] == "cancelled"


def test_duplicate_models_across_connections_and_unsupported_agent_rejected(
    authenticated, tmp_path
):
    client, headers = authenticated
    *_, connection, payload = setup(client, headers, tmp_path)
    other = client.post(
        "/api/connections",
        headers=headers,
        json={"name": "another", "base_url": connection["base_url"]},
    ).json()
    payload["participants"][1]["selection"] = {
        **payload["participants"][0]["selection"],
        "provider_connection_id": other["id"],
    }
    response = client.post("/api/planning_jobs", headers=headers, json=payload)
    assert response.status_code == 409 and response.json()["code"] == "council_duplicate_member"
    payload["participants"][1]["selection"] = {
        "kind": "direct",
        "model_id": "agent",
        "harness_profile_id": "profile",
    }
    response = client.post("/api/planning_jobs", headers=headers, json=payload)
    assert (
        response.status_code == 422 and response.json()["code"] == "council_harness_unimplemented"
    )
