"""Release operations on populated databases and real local files."""

import copy
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import portalocker
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from test_stage4_review import execute, make_run

from agents_ide.config import Settings
from agents_ide.engine.artifacts import ArtifactPayload, record_artifact
from agents_ide.engine.events import append_event
from agents_ide.errors import AppError
from agents_ide.operations import backup as backups
from agents_ide.operations.diagnostics import diagnostics
from agents_ide.operations.maintenance import clear_interrupted, offline
from agents_ide.operations.storage import check_capacity, collect_garbage, set_pin
from agents_ide.persistence import database
from agents_ide.persistence.models import ArtifactManifest, PipelineTemplate, PipelineVersion, Run
from agents_ide.security.filesystem import prepare_data_dir
from agents_ide.security.secrets import SecretStore


def new_target(tmp_path):
    settings = Settings(data_dir=tmp_path / "restored", port=18766)
    prepare_data_dir(settings.data_dir)
    return settings


def test_backup_restore_preserves_results_and_requires_reconciliation(
    authenticated, tmp_path, settings, monkeypatch
):
    run, factory = make_run(authenticated, tmp_path)
    assert execute(run, factory, settings, monkeypatch)[0].final_state.value == "completed"
    queued, _ = make_run(authenticated, tmp_path, suffix="queued")
    with factory() as session:
        original = {r.id: r.snapshot_json for r in session.scalars(select(Run))}
    destination = tmp_path / "backup"
    backups.backup(settings, destination)
    manifest = backups.verify_backup(destination)
    assert manifest["workspaces"] == "excluded"
    assert not (destination / "workspace").exists()
    target = new_target(tmp_path)
    backups.restore(target, destination)
    with closing(sqlite3.connect(target.database_path)) as connection:
        assert dict(connection.execute("SELECT id,snapshot_json FROM runs")) == original
        assert (
            connection.execute("SELECT state FROM runs WHERE id=?", (run["id"],)).fetchone()[0]
            == "completed"
        )
        assert (
            connection.execute("SELECT state FROM runs WHERE id=?", (queued["id"],)).fetchone()[0]
            == "recovering"
        )
        assert connection.execute("SELECT COUNT(*) FROM auth_sessions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM pairing").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM queue_jobs").fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    # A newly restored queued Run may reconcile, but cannot invoke a model.
    engine = database.create_database(target)
    from sqlalchemy.orm import sessionmaker

    restored_factory = sessionmaker(engine, expire_on_commit=False)
    try:
        result, _ = execute(queued, restored_factory, target, monkeypatch)
        assert result.final_state.value == "paused"
    finally:
        engine.dispose()
    with pytest.raises(AppError, match="new|новый"):
        backups.restore(target, destination)


@pytest.mark.parametrize(
    "corruption", ["hash", "traversal", "absolute", "duplicate", "missing", "version"]
)
def test_backup_rejects_damage_before_publishing(authenticated, tmp_path, settings, corruption):
    make_run(authenticated, tmp_path)
    source = tmp_path / "backup"
    backups.backup(settings, source)
    manifest_path = source / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if corruption == "hash":
        manifest["files"][0]["sha256"] = "0" * 64
    elif corruption == "traversal":
        manifest["files"][0]["path"] = "../outside"
    elif corruption == "absolute":
        manifest["files"][0]["path"] = "C:/outside"
    elif corruption == "duplicate":
        manifest["files"].append(manifest["files"][0])
    elif corruption == "missing":
        manifest["files"] = []
    else:
        manifest["format"] = 999
    manifest_path.write_text(json.dumps(manifest))
    target = new_target(tmp_path)
    with pytest.raises(AppError):
        backups.restore(target, source)
    assert not target.database_path.exists()


def test_backup_interruption_has_no_complete_manifest(
    authenticated, tmp_path, settings, monkeypatch
):
    make_run(authenticated, tmp_path)
    monkeypatch.setattr(
        backups, "_check_artifacts", lambda *_: (_ for _ in ()).throw(OSError("full"))
    )
    source = tmp_path / "partial"
    with pytest.raises(OSError):
        backups.backup(settings, source)
    assert not (source / "manifest.json").exists()
    assert not (settings.data_dir / "runtime/maintenance-request").exists()


def test_ciphertext_is_optional_and_unavailable_secret_is_non_destructive(
    authenticated, tmp_path, settings
):
    run, factory = make_run(authenticated, tmp_path)
    reference = "a" * 32
    ciphertext = settings.data_dir / f"secrets/{reference}.dpapi"
    ciphertext.write_bytes(b"ciphertext-from-another-Windows-profile")
    from agents_ide.persistence.models import ProviderConnection

    with factory() as session:
        connection = session.scalar(select(ProviderConnection))
        connection.secret_reference = reference
        session.commit()
    source = tmp_path / "with-secrets"
    backups.backup(settings, source, include_secrets=True)
    target = new_target(tmp_path)
    backups.restore(target, source)
    restored_ciphertext = target.data_dir / f"secrets/{reference}.dpapi"
    assert restored_ciphertext.read_bytes() == ciphertext.read_bytes()
    with pytest.raises(AppError) as error:
        SecretStore(target.data_dir / "secrets").get(reference)
    assert error.value.code == "secret_unavailable"
    report = diagnostics(target)
    assert report["connections"][0]["access"] == "secret_unavailable"
    assert reference not in json.dumps(report)
    assert "ciphertext-from" not in json.dumps(report)
    assert restored_ciphertext.exists()


def test_populated_migration_preset_update_preserves_user_versions(
    authenticated, tmp_path, settings, monkeypatch
):
    from agents_ide.services import presets

    run, factory = make_run(authenticated, tmp_path)
    with factory() as session:
        append_event(session, run["id"], "attempt.progress", {"text": "x"})
        artifact = record_artifact(session, run["id"], ArtifactPayload("result", body="ok"))
        session.commit()
        original = session.get(Run, run["id"]).snapshot_json
        user_versions = [
            (r.id, r.execution_hash)
            for r in session.scalars(
                select(PipelineVersion)
                .join(PipelineTemplate)
                .where(PipelineTemplate.kind != "system")
            )
        ]
    with authenticated[0].app.state.engine.connect() as connection:
        config = Config()
        config.set_main_option(
            "script_location", str(Path(database.__file__).parent / "migrations")
        )
        config.attributes["connection"] = connection
        command.downgrade(config, "0016_council_single_member")
        connection.commit()
    definition = presets.list_builtin_presets()[0]
    changed = copy.deepcopy(definition.body)
    next(node for node in changed["graph"]["nodes"] if node["type"] == "AgentTask")["config"][
        "prompt"
    ] = "Updated preset prompt"
    monkeypatch.setattr(
        presets,
        "get_builtin_preset",
        lambda _: presets.PresetDefinition(definition.name, changed, definition.source_path),
    )
    result = backups.update(settings, tmp_path / "pre-update")
    assert result["nonterminal_runs"] == 1
    with factory() as session:
        row = session.get(Run, run["id"])
        assert row.snapshot_json == original
        assert row.artifact_bytes == artifact.byte_length and row.detailed_event_count == 1
        assert [
            (r.id, r.execution_hash)
            for r in session.scalars(
                select(PipelineVersion)
                .join(PipelineTemplate)
                .where(PipelineTemplate.kind != "system")
            )
        ] == user_versions


@pytest.mark.parametrize(
    "field,value", [("schema_version", "2.0.0"), ("required_features_json", '["future"]')]
)
def test_update_rejects_unsupported_versions(authenticated, tmp_path, settings, field, value):
    _, factory = make_run(authenticated, tmp_path)
    # An old/foreign DB may contain unsupported data. Do not weaken triggers in the product.
    with factory() as session:
        version = session.scalar(select(PipelineVersion))
        foreign = PipelineVersion(
            **{
                column.name: getattr(version, column.name)
                for column in PipelineVersion.__table__.columns
            }
        )
        foreign.id, foreign.version_number, foreign.execution_hash = "foreign", 999, "f" * 64
        setattr(foreign, field, value)
        session.add(foreign)
        session.commit()
    with pytest.raises(AppError) as error:
        backups.update(settings, tmp_path / "should-not-exist")
    assert error.value.code == "schema_unsupported"
    assert not (tmp_path / "should-not-exist").exists()


def test_retention_preserves_active_pinned_references_and_critical_evidence(
    authenticated, tmp_path, settings
):
    run, factory = make_run(authenticated, tmp_path)
    other, _ = make_run(authenticated, tmp_path, suffix="other")
    with factory() as session:
        append_event(session, run["id"], "agent.message_delta", {"text": "x" * 20000})
        append_event(session, run["id"], "run.completed", {})
        critical = record_artifact(session, run["id"], ArtifactPayload("evidence", body="evidence"))
        shared = record_artifact(
            session, run["id"], ArtifactPayload("event_payload", body="shared")
        )
        session.get(Run, other["id"]).runtime_json = json.dumps({"evidence": shared.id})
        session.commit()
    assert collect_garbage(factory)["events_removed"] == 0
    with factory() as session:
        row = session.get(Run, run["id"])
        row.state, row.finished_at = "completed", time.time() - 40 * 86400
        row.pinned = True
        session.commit()
    assert collect_garbage(factory)["events_removed"] == 0
    with factory() as session:
        set_pin(session, run["id"], False)
    result = collect_garbage(factory)
    assert result["events_removed"] == 1 and result["artifact_bytes_purged"] > 0
    with factory() as session:
        assert session.get(ArtifactManifest, critical.id).body_json == '"evidence"'
        assert session.get(ArtifactManifest, shared.id).body_json == '"shared"'
        purged = session.scalar(
            select(ArtifactManifest).where(ArtifactManifest.purged_at.isnot(None))
        )
        assert purged.content_hash and purged.body_json is None
    url = f"/api/runs/{run['id']}"
    assert authenticated[0].get(url + "/events?after=0").json()["reset_required"]
    snapshot = authenticated[0].get(url + "/snapshot").json()
    assert (
        not authenticated[0]
        .get(url + f"/events?after={snapshot['last_sequence']}")
        .json()["reset_required"]
    )
    assert authenticated[0].get(url + f"/artifacts/{purged.id}/content").status_code == 410
    assert collect_garbage(factory) == {"events_removed": 0, "artifact_bytes_purged": 0}


def test_quota_rollback_keeps_critical_events_and_prevents_new_dispatch(
    authenticated, tmp_path, settings, monkeypatch
):
    run, factory = make_run(authenticated, tmp_path)
    settings.detailed_events_limit = 1
    with factory() as session:
        append_event(session, run["id"], "attempt.progress", {})
        session.commit()
    with factory() as session, pytest.raises(AppError):
        append_event(session, run["id"], "attempt.progress", {})
    with factory() as session:
        append_event(session, run["id"], "command.finished", {"status": "ok"})
        session.commit()
    result, _ = execute(run, factory, settings, monkeypatch)
    assert result.final_state.value == "waiting_input"
    assert result.waiting_reason.code == "limit_exceeded"
    settings.detailed_events_limit = 2
    with factory() as session:
        check_capacity(session, run["id"])
    settings.run_artifact_bytes = 1024
    with factory() as session, pytest.raises(AppError):
        record_artifact(session, run["id"], ArtifactPayload("result", body="x" * 2048))
    with factory() as session:
        assert session.get(Run, run["id"]).artifact_bytes == 0


def test_disk_reserve_blocks_work_and_maintenance_is_fail_closed(
    authenticated, tmp_path, settings, monkeypatch
):
    run, factory = make_run(authenticated, tmp_path)
    from agents_ide.operations import storage

    monkeypatch.setattr(storage, "disk_usage", lambda _: {"used_bytes": 0, "free_bytes": 1})
    with factory() as session, pytest.raises(AppError) as error:
        check_capacity(session, run["id"])
    assert error.value.details["limit"] == "disk_free_bytes"
    marker = settings.data_dir / "runtime/maintenance-request"
    marker.write_text("interrupted")
    client, headers = authenticated
    assert (
        client.post("/api/templates", headers=headers, json={"name": "blocked"}).status_code == 503
    )
    assert client.get("/api/health").status_code == 200
    with (
        portalocker.Lock(str(settings.data_dir / "runtime/worker.lock"), timeout=0),
        pytest.raises(portalocker.LockException),
    ):
        clear_interrupted(settings)
    assert marker.exists()
    clear_interrupted(settings)
    assert not marker.exists()


def test_maintenance_pauses_at_boundary_without_calling_adapter(
    authenticated, tmp_path, settings, monkeypatch
):
    run, factory = make_run(authenticated, tmp_path)
    (settings.data_dir / "runtime/maintenance-request").write_text("backup")
    result, _ = execute(run, factory, settings, monkeypatch)
    assert result.final_state.value == "paused"
    from agents_ide.persistence.models import StepAttempt

    with factory() as session:
        assert session.scalar(select(StepAttempt)) is None
    clear_interrupted(settings)


def test_live_manual_writer_lock_refuses_backup(authenticated, tmp_path, settings):
    make_run(authenticated, tmp_path)
    with (
        portalocker.Lock(str(settings.data_dir / "runtime/api.lock"), timeout=0),
        pytest.raises(portalocker.LockException),
        offline(settings, "test"),
    ):
        pytest.fail("must not enter with a live writer")
    assert not (settings.data_dir / "runtime/maintenance-request").exists()


@pytest.mark.parametrize("name", ["artifacts/con", "artifacts/a.", "artifacts//a", "artifacts/a "])
def test_backup_rejects_windows_path_aliases(tmp_path, name):
    with pytest.raises(AppError):
        backups.safe_path(tmp_path, name)


def test_gc_recovers_committed_tombstone_and_rechecks_new_pin(authenticated, tmp_path):
    run, factory = make_run(authenticated, tmp_path)
    with factory() as session:
        row = session.get(Run, run["id"])
        row.state, row.finished_at, row.pinned = "completed", time.time() - 40 * 86400, True
        artifact = record_artifact(
            session, run["id"], ArtifactPayload("event_payload", body="keep")
        )
        artifact.tombstoned_at = time.time()  # Prior GC committed its first phase, then crashed.
        session.commit()
    assert collect_garbage(factory)["artifact_bytes_purged"] == 0
    with factory() as session:
        kept = session.get(ArtifactManifest, artifact.id)
        assert kept.body_json == '"keep"' and kept.tombstoned_at is None
        session.get(Run, run["id"]).pinned = False
        kept.tombstoned_at = time.time()
        session.commit()
    assert collect_garbage(factory)["artifact_bytes_purged"] == len('"keep"')


def test_backup_rejects_unmanifested_wal(authenticated, tmp_path, settings):
    make_run(authenticated, tmp_path)
    source = tmp_path / "snapshot"
    backups.backup(settings, source)
    (source / "db/agents-ide.db-wal").write_bytes(b"unmanifested")
    with pytest.raises(AppError) as error:
        backups.verify_backup(source)
    assert error.value.code == "backup_invalid"


def test_retention_rotates_past_full_batches_of_runs_and_protected_artifacts(
    authenticated, tmp_path
):
    run, factory = make_run(authenticated, tmp_path)
    with factory() as session:
        original = session.get(Run, run["id"])
        values = {column.key: getattr(original, column.key) for column in Run.__table__.columns}
        for index in range(52):
            row = Run(
                **{
                    **values,
                    "id": f"{index:032x}",
                    "idempotency_key": f"retention-{index}",
                    "state": "completed",
                    "finished_at": time.time() - 40 * 86400,
                }
            )
            session.add(row)
            session.flush()
            append_event(session, row.id, "agent.message_delta", {"text": "old"})
            append_event(session, row.id, "run.completed", {})
        protected = []
        for index in range(101):
            artifact = record_artifact(session, row.id, ArtifactPayload("event_payload", body="x"))
            artifact.id = f"{index:032x}"
            if index < 100:
                protected.append(artifact.id)
        original.runtime_json = json.dumps({"keep": protected})
        session.commit()
    first = collect_garbage(factory)
    assert first == {"events_removed": 50, "artifact_bytes_purged": 0}
    second = collect_garbage(factory)
    assert second == {"events_removed": 2, "artifact_bytes_purged": len('"x"')}
    with factory() as session:
        assert all(session.get(ArtifactManifest, key).body_json == '"x"' for key in protected)
        assert session.get(ArtifactManifest, f"{100:032x}").body_json is None
