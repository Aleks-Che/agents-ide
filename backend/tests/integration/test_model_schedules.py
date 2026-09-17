import pytest
from council_support import dispatch, setup
from test_stage2a_model_groups import _harness_profile, _provider
from test_stage9a_harness import create_simulated


@pytest.mark.parametrize("kind", ["agent", "llm"])
def test_schedule_survives_edit_copy_export_import_and_delete(authenticated, kind):
    client, headers = authenticated
    resource = (_harness_profile if kind == "agent" else _provider)(client, headers, "scheduled")
    reference = "harness_profile_id" if kind == "agent" else "provider_connection_id"
    schedule = {
        "enabled": True,
        "same_every_day": False,
        "timezone": "Asia/Yekaterinburg",
        "days": [[hour < 8 + day for hour in range(24)] for day in range(7)],
    }
    response = client.post(
        f"/api/model_groups/{kind}",
        headers=headers,
        json={
            "name": "Scheduled",
            "members": [
                {reference: resource["id"], "model_id": "first", "schedule": schedule},
                {reference: resource["id"], "model_id": "second"},
            ],
        },
    )
    assert response.status_code == 201, response.text
    group = response.json()
    assert group["members"][0]["schedule"] == schedule
    assert group["members"][1]["schedule"] is None
    assert client.get(f"/api/model_groups/{group['id']}").json() == group
    schedule["enabled"] = False
    members = [
        {
            "id": member["id"],
            reference: resource["id"],
            "model_id": member["model_id"],
            "schedule": schedule if i == 0 else None,
        }
        for i, member in enumerate(group["members"])
    ]
    response = client.put(
        f"/api/model_groups/{group['id']}/{kind}/members",
        headers=headers,
        json={"expected_revision": group["revision"], "members": members},
    )
    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated["members"][0]["revision"] == group["members"][0]["revision"] + 1
    assert updated["members"][1]["revision"] == group["members"][1]["revision"]
    copied = client.post(
        f"/api/model_groups/{group['id']}/copy",
        headers=headers,
        json={
            "expected_revision": updated["revision"],
            "name": "Copy",
        },
    )
    assert copied.status_code == 201, copied.text
    assert copied.json()["members"][0]["schedule"] == schedule
    exported = client.get(f"/api/model_groups/{group['id']}/export").json()
    assert exported["members"][0]["schedule"] == schedule
    imported = client.post(
        "/api/model_groups/import",
        headers=headers,
        json={
            "definition": exported,
            "name": "Imported",
            "resource_bindings": {resource["id"]: resource["id"]},
        },
    )
    assert imported.status_code == 201, imported.text
    assert imported.json()["members"][0]["schedule"] == schedule
    deleted = client.delete(
        f"/api/model_groups/{group['id']}/members/{group['members'][1]['id']}",
        params={"expected_revision": updated["revision"]},
        headers=headers,
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["members"][0]["schedule"] == schedule
    bad = client.post(
        f"/api/model_groups/{kind}",
        headers=headers,
        json={
            "name": "Invalid",
            "members": [
                {
                    reference: resource["id"],
                    "model_id": "first",
                    "schedule": {**schedule, "days": [[True] * 24]},
                }
            ],
        },
    )
    assert bad.status_code == 422


@pytest.mark.parametrize("kind", ["agent", "llm"])
def test_planning_skips_scheduled_candidate(authenticated, tmp_path, kind):
    client, headers = authenticated
    _, _, connection, payload = setup(client, headers, tmp_path)
    resource = (
        _harness_profile(client, headers, "Scheduled council") if kind == "agent" else connection
    )
    reference = "harness_profile_id" if kind == "agent" else "provider_connection_id"
    response = client.post(
        f"/api/model_groups/{kind}",
        headers=headers,
        json={
            "name": "Off peak council",
            "members": [
                {
                    reference: resource["id"],
                    "model_id": "peak",
                    "schedule": {
                        "enabled": True,
                        "same_every_day": True,
                        "timezone": "UTC",
                        "days": [[False] * 24],
                    },
                },
                {reference: resource["id"], "model_id": "backup"},
            ],
        },
    )
    assert response.status_code == 201, response.text
    group = response.json()
    payload["participants"][0]["selection"] = {"kind": "group", "group_id": group["id"]}
    job = dispatch(client, create_simulated(client, payload))
    assert job["state"] == "ready_for_confirmation", job
    assert job["members"][0]["selected_model_id"] == "backup"
    assert job["members"][0]["selected_member_id"] == group["members"][1]["id"]
