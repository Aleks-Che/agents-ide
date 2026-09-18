"""Short serialized queue transactions. Expiry requires recovery, never replay."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass

import psutil
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from agents_ide.domain.common import new_id, utc_now
from agents_ide.domain.workspace import reservations_overlap
from agents_ide.engine.ownership import owner_may_be_alive
from agents_ide.errors import AppError
from agents_ide.persistence.models import QueueJob, Run, WorkspaceReservation
from agents_ide.services.transactions import begin_write

MAX_ACTIVE_RUNS = 2


@dataclass(frozen=True)
class ClaimedJob:
    job_id: str
    run_id: str
    generation: int
    lease_expires_at: float


def owned_job(
    session: Session, run_id: str, worker_id: str, generation: int, *, now: float | None = None
) -> QueueJob:
    job = session.scalar(select(QueueJob).where(QueueJob.run_id == run_id))
    if (
        job is None
        or job.claimed_by != worker_id
        or job.generation != generation
        or job.lease_expires_at is None
    ):
        raise AppError("queue_job_lost", "Владение заданием изменилось или освобождено", 409)
    return job


def claim_next_job(
    session_factory: sessionmaker[Session],
    *,
    worker_id: str,
    lease_seconds: float,
    now: float | None = None,
) -> ClaimedJob | None:
    with session_factory() as session:
        begin_write(session)
        now_value = utc_now() if now is None else now
        expired = list(
            session.scalars(
                select(QueueJob).where(
                    QueueJob.claimed_by.isnot(None), QueueJob.lease_expires_at <= now_value
                )
            )
        )
        orphaned = list(
            session.scalars(
                select(QueueJob)
                .join(Run, Run.id == QueueJob.run_id)
                .where(
                    Run.state.in_(
                        ["running", "retry_wait", "pause_requested", "stop_requested", "recovering"]
                    ),
                    or_(QueueJob.claimed_by.is_(None), QueueJob.lease_expires_at.is_(None)),
                )
            )
        )
        expired.extend(job for job in orphaned if job not in expired)
        for job in expired:
            if job.claimed_by and owner_may_be_alive(job.owner_pid, job.owner_create_time):
                continue
            run = session.get(Run, job.run_id)
            if run is not None and run.state not in {
                "completed",
                "failed",
                "cancelled",
                "recovering",
            }:
                from agents_ide.engine.events import append_event

                previous = run.state
                run.state = "recovering"
                run.state_version += 1
                run.updated_at = now_value
                target = json.loads(run.resume_target_json or "{}")
                target.update(
                    {
                        "action": "reconcile",
                        "previous_action": target.get("action"),
                        "execution_id": run.current_execution_id,
                        "node_id": run.current_node_id,
                        "previous_owner": {
                            "pid": job.owner_pid,
                            "create_time": job.owner_create_time,
                        },
                        "blockers": list(
                            dict.fromkeys([*target.get("blockers", []), "owner_expired"])
                        ),
                    }
                )
                run.resume_target_json = json.dumps(target)
                append_event(
                    session,
                    run.id,
                    "run.state_changed",
                    {
                        "from": previous,
                        "to": run.state,
                        "state_version": run.state_version,
                        "reason": "owner_expired",
                    },
                )
            # Transfer only the lease. The reservation remains continuously active;
            # recovery must prove the old operation stopped before any dispatch.
            job.claimed_by, job.lease_expires_at = None, None
        session.flush()
        active = (
            session.scalar(
                select(func.count()).select_from(QueueJob).where(QueueJob.claimed_by.isnot(None))
            )
            or 0
        )
        if active >= MAX_ACTIVE_RUNS:
            session.commit()
            return None
        reservations = list(
            session.scalars(
                select(WorkspaceReservation).where(WorkspaceReservation.released_at.is_(None))
            )
        )
        jobs = session.scalars(
            select(QueueJob)
            .join(Run, Run.id == QueueJob.run_id)
            .where(
                Run.state.in_(["queued", "recovering"]),
                QueueJob.claimed_by.is_(None),
                QueueJob.available_at <= now_value,
            )
            .order_by(QueueJob.available_at, QueueJob.created_at, QueueJob.id)
        )
        for job in jobs:
            run = session.get(Run, job.run_id)
            assert run is not None
            from agents_ide.engine.worktrees import effective_workspace, reservation_scope

            snapshot, runtime = json.loads(run.snapshot_json), json.loads(run.runtime_json)
            workspace = effective_workspace(snapshot, runtime)
            scope = reservation_scope(snapshot, runtime) if workspace.get("scope") else None
            if scope is None:
                continue
            blocking = [
                r
                for r in reservations
                if r.run_id != run.id
                and (
                    r.workspace_json is None
                    or reservations_overlap(scope, json.loads(r.workspace_json))
                )
            ]
            if blocking:
                continue
            generation = max(job.generation, run.worker_generation or 0) + 1
            lease = now_value + lease_seconds
            job.claimed_by, job.generation, job.lease_expires_at = worker_id, generation, lease
            job.owner_pid, job.owner_create_time = os.getpid(), psutil.Process().create_time()
            run.worker_id, run.worker_generation = worker_id, generation
            prior = [r for r in reservations if r.run_id == run.id]
            for reservation in prior:
                reservation.owner_generation = generation
                reservation.lease_expires_at = lease
            if not prior:
                session.add(
                    WorkspaceReservation(
                        id=new_id(),
                        workspace_identity_dev=workspace["identity_dev"],
                        workspace_identity_ino=workspace["identity_ino"],
                        workspace_json=json.dumps(scope),
                        run_id=run.id,
                        owner_generation=generation,
                        lease_expires_at=lease,
                        created_at=now_value,
                        released_at=None,
                    )
                )
            result = ClaimedJob(job.id, run.id, generation, lease)
            session.commit()  # Flush ORM updates before committing; SQL COMMIT alone loses them.
            return result
        session.commit()
        return None


def refresh_lease(
    session_factory: sessionmaker[Session],
    *,
    job_id: str,
    worker_id: str,
    expected_generation: int,
    lease_seconds: float,
    now: float | None = None,
    cancel: threading.Event | None = None,
) -> float:
    with session_factory() as session:
        begin_write(session, cancel=cancel)
        now_value = utc_now() if now is None else now
        row = session.get(QueueJob, job_id)
        if row is None:
            raise AppError("queue_job_missing", "Задание не найдено", 409)
        job = owned_job(session, row.run_id, worker_id, expected_generation, now=now_value)
        job.lease_expires_at = now_value + lease_seconds
        for reservation in session.scalars(
            select(WorkspaceReservation).where(
                WorkspaceReservation.run_id == job.run_id,
                WorkspaceReservation.released_at.is_(None),
                WorkspaceReservation.owner_generation == expected_generation,
            )
        ):
            reservation.lease_expires_at = job.lease_expires_at
        session.commit()
        return now_value + lease_seconds


def abandon_job(
    session_factory: sessionmaker[Session], *, job_id: str, worker_id: str, generation: int
) -> None:
    """A finished dispatcher explicitly relinquishes ownership for recovery.

    The reservation and operation evidence stay in place until reconciliation.
    A live worker process alone must not retain an abandoned dispatch forever.
    """
    with session_factory() as session:
        begin_write(session)
        job = session.get(QueueJob, job_id)
        if job and job.claimed_by == worker_id and job.generation == generation:
            job.owner_pid, job.owner_create_time = None, None
            job.lease_expires_at = 0
        session.commit()


def release_job(
    session_factory: sessionmaker[Session],
    *,
    job_id: str,
    worker_id: str,
    expected_generation: int | None = None,
) -> None:
    with session_factory() as session:
        begin_write(session)
        job = session.get(QueueJob, job_id)
        if job is None:
            return
        owned_job(
            session,
            job.run_id,
            worker_id,
            expected_generation if expected_generation is not None else job.generation,
        )
        run = session.get(Run, job.run_id)
        if run is not None and run.state in {"running", "retry_wait", "recovering"}:
            raise AppError("queue_job_busy", "Незавершённое исполнение требует сверки", 409)
        if run is not None and run.state == "queued":
            job.claimed_by, job.lease_expires_at = None, None
            job.available_at = utc_now() + 0.25
        else:
            session.delete(job)
        # Only a confirmed terminal outcome releases the workspace.
        if run is not None and run.state in {"completed", "failed", "cancelled", "queued"}:
            for reservation in session.scalars(
                select(WorkspaceReservation).where(
                    WorkspaceReservation.run_id == run.id,
                    WorkspaceReservation.released_at.is_(None),
                    WorkspaceReservation.owner_generation == job.generation,
                )
            ):
                reservation.released_at = utc_now()
        session.commit()


def enqueue_run(session: Session, run_id: str, *, available_at: float | None = None) -> QueueJob:
    job = QueueJob(
        id=new_id(),
        run_id=run_id,
        available_at=utc_now() if available_at is None else available_at,
        generation=1,
        created_at=utc_now(),
    )
    session.add(job)
    session.flush()
    return job


def find_active_runs(session: Session) -> list[str]:
    return list(session.scalars(select(QueueJob.run_id).where(QueueJob.claimed_by.isnot(None))))
