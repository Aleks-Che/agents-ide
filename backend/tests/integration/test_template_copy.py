"""Copies preserve saved content and don't share mutable template state."""


def test_copy_preserves_draft_latest_version_and_source(authenticated):
    client, headers = authenticated
    source = client.post(
        "/api/templates",
        json={"name": "Original", "description": "Keep description"},
        headers=headers,
    ).json()
    url = f"/api/templates/{source['id']}"
    graph = {
        "nodes": [{"id": "start", "type": "Start"}, {"id": "end", "type": "End"}],
        "edges": [{"from": "start", "to": "end"}],
    }
    for inputs in [{"task": "first"}, {"task": "latest"}]:
        response = client.post(
            f"{url}/versions", json={"graph": graph, "inputs": inputs}, headers=headers
        )
        assert response.status_code == 201, response.text
    latest = response.json()
    draft = {"graph": graph, "inputs": {"task": "unpublished"}, "origin": "imported"}
    response = client.put(
        f"{url}/draft", json={**draft, "expected_version": source["version"]}, headers=headers
    )
    assert response.status_code == 200, response.text
    source = response.json()
    response = client.post(
        f"{url}/copy", json={"name": "Copy", "expected_version": source["version"]}, headers=headers
    )
    assert response.status_code == 201, response.text
    copied = response.json()
    assert copied["id"] != source["id"]
    assert copied["name"] == "Copy"
    assert copied["description"] == source["description"]
    assert copied["draft"] == source["draft"]
    versions = client.get(f"/api/templates/{copied['id']}/versions").json()
    assert len(versions) == 1
    assert versions[0]["id"] != latest["id"]
    assert versions[0]["version_number"] == 1
    for field in [
        "graph",
        "inputs",
        "settings",
        "required_features",
        "origin",
        "execution_hash",
        "policy_hash",
    ]:
        assert versions[0][field] == latest[field]
    response = client.put(
        f"/api/templates/{copied['id']}/draft",
        json={**draft, "inputs": {"task": "changed copy"}, "expected_version": copied["version"]},
        headers=headers,
    )
    assert response.status_code == 200
    assert client.get(url).json() == source
    assert len(client.get(f"{url}/versions").json()) == 1


def test_copy_empty_template_rejects_conflicts_and_archived_source(authenticated):
    client, headers = authenticated
    source = client.post("/api/templates", json={"name": "Empty"}, headers=headers).json()
    url = f"/api/templates/{source['id']}"
    body = {"name": "Copy", "expected_version": source["version"]}
    assert client.post(f"{url}/copy", json=body).status_code == 403
    response = client.post(f"{url}/copy", json=body, headers=headers)
    assert response.status_code == 201
    copied = response.json()
    assert copied["draft"] == source["draft"]
    assert client.get(f"/api/templates/{copied['id']}/versions").json() == []
    count = len(client.get("/api/templates?include_archived=true").json())
    response = client.post(f"{url}/copy", json={**body, "expected_version": 99}, headers=headers)
    assert response.status_code == 409
    assert response.json()["code"] == "version_conflict"
    archived = client.post(
        f"{url}/archive?expected_version={source['version']}", headers=headers
    ).json()
    response = client.post(
        f"{url}/copy", json={**body, "expected_version": archived["version"]}, headers=headers
    )
    assert response.status_code == 409
    assert response.json()["code"] == "template_archived"
    assert len(client.get("/api/templates?include_archived=true").json()) == count


def test_system_template_copy_is_editable_without_changing_builtin(authenticated):
    client, headers = authenticated
    preset = client.get("/api/presets").json()[0]
    url = f"/api/templates/{preset['template_id']}"
    source = client.get(url).json()
    response = client.post(
        f"{url}/copy",
        json={"name": "Editable", "expected_version": source["version"]},
        headers=headers,
    )
    assert response.status_code == 201
    copied = response.json()
    assert copied["kind"] == "user"
    assert copied["draft"] == source["draft"]
    assert client.get(url).json() == source
