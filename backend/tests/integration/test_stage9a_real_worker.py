"""Council through the actual worker and local HTTP, without paid models."""

import json
import os
import time

import pytest
from council_support import create, document, setup
from test_stage7_real_llm_run import Handler, fake_worker, provider_server  # noqa: F401


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
