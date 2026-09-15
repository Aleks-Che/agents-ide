"""Prepare stopped checkpoints in the isolated Playwright database; never starts a worker."""

import json
import sys
from pathlib import Path

from sqlalchemy import URL, create_engine, delete, select
from sqlalchemy.orm import Session

from agents_ide.domain.common import utc_now
from agents_ide.engine.artifacts import ArtifactPayload, record_artifact
from agents_ide.engine.events import append_event
from agents_ide.persistence.models import QueueJob, Run, RunEvent, WorkspaceReservation

directory = Path(sys.argv[1]).resolve()
local = Path(__file__).resolve().parents[3] / ".local"
# This fixture may mutate only a disposable per-run Playwright database.
assert directory.is_relative_to(local.resolve()) and directory.name.startswith("e2e-")
engine = create_engine(URL.create("sqlite", database=str(directory / "db/agents-ide.db")))
with Session(engine) as session:
    session.connection().exec_driver_sql("BEGIN IMMEDIATE")
    run = session.get(Run, sys.argv[2])
    assert run is not None
    session.execute(delete(QueueJob).where(QueueJob.run_id == run.id))
    if sys.argv[3] == "completed":
        run.state = "completed"
        run.finished_at = utc_now()
        for reservation in session.scalars(
            select(WorkspaceReservation).where(WorkspaceReservation.run_id == run.id)
        ):
            reservation.released_at = utc_now()
    else:
        run.state = "waiting_input"
        run.waiting_reason_json = json.dumps(
            {
                "code": "limit_exceeded",
                "details": {"limit": "max_calls"},
                "allowed_actions": ["resolve", "resume", "stop", "cancel"],
            }
        )
        run.runtime_json = json.dumps(
            {"external_calls": 100, "waiting_code": "limit_exceeded", "budget_quality": "unknown"}
        )
        run.resume_target_json = json.dumps(
            {"action": "dispatch_next", "node_id": "end", "blockers": ["limit_exceeded"]}
        )
        for index in range(405):
            append_event(
                session,
                run.id,
                "budget.updated",
                {"external_calls": index, "quality": "unknown"},
                simulated=True,
            )
        # Exercise a stale cursor and REST pagination before reconnecting SSE.
        session.execute(delete(RunEvent).where(RunEvent.run_id == run.id, RunEvent.sequence < 6))
        record_artifact(
            session,
            run.id,
            ArtifactPayload("review_alpha", body={"value": "alpha-body"}),
            source_kind="simulated",
        )
        record_artifact(
            session,
            run.id,
            ArtifactPayload("review_beta", body={"value": "beta-body"}),
            source_kind="simulated",
        )
    session.commit()
engine.dispose()
