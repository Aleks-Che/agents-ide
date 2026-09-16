import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from council_support import create, dispatch, document, setup
from sqlalchemy import select

from agents_ide.adapters.base import ExternalOutcome, LLMResult
from agents_ide.domain.common import utc_now
from agents_ide.engine.planning_worker import _finish, _prepare, claim_planning_job
from agents_ide.errors import AppError
from agents_ide.persistence.models import PlanningAttempt, PlanningJob, PlanningMember


def failed_job(client, headers, tmp_path, **options):
    *_, payload = setup(client, headers, tmp_path, **options)
    job = create(client, headers, payload)
    return dispatch(
        client,
        job,
        responses=[
            {"node_id": "council_participant_0", "outcome": "confirmed_failure"},
            {
                "node_id": "council_participant_1",
                "raw_text": json.dumps(document("Keep this draft")),
            },
        ],
    )


def retry(client, headers, job, **options):
    return client.post(
        f"/api/planning_jobs/{job['id']}/retry",
        headers=headers,
        json={
            "expected_state_version": job["state_version"],
            "reset_all_failed": True,
            **options,
        },
    )


def test_retry_preserves_accepted_draft_context_attempts_and_clock(authenticated, tmp_path):
    client, headers = authenticated
    job = failed_job(client, headers, tmp_path)
    accepted = next(d for d in job["drafts"] if d["accepted"])
    assert retry(client, headers, job).status_code == 200
    queued = client.get(f"/api/planning_jobs/{job['id']}").json()
    # The draft view includes current member status; its evidence remains intact.
    for old, current in zip(job["drafts"], queued["drafts"], strict=True):
        for key in ("body_text", "content_hash", "accepted", "byte_length", "parse_status"):
            assert current[key] == old[key]
    assert queued["started_at"] == job["started_at"]
    assert queued["read_manifest_hash"] == job["read_manifest_hash"]
    assert queued["usage"] == job["usage"]
    result = dispatch(
        client,
        queued,
        responses=[
            {
                "node_id": "council_participant_0",
                "attempt_index": 2,
                "raw_text": json.dumps(document("Restored")),
            },
            {"node_id": "council_merger", "raw_text": json.dumps(document())},
        ],
    )
    assert result["state"] == "ready_for_confirmation"
    assert result["usage"]["external_calls"] == 4
    assert next(d for d in result["drafts"] if d["member_id"] == accepted["member_id"]) == accepted
    with client.app.state.session_factory() as session:
        assert [
            a.attempt_index
            for a in session.scalars(
                select(PlanningAttempt)
                .where(PlanningAttempt.member_id == job["members"][0]["id"])
                .order_by(PlanningAttempt.attempt_index)
            )
        ] == [1, 2]


def test_merger_retry_does_not_repeat_accepted_participants(authenticated, tmp_path):
    client, headers = authenticated
    *_, payload = setup(client, headers, tmp_path)
    job = dispatch(
        client,
        create(client, headers, payload),
        responses=[
            *[
                {"node_id": f"council_participant_{i}", "raw_text": json.dumps(document())}
                for i in range(2)
            ],
            {"node_id": "council_merger", "outcome": "confirmed_failure"},
        ],
    )
    # This exercises the succeeded guard in a failed job, unlike a state-only rejection.
    invalid = retry(client, headers, job, reset_all_failed=False, reset_member_indices=[0, 2])
    assert invalid.status_code == 409 and invalid.json()["code"] == "planning_retry_invalid"
    response = retry(client, headers, job)
    assert response.json()["reset_member_ids"] == [job["members"][2]["id"]]
    result = dispatch(
        client,
        job,
        responses=[
            {"node_id": "council_merger", "attempt_index": 2, "raw_text": json.dumps(document())},
        ],
    )
    assert result["state"] == "ready_for_confirmation"
    assert result["usage"]["external_calls"] == 4 and result["drafts"] == job["drafts"]


@pytest.mark.parametrize(
    "change,code",
    [
        ("time", "planning_deadline_exceeded"),
        ("calls", "planning_budget_exhausted"),
        ("legacy", "legacy_planning_unverifiable"),
        ("context", "context_changed"),
    ],
)
def test_retry_cannot_reset_budget_or_bypass_integrity(authenticated, tmp_path, change, code):
    client, headers = authenticated
    job = failed_job(client, headers, tmp_path)
    with client.app.state.session_factory() as session:
        row = session.get(PlanningJob, job["id"])
        if change == "time":
            row.started_at = utc_now() - 1000
        elif change == "calls":
            row.usage_json = '{"external_calls":8}'
        elif change == "legacy":
            row.request_hash = None
        else:
            row.context_snapshot_json = "{}"
        session.commit()
    before = client.get(f"/api/planning_jobs/{job['id']}").json()
    response = retry(client, headers, job)
    assert response.status_code == 409 and response.json()["code"] == code
    assert client.get(f"/api/planning_jobs/{job['id']}").json() == before


def test_retry_fences_late_worker_and_requires_unknown_consent(authenticated, tmp_path):
    client, headers = authenticated
    *_, payload = setup(client, headers, tmp_path)
    job = create(client, headers, payload)
    factory = client.app.state.session_factory
    claim = claim_planning_job(factory, "old-worker", job["id"])
    attempt_id, _, _ = _prepare(factory, claim)
    with factory() as session:
        row = session.get(PlanningJob, job["id"])
        row.state = "failed"
        row.state_version += 1
        session.commit()
    failed = client.get(f"/api/planning_jobs/{job['id']}").json()
    assert retry(client, headers, failed, acknowledge_unknown_result=True).json()["code"] == (
        "planning_retry_worker_active"
    )
    with factory() as session:
        session.get(PlanningJob, job["id"]).lease_expires_at = 0
        session.commit()
    denied = retry(client, headers, failed)
    assert denied.status_code == 409
    assert denied.json()["code"] == "planning_retry_unknown_requires_ack"
    assert retry(client, headers, failed, acknowledge_unknown_result=True).status_code == 200
    with pytest.raises(AppError, match="ownership lost"):
        _finish(
            factory,
            claim,
            attempt_id,
            LLMResult(ExternalOutcome.SUCCEEDED, json.dumps(document("Stale")), None, None),
        )
    with factory() as session:
        assert session.get(PlanningAttempt, attempt_id).outcome == "unknown"
        assert session.get(PlanningJob, job["id"]).generation > claim.generation
    result = dispatch(
        client,
        job,
        responses=[
            {
                "node_id": "council_participant_0",
                "attempt_index": 2,
                "raw_text": json.dumps(document("Explicitly repeated")),
            },
            {"node_id": "council_participant_1", "raw_text": json.dumps(document())},
            {"node_id": "council_merger", "raw_text": json.dumps(document())},
        ],
    )
    assert result["state"] == "ready_for_confirmation"
    assert result["usage"]["external_calls"] == 4
    event = next(e for e in result["events"] if e["type"] == "planning.retried")
    assert event["payload"]["acknowledged_unknown_members"] == [job["members"][0]["id"]]


def test_retry_does_not_grant_another_format_repair(authenticated, tmp_path):
    client, headers = authenticated
    *_, payload = setup(client, headers, tmp_path)
    job = dispatch(
        client,
        create(client, headers, payload),
        responses=[
            *[
                {"node_id": "council_participant_0", "attempt_index": i, "raw_text": "Malformed"}
                for i in (1, 2)
            ],
            {"node_id": "council_participant_1", "raw_text": json.dumps(document())},
        ],
    )
    assert retry(client, headers, job).status_code == 200
    result = dispatch(
        client,
        job,
        responses=[
            {"node_id": "council_participant_0", "attempt_index": 3, "raw_text": "Still malformed"},
        ],
    )
    assert result["state"] == "failed" and result["usage"]["external_calls"] == 4
    with client.app.state.session_factory() as session:
        assert session.get(PlanningMember, job["members"][0]["id"]).draft_revision == 1


@pytest.mark.parametrize(
    "options,status",
    [
        ({"reset_member_indices": [-1], "reset_all_failed": False}, 422),
        ({"reset_member_indices": [5], "reset_all_failed": False}, 422),
        ({"reset_member_indices": [0, 0], "reset_all_failed": False}, 422),
        ({"reset_member_indices": [0]}, 422),
        ({"reset_member_indices": [4], "reset_all_failed": False}, 409),
        ({"reset_member_indices": [2], "reset_all_failed": False}, 409),
    ],
)
def test_retry_rejects_invalid_slot_selection_atomically(authenticated, tmp_path, options, status):
    client, headers = authenticated
    job = failed_job(client, headers, tmp_path)
    response = retry(client, headers, job, **options)
    assert response.status_code == status
    assert client.get(f"/api/planning_jobs/{job['id']}").json() == job


def test_duplicate_retry_is_serialized_and_csrf_protected(authenticated, tmp_path):
    client, headers = authenticated
    job = failed_job(client, headers, tmp_path)
    assert retry(client, {}, job).status_code == 403
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: retry(client, headers, job), range(2)))
    assert sorted(r.status_code for r in responses) == [200, 409]
    result = client.get(f"/api/planning_jobs/{job['id']}").json()
    assert len([e for e in result["events"] if e["type"] == "planning.retried"]) == 1


def test_credential_refresh_cannot_change_endpoint_or_pinned_candidates(authenticated, tmp_path):
    client, headers = authenticated
    *_, connection, payload = setup(client, headers, tmp_path)
    job = dispatch(
        client,
        create(client, headers, payload),
        responses=[
            {"node_id": "council_participant_0", "outcome": "confirmed_failure"},
            {"node_id": "council_participant_1", "raw_text": json.dumps(document())},
        ],
    )
    changed = client.patch(
        f"/api/connections/{connection['id']}",
        headers=headers,
        json={
            "expected_version": connection["version"],
            "base_url": "http://127.0.0.1:8/v1",
        },
    )
    assert changed.status_code == 200, changed.text
    response = retry(client, headers, job, refresh_credentials=True)
    assert response.status_code == 409
    assert response.json()["code"] == "planning_retry_endpoint_changed"
    assert client.get(f"/api/planning_jobs/{job['id']}").json() == job


def test_group_retry_restarts_pinned_order_without_repeating_accepted_model(
    authenticated, tmp_path
):
    client, headers = authenticated
    *_, connection, payload = setup(client, headers, tmp_path)
    group = client.post(
        "/api/model_groups/llm",
        headers=headers,
        json={
            "name": "Retry pinned group",
            "members": [
                {"provider_connection_id": connection["id"], "model_id": model}
                for model in ("a", "b")
            ],
        },
    ).json()
    for member in payload["participants"][:2]:
        member["selection"] = {"kind": "group", "group_id": group["id"]}
    unavailable = {"outcome": "unavailable", "retry_safety": "safe", "no_effect": True}
    job = dispatch(
        client,
        create(client, headers, payload),
        responses=[
            {"node_id": "council_participant_0", **unavailable},
            {
                "node_id": "council_participant_0",
                "attempt_index": 2,
                "raw_text": json.dumps(document("Accepted b")),
            },
            {"node_id": "council_participant_1", **unavailable},
        ],
    )
    assert job["state"] == "failed" and job["usage"]["external_calls"] == 3
    changed = client.put(
        f"/api/model_groups/{group['id']}/llm/members",
        headers=headers,
        json={
            "expected_revision": group["revision"],
            "members": [{"provider_connection_id": connection["id"], "model_id": "new-model"}],
        },
    )
    assert changed.status_code == 200
    assert retry(client, headers, job, refresh_credentials=True).status_code == 200
    result = dispatch(
        client,
        job,
        responses=[
            {
                "node_id": "council_participant_1",
                "attempt_index": 2,
                "raw_text": json.dumps(document("Recovered a")),
            },
            {"node_id": "council_merger", "raw_text": json.dumps(document())},
        ],
    )
    assert result["state"] == "ready_for_confirmation"
    assert result["usage"]["external_calls"] == 5
    assert [m["selected_model_id"] for m in result["members"][:2]] == ["b", "a"]
    assert result["drafts"][0] == job["drafts"][0]


def test_retry_subset_must_resolve_unknown_outcomes(authenticated, tmp_path):
    client, headers = authenticated
    *_, payload = setup(client, headers, tmp_path)
    job = create(client, headers, payload)
    job = dispatch(
        client,
        job,
        responses=[
            {"node_id": "council_participant_0", "outcome": "unknown"},
        ],
    )
    with client.app.state.session_factory() as session:
        other = session.get(PlanningMember, job["members"][1]["id"])
        other.status = "failed"
        session.commit()
    before = client.get(f"/api/planning_jobs/{job['id']}").json()
    response = retry(
        client,
        headers,
        before,
        reset_all_failed=False,
        reset_member_indices=[1],
        acknowledge_unknown_result=True,
    )
    assert response.status_code == 409
    assert response.json()["code"] == "planning_retry_unresolved"
    assert client.get(f"/api/planning_jobs/{job['id']}").json() == before
