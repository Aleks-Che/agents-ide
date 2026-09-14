"""Regressions found by reviewing stage 2 against the architecture contracts."""

import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select

from agents_ide.persistence.models import (
    CommandJournal,
    PipelineVersion,
    QueueJob,
    Run,
    RunEvent,
    WorkspaceReservation,
)
from agents_ide.services.runs import reserve_workspace


def setup_run(client, headers, workspace, *, suffix="", chat_id=None):
    workspace.mkdir(parents=True, exist_ok=True)
    project = client.post(
        "/api/projects",
        headers=headers,
        json={
            "name": "project" + suffix,
            "workspace_path": str(workspace),
        },
    ).json()
    template = client.post("/api/templates", headers=headers, json={"name": "tpl" + suffix}).json()
    version = client.post(
        f"/api/templates/{template['id']}/versions",
        headers=headers,
        json={
            "graph": {
                "nodes": [{"id": "start", "type": "Start"}, {"id": "end", "type": "End"}],
                "edges": [{"id": "e1", "from": "start", "to": "end"}],
            },
        },
    ).json()
    binding = client.post(
        f"/api/versions/{version['id']}/bindings",
        headers=headers,
        json={
            "project_id": project["id"],
            "name": "main",
        },
    ).json()
    payload = {
        "project_id": project["id"],
        "binding_id": binding["id"],
        "message": "task",
        "idempotency_key": "key" + suffix,
        "chat_id": chat_id,
    }
    return project, template, version, binding, payload


def test_domain_requires_pairing_and_csrf(client, settings, tmp_path):
    response = client.get("/api/projects")
    assert response.status_code == 401
    code = (settings.data_dir / "runtime/pair-code").read_text()
    client.post("/api/auth/pair", headers={"Origin": settings.origin}, json={"code": code})
    response = client.post(
        "/api/projects",
        headers={"Origin": settings.origin},
        json={
            "name": "forbidden",
            "workspace_path": str(tmp_path),
        },
    )
    assert response.status_code == 403


def test_retry_uses_original_request_after_binding_edit(authenticated, tmp_path):
    client, headers = authenticated
    _, _, _, binding, payload = setup_run(client, headers, tmp_path / "workspace")
    first = client.post("/api/runs", headers=headers, json=payload)
    assert first.status_code == 201
    assert (
        client.patch(
            f"/api/bindings/{binding['id']}",
            headers=headers,
            json={
                "expected_version": 1,
                "limit_overrides": {"max_calls": 50},
            },
        ).status_code
        == 200
    )
    repeat = client.post("/api/runs", headers=headers, json=payload)
    assert repeat.status_code == 201
    assert repeat.json()["id"] == first.json()["id"]


def test_command_type_is_part_of_hash_and_payload_is_durable(authenticated, tmp_path):
    client, headers = authenticated
    *_, payload = setup_run(client, headers, tmp_path / "workspace")
    run = client.post("/api/runs", headers=headers, json=payload).json()
    command = {
        "command_id": "cmd",
        "command_type": "pause",
        "expected_state_version": 0,
        "payload": {"reason": "user request"},
    }
    assert (
        client.post(f"/api/runs/{run['id']}/commands", headers=headers, json=command).status_code
        == 200
    )
    changed = {**command, "command_type": "cancel"}
    assert (
        client.post(f"/api/runs/{run['id']}/commands", headers=headers, json=changed).status_code
        == 409
    )
    with client.app.state.session_factory() as session:
        record = session.scalar(select(CommandJournal))
        assert json.loads(record.payload_json) == command["payload"]


def test_start_rejects_chat_from_another_project(authenticated, tmp_path):
    client, headers = authenticated
    project, *_, payload = setup_run(client, headers, tmp_path / "one")
    chat = client.post(
        f"/api/projects/{project['id']}/chats", headers=headers, json={"title": "chat"}
    ).json()
    *_, other = setup_run(client, headers, tmp_path / "two", suffix="2", chat_id=chat["id"])
    assert client.post("/api/runs", headers=headers, json=other).status_code == 409


def test_parallel_commands_have_one_version_winner(authenticated, tmp_path):
    client, headers = authenticated
    *_, payload = setup_run(client, headers, tmp_path / "workspace")
    run = client.post("/api/runs", headers=headers, json=payload).json()

    def submit(index):
        return client.post(
            f"/api/runs/{run['id']}/commands",
            headers=headers,
            json={
                "command_id": f"command-{index}",
                "command_type": "pause",
                "expected_state_version": 0,
            },
        ).status_code

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(submit, range(4)))
    assert sorted(results) == [200, 409, 409, 409]
    with client.app.state.session_factory() as session:
        assert session.get(Run, run["id"]).state_version == 1


def test_provider_test_must_not_claim_unperformed_success(authenticated):
    client, headers = authenticated
    connection = client.post(
        "/api/connections",
        headers=headers,
        json={
            "name": "offline",
            "base_url": "https://example.invalid",
        },
    ).json()
    result = client.post(f"/api/connections/{connection['id']}/test", headers=headers)
    assert result.status_code == 501
    assert client.get(f"/api/connections/{connection['id']}").json()["last_test_status"] is None


def test_parallel_start_is_atomic_and_idempotent(authenticated, tmp_path):
    client, headers = authenticated
    *_, payload = setup_run(client, headers, tmp_path / "workspace")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(lambda _: client.post("/api/runs", headers=headers, json=payload), range(4))
        )
    assert [result.status_code for result in results] == [201] * 4
    assert len({result.json()["id"] for result in results}) == 1
    with client.app.state.session_factory() as session:
        for model in (Run, QueueJob, RunEvent):
            assert len(session.scalars(select(model)).all()) == 1
        assert not session.scalars(select(WorkspaceReservation)).all()


def test_draft_edit_conflict_and_immutable_publication(authenticated, tmp_path):
    from sqlalchemy.exc import IntegrityError

    client, headers = authenticated
    _, template, version, *rest = setup_run(client, headers, tmp_path / "workspace")
    draft = {"expected_version": 1, "graph": {"nodes": []}}
    saved = client.put(f"/api/templates/{template['id']}/draft", headers=headers, json=draft)
    assert saved.status_code == 200
    assert saved.json()["draft"]["graph"] == draft["graph"]
    assert (
        client.put(
            f"/api/templates/{template['id']}/draft", headers=headers, json=draft
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"/api/templates/{template['id']}/publish",
            headers=headers,
            json={"expected_version": 2},
        ).status_code
        == 422
    )
    draft["expected_version"] = 2
    draft["graph"] = {
        "nodes": [
            {"id": "start", "type": "Start"},
            {"id": "finish", "type": "End"},
        ],
        "edges": [{"id": "e1", "from": "start", "to": "finish"}],
    }
    client.put(f"/api/templates/{template['id']}/draft", headers=headers, json=draft)
    published = client.post(
        f"/api/templates/{template['id']}/publish", headers=headers, json={"expected_version": 3}
    )
    assert published.status_code == 201
    assert published.json()["version_number"] == 2
    assert client.get(f"/api/versions/{version['id']}").json() == version
    with client.app.state.session_factory() as session:
        stored = session.get(PipelineVersion, version["id"])
        stored.graph_json = "{}"
        with pytest.raises(IntegrityError):
            session.commit()


def test_chat_messages_are_snapshotted_and_mutations_versioned(authenticated, tmp_path):
    client, headers = authenticated
    project, *_, payload = setup_run(client, headers, tmp_path / "workspace")
    chat = client.post(
        f"/api/projects/{project['id']}/chats", headers=headers, json={"title": "chat"}
    ).json()
    message = client.post(
        f"/api/chats/{chat['id']}/messages",
        headers=headers,
        json={"role": "user", "content": "original task"},
    ).json()
    payload["chat_id"] = chat["id"]
    run = client.post("/api/runs", headers=headers, json=payload).json()
    assert (
        client.patch(
            f"/api/chats/{chat['id']}",
            headers=headers,
            json={"expected_version": 1, "title": "renamed"},
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/chats/{chat['id']}/archive", headers=headers, json={"expected_version": 2}
        ).status_code
        == 409
    )
    assert (
        client.patch(
            f"/api/messages/{message['id']}",
            headers=headers,
            json={"expected_version": 1, "content": "edited"},
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/messages/{message['id']}/archive", headers=headers, json={"expected_version": 2}
        ).status_code
        == 200
    )
    with client.app.state.session_factory() as session:
        snapshot = json.loads(session.get(Run, run["id"]).snapshot_json)
        assert snapshot["input"]["messages"][0]["content"] == "original task"
    assert client.get(f"/api/runs/{run['id']}").json()["snapshot_hash"] == run["snapshot_hash"]
    assert client.post("/api/runs", headers=headers, json=payload).json()["id"] == run["id"]


@pytest.mark.parametrize("relation", ["same", "nested", "checkout", "independent"])
def test_reservations_respect_workspace_scope(authenticated, tmp_path, relation):
    client, headers = authenticated
    root = tmp_path / "git repo"
    root.mkdir()
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    first_path = root / "one"
    first_path.mkdir()
    *_, first_payload = setup_run(client, headers, first_path)
    first = client.post("/api/runs", headers=headers, json=first_payload).json()
    if relation == "same":
        second_payload = {**first_payload, "idempotency_key": "second"}
    else:
        second_path = first_path / "child" if relation == "nested" else root / "two"
        if relation == "independent":
            second_path = tmp_path / "another checkout"
            second_path.mkdir()
            subprocess.run(["git", "init", str(second_path)], check=True, capture_output=True)
        *_, second_payload = setup_run(client, headers, second_path, suffix="second")
    second = client.post("/api/runs", headers=headers, json=second_payload)
    assert second.status_code == 201  # Conflicts wait in the queue.
    with client.app.state.session_factory() as session:
        assert reserve_workspace(session, first["id"], 1)
        session.commit()
    with client.app.state.session_factory() as session:
        assert reserve_workspace(session, second.json()["id"], 1) == (relation == "independent")
        session.commit()


def test_replaced_workspace_cannot_start(authenticated, tmp_path):
    client, headers = authenticated
    workspace = tmp_path / "workspace"
    *_, payload = setup_run(client, headers, workspace)
    workspace.rename(tmp_path / "old-workspace")
    workspace.mkdir()
    response = client.post("/api/runs", headers=headers, json=payload)
    assert response.status_code == 409
    assert response.json()["code"] == "workspace_conflict"
    with client.app.state.session_factory() as session:
        assert not session.scalars(select(Run)).all()


@pytest.mark.skipif(os.name != "nt", reason="Requires real Windows DPAPI")
def test_snapshot_pins_settings_and_secret_revision(authenticated, tmp_path):
    client, headers = authenticated
    project, template, *_ = setup_run(client, headers, tmp_path / "workspace")
    provider = client.post(
        "/api/connections",
        headers=headers,
        json={
            "name": "provider",
            "base_url": "https://example.invalid/v1",
            "secret": "test-old",
        },
    ).json()
    profile = client.post(
        "/api/harness_profiles",
        headers=headers,
        json={
            "name": "profile",
            "harness_kind": "codex",
            "settings": {"model": "profile-model"},
        },
    ).json()
    graph = {
        "nodes": [
            {"id": "start", "type": "Start"},
            {"id": "agent", "type": "AgentTask", "config": {"role": "dev", "prompt": "fix"}},
            {
                "id": "llm",
                "type": "LLMRequest",
                "config": {
                    "connection_id": provider["id"],
                    "model": "node-model",
                    "prompt": "verify",
                },
            },
            {"id": "end", "type": "End"},
        ],
        "edges": [
            {"id": "e1", "from": "start", "to": "agent"},
            {"id": "e2", "from": "agent", "to": "llm"},
            {"id": "e3", "from": "llm", "to": "end"},
        ],
    }
    version = client.post(
        f"/api/templates/{template['id']}/versions",
        headers=headers,
        json={
            "graph": graph,
            "settings": {
                "branch_policy": "current",
                "model_overrides": {"dev": "template-model"},
                "limit_overrides": {"max_calls": 80},
            },
        },
    ).json()
    binding = client.post(
        f"/api/versions/{version['id']}/bindings",
        headers=headers,
        json={
            "project_id": project["id"],
            "name": "pinned",
            "role_assignments": {"dev": profile["id"]},
            "model_overrides": {"dev": "binding-model"},
        },
    ).json()
    payload = {
        "project_id": project["id"],
        "binding_id": binding["id"],
        "message": "task",
        "idempotency_key": "pinned",
        "overrides": {"model_overrides": {"dev": "run-model"}},
    }
    run = client.post("/api/runs", headers=headers, json=payload)
    assert run.status_code == 201, run.text
    with client.app.state.session_factory() as session:
        snapshot = json.loads(session.get(Run, run.json()["id"]).snapshot_json)
    assert snapshot["resolved_settings"]["branch_policy"] == "current"
    assert snapshot["resolved_settings"]["limit_overrides"]["max_calls"] == 80
    assert snapshot["dependencies"]["nodes"]["agent"]["model"] == "run-model"
    assert snapshot["dependencies"]["nodes"]["llm"]["model"] == "node-model"
    reference = snapshot["dependencies"]["provider_connections"][provider["id"]]["secret_reference"]
    assert "test-old" not in json.dumps(snapshot)
    assert (
        client.patch(
            f"/api/connections/{provider['id']}",
            headers=headers,
            json={
                "expected_version": 1,
                "secret": "test-new",
                "base_url": "https://changed.invalid/v1",
            },
        ).status_code
        == 200
    )
    assert (
        client.patch(
            f"/api/harness_profiles/{profile['id']}",
            headers=headers,
            json={
                "expected_version": 1,
                "settings": {"model": "new-model"},
            },
        ).status_code
        == 200
    )
    assert client.app.state.secrets.get(reference) == "test-old"
    from fastapi.testclient import TestClient

    from agents_ide.api.app import create_app

    settings = client.app.state.settings
    paths = [
        f"/api/projects/{project['id']}",
        f"/api/versions/{version['id']}",
        f"/api/bindings/{binding['id']}",
        f"/api/connections/{provider['id']}",
        f"/api/harness_profiles/{profile['id']}",
        f"/api/runs/{run.json()['id']}",
    ]
    expected = {path: client.get(path).json() for path in paths}
    with TestClient(create_app(settings), base_url=settings.origin) as restarted:
        restarted.cookies.update(client.cookies)
        assert {path: restarted.get(path).json() for path in paths} == expected
        assert restarted.app.state.secrets.get(reference) == "test-old"
    assert (
        client.get(f"/api/runs/{run.json()['id']}").json()["snapshot_hash"]
        == run.json()["snapshot_hash"]
    )
    assert client.post("/api/runs", headers=headers, json=payload).json()["id"] == run.json()["id"]


@pytest.mark.parametrize(
    "url",
    [
        "https:///missing-host",
        "https://example.test?key=test-secret",
        "https://example.test#secret",
        "https://example.test:bad",
        "http://outside.test",
        "https://user:pass@example.test",
    ],
)
def test_invalid_provider_urls_do_not_leak_input(authenticated, url):
    client, headers = authenticated
    response = client.post(
        "/api/connections", headers=headers, json={"name": "bad-url", "base_url": url}
    )
    assert response.status_code in (400, 422)
    assert url not in response.text


def test_catalog_keeps_manual_models_without_claiming_probe(authenticated):
    client, headers = authenticated
    provider = client.post(
        "/api/connections",
        headers=headers,
        json={
            "name": "manual",
            "base_url": "https://example.invalid",
            "manual_models": ["m", "m"],
        },
    ).json()
    response = client.get(f"/api/connections/{provider['id']}/models").json()
    assert response["status"] == "unverified"
    assert response["models"] == [{"id": "m", "source": "manual"}]


def test_concurrent_draft_updates_have_one_winner(authenticated, tmp_path):
    client, headers = authenticated
    _, template, *_ = setup_run(client, headers, tmp_path / "workspace")
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(
            pool.map(
                lambda i: (
                    client.put(
                        f"/api/templates/{template['id']}/draft",
                        headers=headers,
                        json={"expected_version": 1, "inputs": {"winner": i}},
                    ).status_code
                ),
                range(3),
            )
        )
    assert sorted(results) == [200, 409, 409]


def test_failed_unique_update_returns_conflict_not_success(authenticated, tmp_path):
    client, headers = authenticated
    project, *_ = setup_run(client, headers, tmp_path / "workspace")
    first = client.post(
        f"/api/projects/{project['id']}/chats", headers=headers, json={"title": "first"}
    ).json()
    client.post(f"/api/projects/{project['id']}/chats", headers=headers, json={"title": "second"})
    response = client.patch(
        f"/api/chats/{first['id']}",
        headers=headers,
        json={"expected_version": 1, "title": "second"},
    )
    assert response.status_code == 409
    assert "UNIQUE constraint" not in response.text
    assert client.get(f"/api/chats/{first['id']}").json()["title"] == "first"


def test_review_migration_preserves_existing_stage2_history(authenticated, tmp_path):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    from agents_ide.persistence import database

    client, headers = authenticated
    *_, payload = setup_run(client, headers, tmp_path / "workspace")
    run = client.post("/api/runs", headers=headers, json=payload).json()
    with client.app.state.engine.connect() as connection:
        config = Config()
        config.set_main_option(
            "script_location", str(Path(database.__file__).parent / "migrations")
        )
        config.attributes["connection"] = connection
        command.downgrade(config, "0002_domain")
        connection.commit()
        before = connection.exec_driver_sql("SELECT snapshot_json FROM runs").scalar()
        command.upgrade(config, "head")
        connection.commit()
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert connection.exec_driver_sql("SELECT snapshot_json FROM runs").scalar() == before
        assert connection.exec_driver_sql("SELECT request_hash FROM runs").scalar() is None
    restored = client.get(f"/api/runs/{run['id']}").json()
    assert restored["snapshot_hash"] == run["snapshot_hash"]
    # Original request is unavailable in old records; migration must not guess it.
    repeat = client.post("/api/runs", headers=headers, json=payload)
    assert repeat.status_code == 409
    assert repeat.json()["code"] == "idempotency_unverifiable"


def test_queued_run_does_not_enable_unverified_dirty_policy(authenticated, tmp_path):
    client, headers = authenticated
    *_, payload = setup_run(client, headers, tmp_path / "workspace")
    response = client.post(
        "/api/runs",
        headers=headers,
        json={
            **payload,
            "overrides": {"dirty_policy": "allow_nonoverlap"},
        },
    )
    assert response.status_code == 409
    assert response.json()["code"] == "policy_unsupported"


@pytest.mark.parametrize(
    "path",
    [
        "relative",
        "./relative",
        "../relative",
        "~/relative",
        r"\\server\share",
        r"\\wsl$\Ubuntu\tmp",
    ],
)
def test_workspace_rejects_unsupported_paths(authenticated, path):
    client, headers = authenticated
    assert (
        client.post("/api/workspace/probe", headers=headers, json={"path": path}).status_code == 400
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows volume and junction contract")
def test_workspace_junction_alias_and_network_drive(authenticated, tmp_path, monkeypatch):
    import win32con
    import win32file

    client, headers = authenticated
    project, *_ = setup_run(client, headers, tmp_path / "workspace")
    alias = tmp_path / "alias"
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(alias), str(tmp_path / "workspace")],
        check=True,
        capture_output=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        response = client.post(
            "/api/projects", headers=headers, json={"name": "alias", "workspace_path": str(alias)}
        )
        assert response.status_code == 409
        original = client.get(f"/api/projects/{project['id']}").json()
        assert original["workspace"]["identity_ino"] == alias.stat().st_ino
    finally:
        assert alias.is_junction()
        alias.rmdir()
    monkeypatch.setattr(win32file, "GetDriveType", lambda path: win32con.DRIVE_REMOTE)
    response = client.post(
        "/api/workspace/probe", headers=headers, json={"path": str(tmp_path / "workspace")}
    )
    assert response.status_code == 400
    assert response.json()["code"] == "path_unsupported"


def test_draft_size_limit_and_invalid_name_return_validation_error(authenticated, tmp_path):
    client, headers = authenticated
    _, template, *_ = setup_run(client, headers, tmp_path / "workspace")
    response = client.put(
        f"/api/templates/{template['id']}/draft",
        headers=headers,
        json={
            "expected_version": 1,
            "graph": {"large": "а" * 600000},
        },
    )
    assert response.status_code == 422
    response = client.patch(
        f"/api/templates/{template['id']}",
        headers=headers,
        json={"expected_version": 1, "name": "bad/name"},
    )
    assert response.status_code == 422


def test_harness_credentials_are_rejected_without_echo(authenticated):
    client, headers = authenticated
    response = client.post(
        "/api/harness_profiles",
        headers=headers,
        json={
            "name": "profile",
            "harness_kind": "codex",
            "settings": {"env": {"API_KEY": "must-not-be-stored"}},
        },
    )
    assert response.status_code == 422
    assert "must-not-be-stored" not in response.text
    assert client.get("/api/harness_profiles").json() == []
