"""Council through the actual worker and local HTTP, without paid models."""

import json
import os
import time

import pytest
from council_support import create, document, setup
from sqlalchemy import select
from test_stage7_real_llm_run import Handler, fake_worker, provider_server  # noqa: F401

from agents_ide.persistence.models import PlanningAttempt, PlanningJob, PlanningMember


@pytest.mark.skipif(os.name != "nt", reason="DPAPI credentials require Windows")
def test_real_worker_pins_candidates_credentials_and_merges_full_drafts(
    fake_worker,  # noqa: F811
    authenticated,
    provider_server,  # noqa: F811
    tmp_path,
):
    _, base_url = provider_server
    client, headers = authenticated
    secret_a, secret_b = "council-synthetic-key-A", "council-synthetic-key-B"
    _, _, first, payload = setup(client, headers, tmp_path, base_url=base_url, secret=secret_a)
    second = client.post(
        "/api/connections",
        headers=headers,
        json={"name": "second", "base_url": base_url, "secret": secret_b},
    ).json()
    group = client.post(
        "/api/model_groups/llm",
        headers=headers,
        json={
            "name": "council fallbacks",
            "members": [
                {"provider_connection_id": first["id"], "model_id": "unavailable"},
                {"provider_connection_id": second["id"], "model_id": "fallback"},
            ],
        },
    ).json()
    payload["participants"][0]["selection"] = {"kind": "group", "group_id": group["id"]}
    Handler.responses = {
        "unavailable": {"status": 404},
        "fallback": {"content": json.dumps(document("Fallback draft marker"))},
        "model-1": {"content": json.dumps(document("Independent draft marker"))},
        "merger": {"content": json.dumps(document("Complete agreed plan"))},
    }
    job = create(client, headers, payload)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        job = client.get(f"/api/planning_jobs/{job['id']}").json()
        if job["state"] not in {"drafting", "merging"}:
            break
        time.sleep(0.1)
    assert job["state"] == "ready_for_confirmation", job
    assert job["usage"]["external_calls"] == 4
    assert job["usage"]["tokens_used"] == 27
    assert Handler.authorizations == [
        f"Bearer {key}" for key in (secret_a, secret_b, secret_a, secret_a)
    ]
    assert [body["model"] for body in Handler.bodies] == [
        "unavailable",
        "fallback",
        "model-1",
        "merger",
    ]
    merged = json.dumps(Handler.bodies[-1]["messages"])
    assert "Fallback draft marker" in merged and "Independent draft marker" in merged
    assert secret_a not in json.dumps(job) and secret_b not in json.dumps(job)
    assert any(e["payload"].get("error_code") == "model_not_found" for e in job["events"])


@pytest.mark.skipif(os.name != "nt", reason="DPAPI credentials require Windows")
def test_real_worker_retry_uses_rotated_key_without_repeating_accepted_draft(
    fake_worker,  # noqa: F811
    authenticated,
    provider_server,  # noqa: F811
    tmp_path,
):
    _, base_url = provider_server
    client, headers = authenticated
    old_key, new_key = "council-retry-test-old", "council-retry-test-new"
    _, _, connection, payload = setup(client, headers, tmp_path, base_url=base_url, secret=old_key)
    Handler.responses = {
        "model-0": {"status": 401},
        "model-1": {"content": json.dumps(document("Preserved independent draft"))},
        "merger": {"content": json.dumps(document("Restored final plan"))},
    }
    job = create(client, headers, payload)

    def settled(state):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            value = client.get(f"/api/planning_jobs/{job['id']}").json()
            with client.app.state.session_factory() as session:
                released = session.get(PlanningJob, job["id"]).lease_owner is None
            if value["state"] == state and released:
                return value
            time.sleep(0.1)
        pytest.fail(f"Council did not reach {state}: {value}")

    before = settled("failed")
    saved = next(d for d in before["drafts"] if d["accepted"])
    factory = client.app.state.session_factory
    with factory() as session:
        snapshots = {
            m.id: m.candidates_json
            for m in session.scalars(
                select(PlanningMember).where(PlanningMember.job_id == job["id"])
            )
        }
    changed = client.patch(
        f"/api/connections/{connection['id']}",
        headers=headers,
        json={
            "expected_version": connection["version"],
            "secret": new_key,
        },
    )
    assert changed.status_code == 200, changed.text
    Handler.responses["model-0"] = {"content": json.dumps(document("Recovered participant"))}
    response = client.post(
        f"/api/planning_jobs/{job['id']}/retry",
        headers=headers,
        json={
            "expected_state_version": before["state_version"],
            "reset_all_failed": True,
            "refresh_credentials": True,
        },
    )
    assert response.status_code == 200, response.text
    after = settled("ready_for_confirmation")
    assert after["usage"]["external_calls"] == 4
    assert after["started_at"] == before["started_at"]
    assert saved in after["drafts"]
    assert [b["model"] for b in Handler.bodies] == ["model-0", "model-1", "model-0", "merger"]
    assert Handler.authorizations == [f"Bearer {k}" for k in (old_key, old_key, new_key, new_key)]
    assert old_key not in json.dumps(after) and new_key not in json.dumps(after)
    assert "secret_reference" not in json.dumps(after)
    event = next(e for e in after["events"] if e["type"] == "planning.retried")
    assert len(event["payload"]["access_changes"]) == 2  # failed participant and pending merger
    with factory() as session:
        assert {
            m.id: m.candidates_json
            for m in session.scalars(
                select(PlanningMember).where(PlanningMember.job_id == job["id"])
            )
        } == snapshots
        attempts = list(
            session.scalars(
                select(PlanningAttempt)
                .where(PlanningAttempt.member_id == before["members"][0]["id"])
                .order_by(PlanningAttempt.attempt_index)
            )
        )
        assert [json.loads(a.candidate_json)["connection"]["version"] for a in attempts] == [1, 2]
