"""Seed revision drift before a commit attempt in the isolated browser-test DB."""

import json
import sys
from pathlib import Path

from sqlalchemy.orm import Session

from agents_ide.config import Settings
from agents_ide.domain.common import new_id, to_json, utc_now
from agents_ide.persistence.database import create_database
from agents_ide.persistence.models import ProviderConnection, Run, StepAttempt, StepExecution
from agents_ide.services.transactions import begin_write

data_dir, run_id = sys.argv[1:3]
guard_retry = sys.argv[3:] == ["guard-retry"]
engine = create_database(Settings(data_dir=Path(data_dir)))
with Session(engine) as session:
    begin_write(session)
    run = session.get(Run, run_id)
    generation = json.loads(run.snapshot_json)["dependencies"]["nodes"]["commit"][
        "message_generation"
    ]
    connection = session.get(ProviderConnection, generation["connection_id"])
    connection.version += 1
    execution = StepExecution(
        id=new_id(),
        run_id=run_id,
        node_id="commit",
        visit_index=1,
        cycle_id=0,
        status="waiting_input",
    )
    session.add(execution)
    session.flush()
    run.current_node_id, run.current_execution_id, run.current_attempt_id = (
        "commit",
        execution.id,
        None,
    )
    if guard_retry:
        attempt = StepAttempt(
            id=new_id(),
            execution_id=execution.id,
            attempt_index=1,
            status="unknown",
            external_outcome="unknown",
            error_code="external_change_detected",
            finished_at=utc_now(),
        )
        session.add(attempt)
        session.flush()
        execution.attempt_count = 1
        run.current_attempt_id = attempt.id
        runtime = json.loads(run.runtime_json)
        runtime["retry_authorized_attempts"] = [attempt.id]
        run.runtime_json = to_json(runtime)
    run.state, run.state_version = "waiting_input", run.state_version + 1
    run.waiting_reason_json = to_json(
        {
            "code": "configuration_invalid",
            "details": {"reason": "resource_changed"},
            "allowed_actions": ["resolve", "resume", "stop", "cancel"],
        }
    )
    run.resume_target_json = to_json(
        {
            "action": "retry_attempt",
            "node_id": "commit",
            "execution_id": execution.id,
            "blockers": ["configuration_invalid"],
        }
    )
    session.commit()
engine.dispose()
