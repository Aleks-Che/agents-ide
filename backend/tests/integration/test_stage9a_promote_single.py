"""Explicit single-member fallback after a Council quorum loss."""

import json

from council_support import confirm, create, dispatch, document, setup
from sqlalchemy import select

from agents_ide.persistence.models import (
    PlanningDraft,
    PlanningJob,
    PlanningMember,
    PlanningRevision,
)


def _quorum_lost_one_accepted(client, headers, tmp_path, *, with_questions=False):
    """Drive a 2-participant Council to a quorum loss with one accepted draft."""
    questions = (
        [
            {
                "id": "q1",
                "kind": "single",
                "prompt": "Choice?",
                "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
            }
        ]
        if with_questions
        else []
    )
    body = json.dumps(document("Accepted draft", questions))
    *_, payload = setup(client, headers, tmp_path)
    job = create(client, headers, payload)
    failed = dispatch(
        client,
        job,
        responses=[
            {"node_id": "council_participant_0", "outcome": "confirmed_failure"},
            {"node_id": "council_participant_1", "raw_text": body},
        ],
    )
    assert failed["state"] == "failed"
    assert failed["last_error"]["code"] == "council_quorum_missing"
    assert failed["n_participants_actual"] == 1
    return failed


def test_promote_creates_single_member_revision_and_unblocks_run(authenticated, tmp_path):
    client, headers = authenticated
    job = _quorum_lost_one_accepted(client, headers, tmp_path)
    response = client.post(
        f"/api/planning_jobs/{job['id']}/promote_single",
        headers=headers,
        json={
            "expected_state_version": job["state_version"],
            "confirm_degraded": True,
        },
    )
    assert response.status_code == 200, response.text
    promoted = response.json()
    assert promoted["state"] == "ready_for_confirmation"
    assert promoted["degraded"] is True
    assert promoted["n_participants_actual"] == 1
    refreshed = client.get(f"/api/planning_jobs/{job['id']}").json()
    revision = refreshed["revisions"][-1]
    assert revision["author"] == "single_member"
    assert "Accepted draft" in revision["body_text"]
    # The degraded path stays degraded even after explicit confirmation.
    assert refreshed["degraded"] is True
    # Failed drafts remain visible for diagnostics.
    assert any(not d["accepted"] for d in refreshed["drafts"])
    # The event marks the degraded promotion explicitly.
    promoted_event = next(
        e for e in refreshed["events"] if e["type"] == "planning.single_member_promoted"
    )
    assert promoted_event["payload"]["revision_number"] == revision["revision_number"]
    # The promoted revision can be confirmed and the resulting hash flows into RunStart.
    confirm(client, headers, refreshed)
    with client.app.state.session_factory() as session:
        revision_row = session.get(PlanningRevision, revision["id"])
        assert revision_row.confirmation_hash is not None
        assert revision_row.confirmed_at is not None


def test_promote_rejects_without_confirm_degraded(authenticated, tmp_path):
    client, headers = authenticated
    job = _quorum_lost_one_accepted(client, headers, tmp_path)
    response = client.post(
        f"/api/planning_jobs/{job['id']}/promote_single",
        headers=headers,
        json={"expected_state_version": job["state_version"]},
    )
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_error"
    # The cross-field validator rejects the request with confirm_degraded
    # unset. FastAPI reports the constraint on the body level; the test only
    # needs to confirm the explicit consent gate is enforced.


def test_promote_rejects_other_quorum_loss_codes(authenticated, tmp_path):
    client, headers = authenticated
    job = _quorum_lost_one_accepted(client, headers, tmp_path)
    with client.app.state.session_factory() as session:
        row = session.get(PlanningJob, job["id"])
        row.last_error_json = json.dumps({"code": "merge_unavailable"})
        session.commit()
    refreshed = client.get(f"/api/planning_jobs/{job['id']}").json()
    response = client.post(
        f"/api/planning_jobs/{job['id']}/promote_single",
        headers=headers,
        json={
            "expected_state_version": refreshed["state_version"],
            "confirm_degraded": True,
        },
    )
    assert response.status_code == 409
    assert response.json()["code"] == "planning_quorum_required"


def test_promote_rejects_when_no_draft_was_accepted(authenticated, tmp_path):
    client, headers = authenticated
    *_, payload = setup(client, headers, tmp_path)
    job = create(client, headers, payload)
    failed = dispatch(
        client,
        job,
        responses=[
            {"node_id": "council_participant_0", "outcome": "confirmed_failure"},
            {"node_id": "council_participant_1", "outcome": "confirmed_failure"},
        ],
    )
    assert failed["state"] == "failed" and failed["last_error"]["code"] == "council_quorum_missing"
    assert failed["n_participants_actual"] == 0
    response = client.post(
        f"/api/planning_jobs/{job['id']}/promote_single",
        headers=headers,
        json={
            "expected_state_version": failed["state_version"],
            "confirm_degraded": True,
        },
    )
    assert response.status_code == 409
    assert response.json()["code"] == "planning_single_member_count"


def test_promote_rejects_state_version_conflict_and_terminal_states(authenticated, tmp_path):
    client, headers = authenticated
    job = _quorum_lost_one_accepted(client, headers, tmp_path)
    response = client.post(
        f"/api/planning_jobs/{job['id']}/promote_single",
        headers=headers,
        json={"expected_state_version": job["state_version"] + 99, "confirm_degraded": True},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "planning_state_version_invalid"

    # Promote to a non-failed state and verify the new state rejects promotion.
    client.post(
        f"/api/planning_jobs/{job['id']}/promote_single",
        headers=headers,
        json={"expected_state_version": job["state_version"], "confirm_degraded": True},
    )
    refreshed = client.get(f"/api/planning_jobs/{job['id']}").json()
    response = client.post(
        f"/api/planning_jobs/{job['id']}/promote_single",
        headers=headers,
        json={
            "expected_state_version": refreshed["state_version"],
            "confirm_degraded": True,
        },
    )
    assert response.status_code == 409
    assert response.json()["code"] == "planning_state_invalid"


def test_promote_with_questions_transitions_to_needs_answers(authenticated, tmp_path):
    client, headers = authenticated
    job = _quorum_lost_one_accepted(client, headers, tmp_path, with_questions=True)
    response = client.post(
        f"/api/planning_jobs/{job['id']}/promote_single",
        headers=headers,
        json={
            "expected_state_version": job["state_version"],
            "confirm_degraded": True,
        },
    )
    assert response.status_code == 200, response.text
    promoted = response.json()
    assert promoted["state"] == "needs_answers"
    refreshed = client.get(f"/api/planning_jobs/{job['id']}").json()
    revision = refreshed["revisions"][-1]
    assert revision["author"] == "single_member"
    assert revision["readiness"] == "needs_answers"
    assert len(revision["questions"]) == 1


def test_promote_records_pinned_accepted_member_in_event(authenticated, tmp_path):
    client, headers = authenticated
    job = _quorum_lost_one_accepted(client, headers, tmp_path)
    response = client.post(
        f"/api/planning_jobs/{job['id']}/promote_single",
        headers=headers,
        json={
            "expected_state_version": job["state_version"],
            "confirm_degraded": True,
        },
    )
    assert response.status_code == 200, response.text
    promoted = response.json()
    with client.app.state.session_factory() as session:
        accepted_member = session.scalar(
            select(PlanningMember).where(
                PlanningMember.job_id == job["id"], PlanningMember.status == "succeeded"
            )
        )
        assert accepted_member is not None
        assert promoted["accepted_member_id"] == accepted_member.id
        # Failed drafts remain in the table for diagnostics.
        failed_count = session.scalar(
            select(PlanningDraft).where(
                PlanningDraft.job_id == job["id"], PlanningDraft.accepted.is_(False)
            )
        )
        assert failed_count is not None
