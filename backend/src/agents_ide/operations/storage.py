"""Quotas and conservative two-phase retention of expendable event payloads.

Current artifacts live in SQLite. files_json describes workspace evidence, never
owned files to delete. Unknown artifact kinds and external source_ref are retained.
"""

from __future__ import annotations

import json
import shutil
import time
from contextlib import suppress
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from agents_ide.config import Settings
from agents_ide.errors import AppError
from agents_ide.persistence.models import (
    ArtifactManifest,
    CommandJournal,
    PlanItem,
    Run,
    RunEvent,
    StepAttempt,
    StepExecution,
)
from agents_ide.security.filesystem import atomic_write
from agents_ide.services.transactions import begin_write

DETAIL_TYPES = ("agent.message_delta", "attempt.text_delta", "attempt.progress")
TERMINAL = ("completed", "failed", "cancelled")


def settings_for(session: Session) -> Settings:
    value = session.connection().info.get("storage_settings")
    assert isinstance(value, Settings)
    return value


def disk_usage(settings: Settings) -> dict[str, int]:
    # Do not traverse linked paths or user workspaces, including simulated repos.
    total = 0
    for directory in ("db", "artifacts", "logs", "secrets", "runtime", "temp", "simulated"):
        root = settings.data_dir / directory
        if root.is_symlink() or root.is_junction():
            raise AppError("path_violation", "Linked data directory", 409)
        for current, directories, files in root.walk(follow_symlinks=False):
            directories[:] = [
                name
                for name in directories
                if not (current / name).is_symlink() and not (current / name).is_junction()
            ]
            for name in files:
                path = current / name
                if not path.is_symlink():
                    with suppress(FileNotFoundError):
                        total += path.stat().st_size
    return {"used_bytes": total, "free_bytes": shutil.disk_usage(settings.data_dir).free}


def check_capacity(
    session: Session, run_id: str | None = None, *, extra: int = 0, check_events: bool = True
) -> None:
    settings = settings_for(session)
    if run_id:
        run = session.get(Run, run_id)
        if run and (run.artifact_bytes or 0) + extra > settings.run_artifact_bytes:
            raise AppError(
                "limit_exceeded", "Квота артефактов Run", 409, {"limit": "run_artifact_bytes"}
            )
        if (
            check_events
            and run
            and (run.detailed_event_count or 0) >= settings.detailed_events_limit
        ):
            raise AppError(
                "limit_exceeded",
                "Лимит подробных событий Run",
                409,
                {"limit": "detailed_events_limit"},
            )
    usage = disk_usage(settings)
    if usage["free_bytes"] < settings.disk_reserve_bytes + extra:
        raise AppError(
            "limit_exceeded", "Недостаточно свободного места", 409, {"limit": "disk_free_bytes"}
        )
    if usage["used_bytes"] + extra > settings.data_budget_bytes - settings.disk_reserve_bytes:
        raise AppError(
            "limit_exceeded", "Квота каталога данных", 409, {"limit": "data_budget_bytes"}
        )


def is_referenced(session: Session, artifact_id: str) -> bool:
    # Exact references plus conservative text matches in extensible JSON payloads.
    # Check all Runs, not only the artifact's owner. False positives retain data.
    for model, columns in (
        (
            StepExecution,
            (
                StepExecution.raw_result_ref,
                StepExecution.evidence_manifest_id,
                StepExecution.validated_result_json,
            ),
        ),
        (
            StepAttempt,
            (
                StepAttempt.request_artifact_id,
                StepAttempt.result_artifact_id,
                StepAttempt.error_details_json,
            ),
        ),
        (PlanItem, (PlanItem.evidence_ids_json,)),
        (Run, (Run.runtime_json, Run.snapshot_json, Run.resume_target_json)),
        (RunEvent, (RunEvent.payload_json,)),
        (CommandJournal, (CommandJournal.payload_json, CommandJournal.response_json)),
        (ArtifactManifest, (ArtifactManifest.body_json, ArtifactManifest.files_json)),
    ):
        query = select(model.id).where(or_(*(column.contains(artifact_id) for column in columns)))
        if model is ArtifactManifest:
            query = query.where(ArtifactManifest.id != artifact_id)
        if session.scalar(query.limit(1)):
            return True
    return False


def collect_garbage(factory: sessionmaker[Session], *, now: float | None = None) -> dict[str, int]:
    now = time.time() if now is None else now
    removed_events, purged_bytes = 0, 0
    with factory() as session:
        begin_write(session)
        settings = settings_for(session)
        cursor_path = settings.data_dir / "runtime/retention-cursor.json"
        cursors: dict[str, str] = {}
        with suppress(OSError, ValueError, TypeError):
            saved = json.loads(cursor_path.read_text())
            if isinstance(saved, dict):
                cursors = {key: value for key, value in saved.items() if isinstance(value, str)}
        eligible = list(
            session.scalars(
                select(Run)
                .where(
                    Run.state.in_(TERMINAL),
                    Run.pinned.is_(False),
                    Run.finished_at < now - settings.retention_days * 86400,
                )
                .order_by(Run.id <= cursors.get("run", ""), Run.id)
                .limit(50)
            )
        )
        for run in eligible:
            highest = (
                session.scalar(select(func.max(RunEvent.sequence)).where(RunEvent.run_id == run.id))
                or 0
            )
            # Bounded batches: event IDs establish a replay reset floor despite
            # retained critical events below that floor.
            rows = list(
                session.execute(
                    select(RunEvent.id, RunEvent.sequence)
                    .where(
                        RunEvent.run_id == run.id,
                        RunEvent.type.in_(DETAIL_TYPES),
                        RunEvent.sequence < highest,
                    )
                    .order_by(RunEvent.sequence)
                    .limit(2000)
                )
            )
            if rows:
                session.execute(delete(RunEvent).where(RunEvent.id.in_([r.id for r in rows])))
                run.retention_sequence = max(run.retention_sequence, rows[-1].sequence + 1)
                removed_events += len(rows)
            # All result/evidence/command/Git manifests remain intact. A payload
            # referenced by a retained critical event also remains intact.
        # Rotate the global artifact scan too: referenced payloads at the front
        # must not starve later unreferenced payloads or later Runs.
        candidates = list(
            session.scalars(
                select(ArtifactManifest)
                .join(Run, ArtifactManifest.run_id == Run.id)
                .where(
                    Run.state.in_(TERMINAL),
                    Run.pinned.is_(False),
                    Run.finished_at < now - settings.retention_days * 86400,
                    ArtifactManifest.schema_type == "event_payload",
                    ArtifactManifest.source_ref.is_(None),
                    ArtifactManifest.body_json.isnot(None),
                    ArtifactManifest.tombstoned_at.is_(None),
                )
                .order_by(ArtifactManifest.id <= cursors.get("artifact", ""), ArtifactManifest.id)
                .limit(100)
            )
        )
        for artifact in candidates:
            if artifact.plan_item_ids_json == "[]" and not is_referenced(session, artifact.id):
                artifact.tombstoned_at = now
        if eligible:
            cursors["run"] = eligible[-1].id
        if candidates:
            cursors["artifact"] = candidates[-1].id
        # This file is only a fairness hint, never deletion authority. Write
        # under the DB writer lock; a crash/rollback can skip only one rotation.
        atomic_write(cursor_path, json.dumps(cursors).encode())
        session.commit()
    # A crash here leaves durable tombstones; the next invocation rechecks every
    # reference and protection before completing deletion in a separate commit.
    with factory() as session:
        begin_write(session)
        for artifact in session.scalars(
            select(ArtifactManifest)
            .where(
                ArtifactManifest.tombstoned_at.isnot(None),
                ArtifactManifest.purged_at.is_(None),
            )
            .limit(500)
        ):
            owner_run = session.get(Run, artifact.run_id)
            if (
                not owner_run
                or owner_run.state not in TERMINAL
                or owner_run.pinned
                or owner_run.finished_at is None
                or owner_run.finished_at >= now - settings.retention_days * 86400
                or artifact.schema_type != "event_payload"
                or artifact.plan_item_ids_json != "[]"
                or artifact.source_ref is not None
                or is_referenced(session, artifact.id)
            ):
                artifact.tombstoned_at = None
                continue
            purged_bytes += artifact.byte_length
            owner_run.artifact_bytes = max(0, owner_run.artifact_bytes - artifact.byte_length)
            artifact.body_json = None
            artifact.purged_at = now
        session.commit()
    return {"events_removed": removed_events, "artifact_bytes_purged": purged_bytes}


def set_pin(session: Session, run_id: str, pinned: bool) -> dict[str, Any]:
    begin_write(session)
    run = session.get(Run, run_id)
    if run is None:
        raise AppError("run_not_found", "Run не найден", 404)
    run.pinned = pinned
    session.commit()
    return {"run_id": run_id, "pinned": pinned}
