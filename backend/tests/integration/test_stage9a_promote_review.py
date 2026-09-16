"""Regression coverage for the explicit single-draft recovery boundary."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from council_support import confirm, document
from sqlalchemy import select
from test_stage9a_planning_run import binding
from test_stage9a_promote_single import _quorum_lost_one_accepted

from agents_ide.domain.common import content_hash, utc_now
from agents_ide.engine.planning_worker import PlanningClaim, _owned
from agents_ide.errors import AppError
from agents_ide.persistence.models import PlanningDraft, PlanningJob, PlanningMember, Run


def promote(client, headers, job, **extra):
    return client.post(
        f"/api/planning_jobs/{job['id']}/promote_single",
        headers=headers,
        json={"expected_state_version": job["state_version"], "confirm_degraded": True, **extra},
    )


@pytest.mark.parametrize("consent", [False, 1, "true", None])
def test_promotion_requires_literal_boolean_consent(authenticated, tmp_path, consent):
    client, headers = authenticated
    job = _quorum_lost_one_accepted(client, headers, tmp_path)
    assert promote(client, headers, job, confirm_degraded=consent).status_code == 422
    assert client.get(f"/api/planning_jobs/{job['id']}").json() == job


@pytest.mark.parametrize("fault", ["truncated", "hash", "length", "invalid", "legacy", "context"])
def test_unverifiable_evidence_is_rejected_atomically(authenticated, tmp_path, fault):
    client, headers = authenticated
    job = _quorum_lost_one_accepted(client, headers, tmp_path)
    with client.app.state.session_factory() as session:
        row = session.get(PlanningJob, job["id"])
        draft = session.scalar(
            select(PlanningDraft).where(
                PlanningDraft.job_id == job["id"], PlanningDraft.accepted.is_(True)
            )
        )
        if fault == "truncated":
            draft.body_truncated = True
        elif fault == "hash":
            draft.body_text = json.dumps(document("Unverified replacement"))
        elif fault == "length":
            draft.byte_length += 1
        elif fault == "invalid":
            draft.body_text = "invalid JSON"
            draft.content_hash = content_hash(draft.body_text)
            draft.byte_length = len(draft.body_text)
        elif fault == "legacy":
            row.request_hash = None
        else:
            row.context_snapshot_json = "{}"
        session.commit()
    before = client.get(f"/api/planning_jobs/{job['id']}").json()
    result = promote(client, headers, job)
    assert result.status_code == 409, result.text
    assert client.get(f"/api/planning_jobs/{job['id']}").json() == before


def test_promotion_clears_failure_fences_worker_and_preserves_evidence(authenticated, tmp_path):
    client, headers = authenticated
    job = _quorum_lost_one_accepted(client, headers, tmp_path)
    with client.app.state.session_factory() as session:
        row = session.get(PlanningJob, job["id"])
        row.lease_owner, row.lease_expires_at = "old-worker", utc_now() + 60
        claim = PlanningClaim(row.id, row.lease_owner, row.generation)
        session.commit()
    assert promote(client, headers, job).json()["code"] == "planning_worker_active"
    with client.app.state.session_factory() as session:
        row = session.get(PlanningJob, job["id"])
        row.lease_expires_at = utc_now() - 1
        # No new external call is needed, even when the original budget expired.
        row.started_at = utc_now() - 86400
        session.commit()
    before = client.get(f"/api/planning_jobs/{job['id']}").json()
    assert promote(client, headers, before).status_code == 200
    after = client.get(f"/api/planning_jobs/{job['id']}").json()
    assert after["last_error"] is None and after["finished_at"] is None
    assert after["state_version"] == before["state_version"] + 1
    for key in ("drafts", "members", "usage", "budget", "started_at", "read_manifest_hash"):
        assert after[key] == before[key]
    with client.app.state.session_factory() as session:
        row = session.get(PlanningJob, job["id"])
        assert row.generation == claim.generation + 1 and row.lease_owner is None
        with pytest.raises(AppError, match="ownership lost"):
            _owned(session, claim)


@pytest.mark.parametrize("status", ["running", "unknown", "pending", "succeeded"])
def test_promotion_cannot_bypass_unresolved_or_additional_participant(
    authenticated, tmp_path, status
):
    client, headers = authenticated
    job = _quorum_lost_one_accepted(client, headers, tmp_path)
    with client.app.state.session_factory() as session:
        member = session.scalar(
            select(PlanningMember).where(
                PlanningMember.job_id == job["id"], PlanningMember.slot_index == 0
            )
        )
        member.status = status
        session.commit()
    assert promote(client, headers, job).status_code == 409


@pytest.mark.parametrize("competing_retry", [False, True])
def test_promotion_serializes_with_duplicate_and_retry_and_requires_csrf(
    authenticated, tmp_path, competing_retry
):
    client, headers = authenticated
    job = _quorum_lost_one_accepted(client, headers, tmp_path)
    assert promote(client, {}, job).status_code == 403

    def request(i):
        if competing_retry and i == 1:
            return client.post(
                f"/api/planning_jobs/{job['id']}/retry",
                headers=headers,
                json={
                    "expected_state_version": job["state_version"],
                    "reset_all_failed": True,
                },
            )
        return promote(client, headers, job)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(request, range(2)))
    assert sorted(r.status_code for r in results) == [200, 409]
    job = client.get(f"/api/planning_jobs/{job['id']}").json()
    assert (
        len(
            [
                e
                for e in job["events"]
                if e["type"] in {"planning.single_member_promoted", "planning.retried"}
            ]
        )
        == 1
    )


def test_promoted_plan_answers_confirmation_and_run_keep_degraded_provenance(
    authenticated, tmp_path
):
    client, headers = authenticated
    job = _quorum_lost_one_accepted(client, headers, tmp_path, with_questions=True)
    assert promote(client, headers, job).status_code == 200
    result = client.post(
        f"/api/planning_jobs/{job['id']}/answers",
        headers=headers,
        json={
            "expected_revision": 1,
            "answers": [{"question_id": "q1", "kind": "single", "selected_option_ids": ["a"]}],
        },
    )
    assert result.status_code == 200, result.text
    ready = client.get(f"/api/planning_jobs/{job['id']}").json()
    assert ready["degraded"] and ready["revisions"][-1]["author"] == "user"
    source = {
        "job_id": job["id"],
        "revision_number": 2,
        "confirmation_hash": ready["revisions"][-1]["confirmation_hash"],
    }
    bound = binding(client, headers, job["project_id"])
    start = {
        "project_id": job["project_id"],
        "chat_id": job["chat_id"],
        "binding_id": bound["id"],
        "execution_mode": "real",
        "planning_source": source,
        "idempotency_key": "promoted",
    }
    assert client.post("/api/runs", headers=headers, json=start).status_code == 409
    assert confirm(client, headers, ready) == source
    assert (
        client.post(
            "/api/runs",
            headers=headers,
            json={**start, "inputs": {"planning_provenance": {"degraded": False}}},
        ).json()["code"]
        == "planning_inputs_conflict"
    )
    response = client.post("/api/runs", headers=headers, json=start)
    assert response.status_code == 201, response.text
    run_id = response.json()["id"]
    with client.app.state.session_factory() as session:
        snapshot = json.loads(session.get(Run, run_id).snapshot_json)
        provenance = snapshot["planning_provenance"]
        assert provenance["degraded"] is True and provenance["n_participants_actual"] == 1
        assert provenance["n_participants_requested"] == 2
        assert provenance["revision_author"] == "user"
        assert provenance["source_member_id"] == next(
            d["member_id"] for d in job["drafts"] if d["accepted"]
        )
        assert snapshot["input"]["values"]["planning_provenance"] == provenance
        assert snapshot["planning_source"] == source
        # The public Run snapshot reads the persisted source, not live Council state.
        session.get(PlanningJob, job["id"]).degraded = False
        session.commit()
    assert client.get(f"/api/runs/{run_id}/snapshot").json()["planning_provenance"] == provenance
