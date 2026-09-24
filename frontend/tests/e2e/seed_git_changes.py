"""Seed a pre-intent Git guard stop in the isolated browser-test database."""

import hashlib
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

data_dir, run_id, *options = sys.argv[1:]
before_dispatch = options in (["before-dispatch"], ["ignored-before-dispatch"])
engine = create_database(Settings(data_dir=Path(data_dir)))
with Session(engine) as session:
    begin_write(session)
    run = session.get(Run, run_id)
    workspace = Path(json.loads(run.snapshot_json)["workspace"]["workspace_path"])
    baseline = git.capture_baseline(workspace, run_id, ["src/**"])
    if options == ["ignored-before-dispatch"]:
        relative = "frontend/review-check.log"
        old = (workspace / relative).read_bytes()
        baseline.protected[relative] = {
            "sha256": hashlib.sha256(old).hexdigest(),
            "size": len(old),
            "mode": "100644",
            "ignored": True,
        }
    (workspace / "frontend/review-check.log").write_text("new log content\n", encoding="utf-8")
    execution_id = attempt_id = None
    if not before_dispatch:
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
        execution_id, attempt_id = execution.id, attempt.id
    run.current_node_id, run.current_execution_id, run.current_attempt_id = (
        "commit",
        execution_id,
        attempt_id,
    )
    run.state, run.state_version = "waiting_input", run.state_version + 1
    run.waiting_reason_json = to_json(
        {
            "code": "external_change_detected",
            "details": {
                "reason": "external_change_detected",
                "message": "Files outside the allowlist changed",
            },
            "allowed_actions": ["resolve", "resume", "stop", "cancel"],
        }
    )
    run.resume_target_json = to_json(
        {
            "action": "dispatch_next" if before_dispatch else "retry_attempt",
            "node_id": "commit",
            "execution_id": execution_id,
            "blockers": ["external_change_detected"],
        }
    )
    runtime = json.loads(run.runtime_json)
    runtime["git"] = {
        "baseline": baseline.to_dict(),
        "head": baseline.head_sha,
        "branch": baseline.branch,
        "allowlist": ["src/**"],
        "phase": "ready",
    }
    if before_dispatch:
        runtime["next_node_id"] = "commit"
    run.runtime_json = to_json(runtime)
    session.commit()
engine.dispose()
