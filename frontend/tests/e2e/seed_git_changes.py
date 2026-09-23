"""Seed a pre-intent Git guard stop in the isolated browser-test database."""

import json
import sys
from pathlib import Path

from sqlalchemy.orm import Session

from agents_ide.config import Settings
from agents_ide.domain.common import new_id, to_json, utc_now
from agents_ide.engine import git_commit as git
from agents_ide.persistence.database import create_database
from agents_ide.persistence.models import Run, StepAttempt, StepExecution
from agents_ide.services.transactions import begin_write

data_dir, run_id = sys.argv[1:]
engine = create_database(Settings(data_dir=Path(data_dir)))
with Session(engine) as session:
    begin_write(session)
    run = session.get(Run, run_id)
    workspace = Path(json.loads(run.snapshot_json)["workspace"]["workspace_path"])
    baseline = git.capture_baseline(workspace, run_id, ["**"])
    (workspace / "frontend/review-check.log").write_text("new log content\n", encoding="utf-8")
    execution = StepExecution(
        id=new_id(), run_id=run_id, node_id="commit", visit_index=1, cycle_id=0, status="failed"
    )
    session.add(execution)
    session.flush()
    attempt = StepAttempt(
        id=new_id(),
        execution_id=execution.id,
        attempt_index=1,
        status="unknown",
        finished_at=utc_now(),
        error_code="external_change_detected",
        error_details_json=to_json({"message": "Files outside the allowlist changed"}),
    )
    session.add(attempt)
    run.current_node_id, run.current_execution_id, run.current_attempt_id = (
        "commit",
        execution.id,
        attempt.id,
    )
    run.state, run.state_version = "waiting_input", run.state_version + 1
    run.waiting_reason_json = to_json(
        {
            "code": "external_change_detected",
            "details": {},
            "allowed_actions": ["resolve", "resume", "stop", "cancel"],
        }
    )
    run.resume_target_json = to_json(
        {
            "action": "retry_attempt",
            "node_id": "commit",
            "execution_id": execution.id,
            "blockers": ["external_change_detected"],
        }
    )
    runtime = json.loads(run.runtime_json)
    runtime["git"] = {
        "baseline": baseline.to_dict(),
        "head": baseline.head_sha,
        "branch": baseline.branch,
        "allowlist": ["**"],
    }
    run.runtime_json = to_json(runtime)
    session.commit()
engine.dispose()
