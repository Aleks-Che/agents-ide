"""Installed harnesses share settings while group members select their own models."""

import pytest

from agents_ide.adapters.model_catalog import CatalogModels
from agents_ide.engine import codex_runtime
from agents_ide.services import harness_discovery


@pytest.fixture
def installations(monkeypatch, tmp_path):
    paths = {kind: str(tmp_path / f"{kind}.exe") for kind in ("codex", "opencode")}
    monkeypatch.setattr(harness_discovery, "discover_executables", lambda: paths)
    return paths


def discover(client, headers):
    response = client.post("/api/harnesses/discover", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_discovery_is_idempotent_and_preserves_settings_after_upgrade(
    authenticated, installations, tmp_path
):
    client, headers = authenticated
    first = discover(client, headers)
    assert [h["harness_kind"] for h in first] == ["codex", "opencode"]
    assert first[0]["settings"] == {"permission_mode": "read_only"}
    assert first[1]["settings"] == {"permission_mode": "native"}
    assert discover(client, headers) == first
    installations["codex"] = str(tmp_path / "new/codex.exe")
    changed = discover(client, headers)
    assert changed[0]["id"] == first[0]["id"]
    assert changed[0]["executable_path"] == installations["codex"]
    assert changed[0]["settings"] == first[0]["settings"]
    assert changed[0]["version"] == first[0]["version"] + 1
    assert len(client.get("/api/harness_profiles").json()) == 2


def test_discovery_reuses_existing_reference_without_deleting_legacy_profiles(
    authenticated, installations
):
    client, headers = authenticated
    profiles = [
        client.post(
            "/api/harness_profiles",
            headers=headers,
            json={
                "name": name,
                "harness_kind": "codex",
                "executable_path": installations["codex"],
                "settings": {"permission_mode": "read_only"},
            },
        ).json()
        for name in ("Existing", "Existing copy")
    ]
    assert discover(client, headers)[0]["id"] == profiles[0]["id"]
    assert not client.get(f"/api/harness_profiles/{profiles[1]['id']}").json()["archived"]


def test_no_installations_has_no_placeholder_profiles(authenticated, installations):
    client, headers = authenticated
    installations.clear()
    assert discover(client, headers) == []
    assert client.get("/api/harness_profiles").json() == []


def test_legacy_harness_without_mode_gets_supported_default(authenticated, installations):
    client, headers = authenticated
    legacy = client.post(
        "/api/harness_profiles",
        headers=headers,
        json={
            "name": "Legacy",
            "harness_kind": "codex",
            "settings": {},
        },
    ).json()
    current = discover(client, headers)[0]
    assert current["id"] == legacy["id"]
    assert current["settings"]["permission_mode"] == "read_only"
    assert discover(client, headers)[0] == current


@pytest.mark.parametrize("value", [[], {}, 42, True])
def test_default_model_must_be_a_catalog_id(authenticated, installations, value):
    client, headers = authenticated
    harness = discover(client, headers)[0]
    result = client.patch(
        f"/api/harness_profiles/{harness['id']}",
        headers=headers,
        json={
            "expected_version": harness["version"],
            "settings": {"default_model": value},
        },
    )
    assert result.status_code == 422
    assert result.json()["code"] == "harness_model_unavailable"


def test_native_catalog_default_and_multiple_models_share_one_harness(
    authenticated, installations, monkeypatch
):
    client, headers = authenticated
    calls = []

    def probe(*args):
        calls.append(args)
        return "codex-test", CatalogModels(
            {
                "model-a": {"source": "native_catalog", "reasoning_efforts": ["low", "high"]},
                "model-b": {"source": "native_catalog", "reasoning_efforts": ["high"]},
            }
        )

    monkeypatch.setattr(codex_runtime, "probe_executable", probe)
    harness = discover(client, headers)[0]
    base = f"/api/harness_profiles/{harness['id']}"
    catalog = client.post(base + "/models/refresh", headers=headers)
    assert catalog.status_code == 200, catalog.text
    assert catalog.json()["status"] == "fresh"
    assert [m["id"] for m in catalog.json()["models"]] == ["model-a", "model-b"]
    harness = client.get(base).json()
    update = {
        "expected_version": harness["version"],
        "settings": {**harness["settings"], "default_model": "model-a"},
    }
    saved = client.patch(base, headers=headers, json=update)
    assert saved.status_code == 200, saved.text
    assert saved.json()["catalog_models"] == ["model-a", "model-b"]
    assert client.post(base + "/models/refresh", headers=headers).status_code == 200
    assert len(calls) == 1
    group = client.post(
        "/api/model_groups/agent",
        headers=headers,
        json={
            "name": "One harness - two models",
            "members": [
                {"harness_profile_id": harness["id"], "model_id": model}
                for model in ("model-a", "model-b")
            ],
        },
    )
    assert group.status_code == 201, group.text
    update["expected_version"] = saved.json()["version"]
    update["settings"]["default_model"] = "model-b"
    assert client.patch(base, headers=headers, json=update).status_code == 200
    current_group = client.get(f"/api/model_groups/{group.json()['id']}").json()
    assert [m["model_id"] for m in current_group["members"]] == ["model-a", "model-b"]
    assert {m["harness_profile_id"] for m in current_group["members"]} == {harness["id"]}
    assert len(discover(client, headers)) == 2
    update["expected_version"] += 1
    update["settings"]["default_model"] = "not-in-native-catalog"
    rejected = client.patch(base, headers=headers, json=update)
    assert rejected.status_code == 422
    assert rejected.json()["code"] == "harness_model_unavailable"


def test_failed_catalog_refresh_is_visible_and_can_retry(authenticated, installations, monkeypatch):
    client, headers = authenticated
    harness = discover(client, headers)[0]
    base = f"/api/harness_profiles/{harness['id']}"

    def fail(*args):
        raise OSError("synthetic failure")

    monkeypatch.setattr(codex_runtime, "probe_executable", fail)
    failed = client.post(base + "/models/refresh", headers=headers)
    assert failed.status_code == 422
    assert failed.json()["code"] == "harness_catalog_unavailable"
    assert client.get(base).json()["last_test_status"] == "failed"
    monkeypatch.setattr(
        codex_runtime, "probe_executable", lambda *args: ("test", CatalogModels({}))
    )
    empty = client.post(base + "/models/refresh", headers=headers)
    assert empty.status_code == 200
    assert empty.json()["models"] == []


def test_refresh_reloads_after_native_auth_changes(
    authenticated, installations, monkeypatch, tmp_path
):
    client, headers = authenticated
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    calls = []

    def probe(*args):
        calls.append(args)
        return "test", CatalogModels({f"model-{len(calls)}": {}})

    monkeypatch.setattr(codex_runtime, "probe_executable", probe)
    harness = discover(client, headers)[0]
    endpoint = f"/api/harness_profiles/{harness['id']}/models/refresh"
    assert client.post(endpoint, headers=headers).json()["models"][0]["id"] == "model-1"
    (tmp_path / "auth.json").write_text("synthetic-changed-auth")
    result = client.post(endpoint, headers=headers)
    assert result.status_code == 200, result.text
    assert result.json()["models"][0]["id"] == "model-2"
    assert "synthetic" not in result.text


@pytest.mark.parametrize("force", [False, True])
def test_restart_recovers_catalog_after_installed_binary_moves(
    authenticated, installations, monkeypatch, tmp_path, force
):
    from pathlib import Path

    from test_stage6b_review import seed

    client, headers = authenticated
    calls = []

    def probe(executable, *_):
        calls.append(executable)
        if not Path(executable).is_file():
            raise FileNotFoundError(executable)
        return "codex-test", CatalogModels(
            {"gpt-5.6-sol": {"source": "native_catalog", "reasoning_efforts": ["high"]}}
        )

    monkeypatch.setattr(codex_runtime, "probe_executable", probe)
    payload = seed(authenticated, tmp_path, params={"reasoning_effort": "high"})
    profile = client.get("/api/harness_profiles").json()[0]
    base = f"/api/harness_profiles/{profile['id']}"
    old_binary = tmp_path / "old-codex.exe"
    old_binary.touch()
    assert (
        client.patch(
            base,
            headers=headers,
            json={"expected_version": profile["version"], "executable_path": str(old_binary)},
        ).status_code
        == 200
    )
    assert client.post(base + "/models/refresh", headers=headers).status_code == 200
    response = client.post("/api/runs", headers=headers, json=payload)
    assert response.status_code == 201, response.text
    run = response.json()
    profile = client.get(base).json()

    old_binary.unlink()
    new_binary = Path(installations["codex"])
    new_binary.touch()
    restart_url = f"/api/runs/{run['id']}/restart"
    body = {"command_id": "restart-after-upgrade", "expected_state_version": run["state_version"]}
    rejected = client.post(restart_url, headers=headers, json=body)
    assert rejected.status_code == 422
    assert "harness_catalog_unverified" in rejected.text
    assert len(client.get("/api/runs").json()) == 1

    refreshed = client.post(base + f"/models/refresh?force={str(force).lower()}", headers=headers)
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["status"] == "fresh"
    current = client.get(base).json()
    assert current["executable_path"] == str(new_binary)
    assert current["settings"] == profile["settings"]
    assert current["id"] == profile["id"]
    assert calls == [str(old_binary), str(new_binary)]

    restarted = client.post(restart_url, headers=headers, json=body)
    assert restarted.status_code == 201, restarted.text
    assert restarted.json()["id"] != run["id"]
    assert (
        client.post(restart_url, headers=headers, json=body).json()["id"] == restarted.json()["id"]
    )
    assert len(client.get("/api/runs").json()) == 2
    assert client.get(f"/api/runs/{run['id']}").json()["snapshot_hash"] == run["snapshot_hash"]


def test_refresh_keeps_existing_custom_executable(
    authenticated, installations, monkeypatch, tmp_path
):
    client, headers = authenticated
    custom_binary = tmp_path / "custom-codex.exe"
    custom_binary.touch()
    profile = client.post(
        "/api/harness_profiles",
        headers=headers,
        json={"name": "Custom", "harness_kind": "codex", "executable_path": str(custom_binary)},
    ).json()
    calls = []

    def probe(executable, *_):
        calls.append(executable)
        return "codex-test", CatalogModels({})

    monkeypatch.setattr(codex_runtime, "probe_executable", probe)
    response = client.post(
        f"/api/harness_profiles/{profile['id']}/models/refresh?force=true", headers=headers
    )
    assert response.status_code == 200, response.text
    assert calls == [str(custom_binary)]


def test_npm_wrapper_resolves_native_binary_without_running_it(tmp_path, monkeypatch):
    wrapper = tmp_path / "opencode.cmd"
    wrapper.write_text("must never execute")
    native = tmp_path / "node_modules/opencode-ai/bin/opencode.exe"
    native.parent.mkdir(parents=True)
    native.touch()
    monkeypatch.setattr(harness_discovery, "native_executable", lambda path: path == native)
    assert harness_discovery.resolve_installation("opencode", wrapper) == native.resolve()
    assert harness_discovery.resolve_installation("codex", wrapper) is None


def test_path_installation_is_preferred(monkeypatch, tmp_path):
    binary = tmp_path / "codex.exe"
    binary.touch()
    monkeypatch.setattr(
        harness_discovery.shutil, "which", lambda kind: str(binary) if kind == "codex" else None
    )
    monkeypatch.setattr(harness_discovery, "native_executable", lambda path: path == binary)
    assert harness_discovery.discover_executables() == {"codex": str(binary.resolve())}
