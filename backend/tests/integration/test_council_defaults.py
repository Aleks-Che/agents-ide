from council_support import setup
from test_stage2a_model_groups import _harness_profile, _provider
from test_stage9a_harness import create_simulated


def test_saved_council_revision_validation_and_new_job(authenticated, tmp_path):
    client, headers = authenticated
    endpoint = "/api/settings/planning-council"
    assert client.get(endpoint).json() == {"participants": [], "revision": 0}
    profile = _harness_profile(client, headers, "Council agent")
    connection = _provider(client, headers, "Council LLM")
    group = client.post(
        "/api/model_groups/llm",
        headers=headers,
        json={
            "name": "Council group",
            "members": [
                {"provider_connection_id": connection["id"], "model_id": "group-model"},
            ],
        },
    ).json()
    specs = [
        {
            "role": "participant",
            "selection": {
                "kind": "direct",
                "harness_profile_id": profile["id"],
                "model_id": "agent-model",
            },
        },
        {"role": "participant", "selection": {"kind": "group", "group_id": group["id"]}},
        {
            "role": "merger",
            "selection": {
                "kind": "direct",
                "provider_connection_id": connection["id"],
                "model_id": "merger",
            },
        },
    ]
    payload = {"revision": 0, "participants": specs}
    assert client.put(endpoint, json=payload).status_code == 403
    response = client.put(endpoint, headers=headers, json=payload)
    assert response.status_code == 200, response.text
    stored = response.json()
    assert stored["revision"] == 1
    assert client.get(endpoint).json() == stored
    assert client.put(endpoint, headers=headers, json=payload).status_code == 409
    invalid = {"revision": 1, "participants": specs[:2]}
    assert client.put(endpoint, headers=headers, json=invalid).status_code == 422
    assert client.get(endpoint).json() == stored
    *_, job_payload = setup(client, headers, tmp_path)
    # The saved choice is reusable without starting jobs or persisting project-specific data.
    job_payload["participants"] = stored["participants"]
    job = create_simulated(client, job_payload)
    assert [m["selection"]["kind"] for m in job["members"]] == ["direct", "group", "direct"]


def test_atomic_group_save_rolls_back_metadata_when_candidates_invalid(authenticated):
    client, headers = authenticated
    connection = _provider(client, headers, "Atomic")
    member = {"provider_connection_id": connection["id"], "model_id": "old"}
    created = client.post(
        "/api/model_groups/llm",
        headers=headers,
        json={
            "name": "Original",
            "members": [member],
        },
    ).json()
    endpoint = f"/api/model_groups/{created['id']}/llm"
    response = client.patch(
        endpoint,
        headers=headers,
        json={
            "expected_revision": 1,
            "name": "Changed",
            "members": [member, member],
        },
    )
    assert response.status_code == 422
    assert client.get(f"/api/model_groups/{created['id']}").json() == created
    response = client.patch(
        endpoint,
        headers=headers,
        json={
            "expected_revision": 1,
            "name": "Changed",
            "description": "All at once",
            "members": [member, {**member, "model_id": "new"}],
        },
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["revision"] == 2 and result["name"] == "Changed"
    assert result["description"] == "All at once"
    assert [m["model_id"] for m in result["members"]] == ["old", "new"]
